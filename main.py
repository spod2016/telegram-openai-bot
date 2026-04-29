import logging
import random
import string
import base64
import io
import textwrap
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOVELAI_API_KEY = os.getenv("NOVELAI_API_KEY")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent game analytics log
# ---------------------------------------------------------------------------
import json as _json

GAME_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_log.jsonl")
_game_log_lock = threading.Lock()

def log_game_event(event: str, data: dict) -> None:
    """Append a JSON line to the persistent game log and emit to deployment logs."""
    record = {"event": event, "ts": datetime.utcnow().isoformat(), **data}
    # Always emit to deployment logs (queryable via log tooling)
    logger.info("GAME_EVENT: " + _json.dumps(record, ensure_ascii=False))
    # Also write to local file (persists on deployed VM)
    try:
        with _game_log_lock:
            with open(GAME_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"game_log write failed: {exc}")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
games: dict = {}
comic_sessions: dict = {}
pending_comic_input: dict = {}  # chat_id -> token, for free-text comic turns

# ---------------------------------------------------------------------------
# ConversationHandler states
# ---------------------------------------------------------------------------
WAITING_FOR_STYLE = 1
WAITING_FOR_ANSWER = 2
WAITING_FOR_SOLO_ANSWER = 3
WAITING_FOR_AGE_CONFIRM = 4    # host age confirmation before adult game starts
WAITING_FOR_PLAYER_AGE  = 5    # joining player age confirmation for adult games

# ---------------------------------------------------------------------------
# Game config
# ---------------------------------------------------------------------------
ROLES = ["WHO", "WHAT ARE THEY DOING", "WHERE", "MOOD", "TWIST"]
ROLE_QUESTIONS = [
    "🎭 You are the WHO. Who is the main character? (reply with a person or character)",
    "🎬 You are the ACTION. What are they doing? (reply with an action or activity)",
    "📍 You are the WHERE. Where does it happen? (reply with a location or place)",
    "🌫️ You are the MOOD. What is the atmosphere or tone? (reply with a mood or feeling, e.g. eerie, joyful, tense)",
    "🌀 You are the TWIST. Add an unexpected detail! (reply with something surprising or bizarre)",
]

STYLES = [
    ("Comic Book",      "comic book style, vibrant colors, bold outlines, halftone dots, action panels"),
    ("Watercolor",      "soft watercolor illustration, pastel tones, flowing washes, delicate brushstrokes"),
    ("Pixel Art",       "retro pixel art, 16-bit style, chunky pixels, game sprite aesthetic"),
    ("Oil Painting",    "classical oil painting, rich textures, dramatic lighting, old masters style"),
    ("Anime",           "anime style, clean linework, expressive characters, colorful cel shading"),
    ("Noir",            "black and white noir, high contrast, dramatic shadows, film noir atmosphere"),
    ("Surrealist",      "surrealist dreamscape, Salvador Dali inspired, melting reality, bizarre imagery"),
    ("Cyberpunk",       "cyberpunk aesthetic, neon lights, dark dystopian city, futuristic glowing elements"),
    ("Children's Book", "children's book illustration, cute, warm, simple shapes, friendly characters"),
    ("Renaissance",     "Renaissance painting style, classical composition, chiaroscuro lighting, museum quality"),
]

# Adult style — separate entry, uses NovelAI
ADULT_STYLE = ("🔞 Anime Adult", "explicit adult anime content, highly detailed, sensual illustration")

ADULT_STYLE_PROMPT_SUFFIX = (
    "explicit adult content, highly detailed, mature themes, sensual, erotic illustration. "
    "No censorship."
)

GAME_TIMEOUT_MINUTES = 30
STRIP_LINE_H = 18          # pixel height per text line in comic strip captions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_token(length=6) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


def is_game_expired(game: dict) -> bool:
    return datetime.utcnow() - game["created_at"] > timedelta(minutes=GAME_TIMEOUT_MINUTES)


def build_style_menu(include_adult: bool = True) -> str:
    lines = ["🎨 <b>Choose an art style for your image:</b>\n"]
    for i, (name, _) in enumerate(STYLES, 1):
        lines.append(f"{i}. {name}")
    lines.append("\nReply with a number (1–10).")
    return "\n".join(lines)


def display_name_for(user) -> str:
    if user.username:
        return f"@{user.username}"
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"
    return name


def _dbg(debug_log: list | None, step: str, data: str) -> None:
    """Append a debug entry if debug_log is active."""
    if debug_log is None:
        return
    import time
    debug_log.append({
        "ts":   time.strftime("%H:%M:%S", time.gmtime()),
        "step": step,
        "data": data,
    })
    logger.debug(f"[DBG] {step}: {data[:120]}")


async def _shorten_url(url: str) -> str:
    """Shorten a URL using is.gd. Returns original URL on failure."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                "https://is.gd/create.php",
                params={"format": "simple", "url": url},
            )
        if response.status_code == 200 and response.text.strip().startswith("https://"):
            short = response.text.strip()
            logger.info(f"Shortened {url} → {short}")
            return short
    except Exception as e:
        logger.warning(f"URL shortening failed: {e}")
    return url


# ---------------------------------------------------------------------------
# /create flow
# ---------------------------------------------------------------------------

async def create_game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This bot only works in private chats.")
        return ConversationHandler.END

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /create N (where N is between 1 and 5)")
        return ConversationHandler.END

    n = int(args[0])
    if n < 1 or n > 5:
        await update.message.reply_text("❌ Number of players must be between 1 and 5.")
        return ConversationHandler.END

    context.user_data["pending_num_players"] = n
    await update.message.reply_text(build_style_menu(include_adult=True), parse_mode="HTML")
    return WAITING_FOR_STYLE


async def receive_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Secret passphrase triggers adult debug mode
    if text.lower() == "ddd":
        if not NOVELAI_API_KEY:
            await update.message.reply_text(
                "⚠️ Adult mode is not configured on this server. "
                "Please choose a style from 1–10."
            )
            return WAITING_FOR_STYLE
        context.user_data["pending_style_choice"] = 11
        context.user_data["pending_debug_mode"]   = True
        await update.message.reply_text(
            "🔞🐛 <b>Adult Debug mode selected.</b>\n\n"
            "All prompts, Groq responses, and NAI payloads will be captured "
            "and sent to you as a report at the end of the game.\n\n"
            "⚠️ You must be 18 or older to proceed.\n\n"
            "Are you 18+? Reply <b>YES</b> to confirm or <b>NO</b> to go back.",
            parse_mode="HTML",
        )
        return WAITING_FOR_AGE_CONFIRM

    # Secret passphrase triggers adult mode — not shown in the menu
    if text.lower() == "hellokitty":
        if not NOVELAI_API_KEY:
            await update.message.reply_text(
                "⚠️ Adult mode is not configured on this server. "
                "Please choose a style from 1–10."
            )
            return WAITING_FOR_STYLE
        context.user_data["pending_style_choice"] = 11
        context.user_data["pending_debug_mode"]   = False
        await update.message.reply_text(
            "🔞 <b>Adult mode selected.</b>\n\n"
            "This mode generates explicit content using an external AI model.\n\n"
            "⚠️ You must be 18 or older to proceed.\n\n"
            "Are you 18+? Reply <b>YES</b> to confirm or <b>NO</b> to go back and choose a different style.",
            parse_mode="HTML",
        )
        return WAITING_FOR_AGE_CONFIRM

    if not text.isdigit() or not (1 <= int(text) <= 10):
        await update.message.reply_text("❌ Please reply with a number between 1 and 10.")
        return WAITING_FOR_STYLE

    # Standard style
    context.user_data["pending_style_choice"] = int(text)
    context.user_data["pending_debug_mode"]   = False
    return await _finalise_style(update, context)


async def receive_age_confirm_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle host age confirmation for adult mode."""
    answer = update.message.text.strip().upper()

    if answer in ("YES", "Y", "ДА", "ДА"):
        # Confirmed — proceed with adult style
        return await _finalise_style(update, context)
    else:
        # Declined — show safe style menu without option 11
        context.user_data.pop("pending_style_choice", None)
        await update.message.reply_text(
            "No problem! Please choose a style from the list below.",
        )
        await update.message.reply_text(build_style_menu(include_adult=False), parse_mode="HTML")
        return WAITING_FOR_STYLE


async def _finalise_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create the game after style and (if needed) age confirmation."""
    choice   = context.user_data.get("pending_style_choice", 1)
    n        = context.user_data["pending_num_players"]
    is_adult = (choice == 11)
    is_debug = context.user_data.get("pending_debug_mode", False) and is_adult

    if is_adult:
        style_name   = ADULT_STYLE[0]
        style_prompt = ADULT_STYLE_PROMPT_SUFFIX
    else:
        style_name, style_prompt = STYLES[choice - 1]

    # Solo mode
    if n == 1:
        chat_id = update.effective_chat.id
        token = generate_token()
        while token in games:
            token = generate_token()

        games[token] = {
            "token": token,
            "num_players": 1,
            "answers": {},
            "roles": {},
            "player_order": [],
            "player_names": {},
            "created_at": datetime.utcnow(),
            "finished": False,
            "style_prompt": style_prompt,
            "style_name": style_name,
            "original_phrase": None,
            "solo_answers": [],
            "adult_mode": is_adult,
            "debug_mode":  is_debug,
            "debug_log":   [] if is_debug else None,
        }

        _join_game(games[token], token, chat_id, update.effective_user, context)

        log_game_event("started", {
            "token": token,
            "mode": "solo",
            "num_players": 1,
            "style_name": style_name,
            "adult_mode": is_adult,
        })

        await update.message.reply_text(
            f"🎮 <b>Solo game started!</b>\n"
            f"🎨 Style: <b>{style_name}</b>\n\n"
            f"You'll answer all 5 questions yourself. Let's go!\n\n"
            f"{ROLE_QUESTIONS[0]}",
            parse_mode="HTML",
        )
        return WAITING_FOR_SOLO_ANSWER

    # Multiplayer
    token = generate_token()
    while token in games:
        token = generate_token()

    games[token] = {
        "token": token,
        "num_players": n,
        "answers": {},
        "roles": {},
        "player_order": [],
        "player_names": {},
        "created_at": datetime.utcnow(),
        "finished": False,
        "style_prompt": style_prompt,
        "style_name": style_name,
        "original_phrase": None,
        "adult_mode": is_adult,
        "debug_mode":  is_debug,
        "debug_log":   [] if is_debug else None,
    }

    log_game_event("started", {
        "token": token,
        "mode": "multiplayer",
        "num_players": n,
        "style_name": style_name,
        "adult_mode": is_adult,
    })

    bot_username = context.bot.username
    deep_link    = f"https://t.me/{bot_username}?start={token}"
    short_link   = await _shorten_url(deep_link)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text="📤 Share game invite",
            url=f"https://t.me/share/url?text=Let%27s%20make%20a%20comic%20together%21%20Join%20my%20Skazk.AI%20game%20%E2%80%94%20takes%20just%20minutes%2C%20guaranteed%20to%20be%20ridiculous&url={short_link}",
        )],
        [InlineKeyboardButton(
            text="▶️ Join this game yourself",
            url=short_link,
        )],
    ])

    adult_note = "\n🔞 <b>This is an adult game. All players must confirm they are 18+ before joining.</b>" if is_adult else ""

    await update.message.reply_text(
        f"✅ Game created!\n\n"
        f"🔑 Token: <code>{token}</code>\n"
        f"👥 Players needed: {n}\n"
        f"🎨 Style: <b>{style_name}</b>"
        f"{adult_note}\n\n"
        f"Tap <b>Share game invite</b> to send the join link to other players.\n\n"
        f"⏳ This game expires in {GAME_TIMEOUT_MINUTES} minutes.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /start deep-link join  &  /play join
# ---------------------------------------------------------------------------

def _join_game(game, token, chat_id, user, context):
    """Shared logic for joining a game. Returns role_index."""
    role_index = len(game["player_order"])
    game["player_order"].append(chat_id)
    game["roles"][chat_id] = role_index
    game["player_names"][chat_id] = display_name_for(user)
    context.user_data["current_token"] = token
    return role_index


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "👋 Welcome to Skazk.AI!\n\nUse /create 1 to play solo, or /create N (2–5) to play with friends."
        )
        return

    token = args[0].upper()
    chat_id = update.effective_chat.id

    if token not in games:
        await update.message.reply_text("❌ Game not found. It may have expired. Ask the host to create a new one.")
        return

    game = games[token]

    if is_game_expired(game):
        del games[token]
        await update.message.reply_text("⏰ This game has expired. Ask the host to create a new one.")
        return

    if game["finished"]:
        await update.message.reply_text("🏁 This game is already complete.")
        return

    if chat_id in game["player_order"]:
        await update.message.reply_text("⚠️ You've already joined this game!")
        return

    if len(game["player_order"]) >= game["num_players"]:
        await update.message.reply_text("🚫 Game is full. No more players can join.")
        return

    # Adult game — ask player to confirm age before joining
    if game.get("adult_mode"):
        context.user_data["pending_adult_token"] = token
        await update.message.reply_text(
            f"🔞 <b>This is an adult game.</b>\n\n"
            f"Explicit content will be generated. You must be 18 or older to participate.\n\n"
            f"Are you 18+? Reply <b>YES</b> to join or <b>NO</b> to decline.",
            parse_mode="HTML",
        )
        return WAITING_FOR_PLAYER_AGE

    role_index = _join_game(game, token, chat_id, update.effective_user, context)

    await update.message.reply_text(
        f"🎮 You joined game <code>{token}</code>!\n\n"
        f"Your role: <b>{ROLES[role_index]}</b>\n\n"
        f"{ROLE_QUESTIONS[role_index]}",
        parse_mode="HTML",
    )
    return WAITING_FOR_ANSWER


async def receive_player_age_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Age confirmation for players joining an adult game."""
    answer  = update.message.text.strip().upper()
    token   = context.user_data.get("pending_adult_token")
    chat_id = update.effective_chat.id

    if not token or token not in games:
        await update.message.reply_text("❌ Game not found. Ask the host to create a new one.")
        return ConversationHandler.END

    if answer in ("YES", "Y", "ДА"):
        game = games[token]

        if len(game["player_order"]) >= game["num_players"]:
            await update.message.reply_text("🚫 Sorry, the game filled up while you were confirming.")
            context.user_data.pop("pending_adult_token", None)
            return ConversationHandler.END

        role_index = _join_game(game, token, chat_id, update.effective_user, context)
        context.user_data.pop("pending_adult_token", None)
        await update.message.reply_text(
            f"✅ Age confirmed. Welcome!\n\n"
            f"🎮 You joined game <code>{token}</code>!\n\n"
            f"Your role: <b>{ROLES[role_index]}</b>\n\n"
            f"{ROLE_QUESTIONS[role_index]}",
            parse_mode="HTML",
        )
        return WAITING_FOR_ANSWER
    else:
        context.user_data.pop("pending_adult_token", None)
        await update.message.reply_text(
            "🚫 Sorry, you must be 18+ to join this game. "
            "Ask the host to create a regular game for you."
        )
        return ConversationHandler.END


async def play_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This bot only works in private chats.")
        return ConversationHandler.END

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /play TOKEN")
        return ConversationHandler.END

    token = args[0].upper()
    chat_id = update.effective_chat.id

    if token not in games:
        await update.message.reply_text("❌ Game not found. Check your token and try again.")
        return ConversationHandler.END

    game = games[token]

    if is_game_expired(game):
        del games[token]
        await update.message.reply_text("⏰ This game has expired. Please create a new one with /create.")
        return ConversationHandler.END

    if game["finished"]:
        await update.message.reply_text("🏁 This game is already complete.")
        return ConversationHandler.END

    if chat_id in game["player_order"]:
        await update.message.reply_text("⚠️ You've already joined this game!")
        return ConversationHandler.END

    if len(game["player_order"]) >= game["num_players"]:
        await update.message.reply_text("🚫 Game is full. No more players can join.")
        return ConversationHandler.END

    role_index = _join_game(game, token, chat_id, update.effective_user, context)

    await update.message.reply_text(
        f"🎮 You joined game <code>{token}</code>!\n\n"
        f"Your role: <b>{ROLES[role_index]}</b>\n\n"
        f"{ROLE_QUESTIONS[role_index]}",
        parse_mode="HTML",
    )
    return WAITING_FOR_ANSWER


# ---------------------------------------------------------------------------
# Answer collection
# ---------------------------------------------------------------------------

async def receive_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    answer = update.message.text.strip()
    token = context.user_data.get("current_token")

    if not token or token not in games:
        await update.message.reply_text("❌ Something went wrong. Please join a game with /play TOKEN.")
        return ConversationHandler.END

    game = games[token]

    if is_game_expired(game):
        del games[token]
        await update.message.reply_text("⏰ This game has expired.")
        return ConversationHandler.END

    if chat_id not in game["roles"]:
        await update.message.reply_text("❌ You are not part of this game.")
        return ConversationHandler.END

    if chat_id in game["answers"]:
        await update.message.reply_text("✅ You already submitted your answer!")
        return ConversationHandler.END

    game["answers"][chat_id] = answer
    await update.message.reply_text("✅ Answer received! Waiting for other players...")

    if len(game["answers"]) == game["num_players"]:
        try:
            await finalize_game(context, token)
        except Exception as e:
            logger.exception(f"finalize_game failed for {token}: {e}")
            for pid in game.get("player_order", [chat_id]):
                try:
                    await context.bot.send_message(
                        chat_id=pid,
                        text=(
                            "⚠️ <b>Something went wrong generating the image.</b>\n\n"
                            "This is usually a temporary network issue. "
                            "Please start a new game with /create."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    return ConversationHandler.END


async def receive_solo_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the sequential Q&A for solo games."""
    chat_id = update.effective_chat.id
    answer  = update.message.text.strip()
    token   = context.user_data.get("current_token")

    if not token or token not in games:
        await update.message.reply_text("❌ Something went wrong. Use /create to start a new game.")
        return ConversationHandler.END

    game = games[token]

    if is_game_expired(game):
        del games[token]
        await update.message.reply_text("⏰ This game has expired.")
        return ConversationHandler.END

    game["solo_answers"].append(answer)
    role_index = len(game["solo_answers"])  # next question index

    if role_index < len(ROLE_QUESTIONS):
        # More questions to ask
        await update.message.reply_text(ROLE_QUESTIONS[role_index])
        return WAITING_FOR_SOLO_ANSWER
    else:
        # All 5 answered — build the answers dict in the expected format
        # and finalize as if all players submitted
        for i, ans in enumerate(game["solo_answers"]):
            game["answers"][chat_id] = ans  # placeholder — finalize reads solo_answers directly
        await update.message.reply_text("✅ All answers in! Generating your image… 🎨")
        try:
            await finalize_game(context, token)
        except Exception as e:
            logger.exception(f"finalize_game failed for solo {token}: {e}")
            try:
                await update.message.reply_text(
                    "⚠️ <b>Something went wrong generating the image.</b>\n\n"
                    "This is usually a temporary network issue. "
                    "Please start a new game with /create.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return ConversationHandler.END


# ---------------------------------------------------------------------------
# Finalize regular game → generate image → offer comic mode
# ---------------------------------------------------------------------------

async def finalize_game(context: ContextTypes.DEFAULT_TYPE, token: str):
    game = games[token]
    game["finished"] = True

    log_game_event("completed", {
        "token": token,
        "mode": "solo" if game.get("num_players") == 1 else "multiplayer",
        "num_players": game.get("num_players", 1),
        "style_name": game.get("style_name", ""),
        "adult_mode": game.get("adult_mode", False),
        "started_at": game["created_at"].isoformat() if isinstance(game.get("created_at"), datetime) else str(game.get("created_at")),
        "actual_players": len(game.get("player_order", [])),
    })

    ordered_answers = (
        game["solo_answers"]
        if game.get("num_players") == 1
        else [game["answers"][cid] for cid in game["player_order"]]
    )

    who    = ordered_answers[0]
    action = ordered_answers[1] if len(ordered_answers) > 1 else "doing something"
    where  = ordered_answers[2] if len(ordered_answers) > 2 else None
    mood   = ordered_answers[3] if len(ordered_answers) > 3 else None
    twist  = ordered_answers[4] if len(ordered_answers) > 4 else None

    phrase = f"{who} is {action}"
    if where:
        phrase += f" in {where}"
    if mood:
        phrase += f", with a {mood} atmosphere"
    if twist:
        phrase += f", but {twist}"

    # Store for comic continuity
    game["original_phrase"] = phrase

    # Fix 5: Log player answers FIRST, before any API calls
    _dbg(game.get("debug_log"), "PLAYER_ANSWERS",
         f"who='{who}' | action='{action}' | where='{where}' | "
         f"mood='{mood}' | twist='{twist}'\nphrase='{phrase}'")

    mood_part  = f" Mood: {mood}."  if mood  else ""
    twist_part = f" Unexpected twist: {twist}." if twist else ""
    style_prompt = game.get("style_prompt", STYLES[0][1])
    style_name   = game.get("style_name",   STYLES[0][0])

    # Generate the character bible NOW so the initial image and all comic
    # panels share the exact same character description from the very start.
    character_bible = await _generate_character_bible(phrase, style_name)
    game["character_bible"] = character_bible
    if character_bible:
        logger.info(f"Character bible for {token}: {character_bible}")

    # For adult mode: generate full cast bible and fix a seed
    is_adult = game.get("adult_mode", False)
    if is_adult:
        nai_cast = await _generate_cast_bible_nai(phrase, who_answer=who,
                                                   debug_log=game.get("debug_log"))
        game["nai_cast"] = nai_cast
        game["nai_tags"] = nai_cast[0]["tags"] if nai_cast else ""  # keep for legacy compat
        game["nai_seed"] = random.randint(0, 2**32 - 1)
        logger.info(f"Cast bible for {token}: {[c['name'] for c in nai_cast]}")
        # Origin image: parse all characters from the premise
        structured   = await _scene_to_nai_structured(phrase, nai_cast, debug_log=game.get("debug_log"))
        quality_tags = "masterpiece, best quality, highly detailed, explicit"
        char_caps    = structured["char_captions"]
        image_prompt = {
            "input":        f"{_char_count_prefix(char_caps)}{quality_tags}, {structured['scene_tags']}",
            "char_captions": char_caps,
        }
    else:
        game["nai_cast"] = []
        game["nai_tags"] = ""
        game["nai_seed"] = None
        bible_prefix = f"{character_bible}. " if character_bible else ""
        image_prompt = f"{bible_prefix}{phrase}.{mood_part}{twist_part} {style_prompt}."

    fallback_prompt = (
        f"A single illustration of: {phrase}. "
        f"Visual style: {style_prompt}. "
        f"One image only, no text."
    )
    image_data, error_msg = await _generate_image(
        image_prompt,
        label=f"game {token} [{style_name}]",
        fallback_prompt=fallback_prompt,
        adult_mode=is_adult,
        nai_seed=game.get("nai_seed"),
        debug_log=game.get("debug_log"),
    )

    # Keep the initial image so it can be included as panel 0 in the comic book
    game["initial_image_data"] = image_data

    names      = game.get("player_names", {})
    player_ids = game["player_order"]

    def name(index):
        if index < len(player_ids):
            return f" ({names.get(player_ids[index], 'Player')})"
        return ""

    result_text = (
        f"🎬 <b>Your story begins!</b>\n\n"
        f"🎨 Style: <b>{style_name}</b>\n\n"
        f"📖 <b>The story:</b>\n"
        f"<i>{phrase}</i>\n\n"
        f"🎭 WHO: {who}{name(0)}\n"
        f"🎬 ACTION: {action}{name(1)}\n"
        f"📍 WHERE: {where}{name(2)}"
        + (f"\n🌫️ MOOD: {mood}{name(3)}"   if mood  else "")
        + (f"\n🌀 TWIST: {twist}{name(4)}"  if twist else "")
    )

    for chat_id in player_ids:
        try:
            if image_data:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_data,
                    caption=result_text,
                    parse_mode="HTML",
                )
            else:
                safe_error = (error_msg or "Unknown error.")
                safe_error = safe_error.replace("<", "‹").replace(">", "›")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        result_text
                        + "\n\n❌ <b>Image generation failed — the game cannot continue.</b>\n"
                        + safe_error
                        + "\n\nPlease start a new game with /create."
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to send result to {chat_id}: {e}")

    # If image failed, terminate here — no comic mode without an origin image
    if not image_data:
        games.pop(token, None)
        return

    # Offer comic mode to the HOST only
    host_id = player_ids[0]
    comic_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Continue as Comic Book!", callback_data=f"start_comic:{token}"),
    ]])
    await context.bot.send_message(
        chat_id=host_id,
        text=(
            "✨ <b>Want to keep going?</b>\n\n"
            "Start a comic book based on this story — each player writes a scene "
            "and the bot generates a panel. At the end everyone gets the full comic!"
        ),
        parse_mode="HTML",
        reply_markup=comic_keyboard,
    )


# ---------------------------------------------------------------------------
# Comic mode — start & round selection
# ---------------------------------------------------------------------------

async def start_comic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]

    game = games.get(token)
    if not game:
        await query.edit_message_text("❌ Game not found or expired.")
        return

    if token in comic_sessions:
        await query.edit_message_text("🎬 A comic book for this game has already started!")
        return

    n = len(game["player_order"])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("1 round",  callback_data=f"comic_rounds:{token}:1"),
        InlineKeyboardButton("2 rounds", callback_data=f"comic_rounds:{token}:2"),
        InlineKeyboardButton("3 rounds", callback_data=f"comic_rounds:{token}:3"),
    ]])
    await query.edit_message_text(
        f"🎬 <b>Comic Book Mode</b>\n\n"
        f"How many rounds should the comic run?\n"
        f"({n} players, {n} panels per round)",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def set_comic_rounds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, token, rounds_str = query.data.split(":")
    rounds = int(rounds_str)

    game = games.get(token)
    if not game:
        await query.edit_message_text("❌ Game not found.")
        return

    if token in comic_sessions:
        await query.edit_message_text("🎬 Already started!")
        return

    num_players = len(game["player_order"])
    num_panels  = num_players * rounds

    comic_sessions[token] = {
        "style_prompt":       game["style_prompt"],
        "style_name":         game["style_name"],
        "original_phrase":    game.get("original_phrase", ""),
        "player_order":       list(game["player_order"]),
        "player_names":       dict(game["player_names"]),
        "num_players":        num_players,
        "current_turn_index": 0,
        "panels":             [],
        "num_panels":         num_panels,
        "rounds":             rounds,
        "created_at":         datetime.utcnow(),
        "compiling":          False,
        "adult_mode":         game.get("adult_mode", False),
    }

    await query.edit_message_text(
        f"🎬 <b>Comic Book started!</b>\n"
        f"{rounds} round(s) · {num_panels} panels total.\n\n"
        f"Players will be notified when it's their turn.",
        parse_mode="HTML",
    )

    host_id = query.from_user.id

    # Reuse the character bible already generated during finalize_game
    character_bible = game.get("character_bible", "")
    comic_sessions[token]["character_bible"] = character_bible
    if character_bible:
        logger.info(f"Reusing character bible for comic {token}: {character_bible}")

    # For adult mode: pass full cast bible and seed into comic session
    comic_sessions[token]["nai_cast"]   = list(game.get("nai_cast", []))
    comic_sessions[token]["nai_tags"]   = game.get("nai_tags", "")
    comic_sessions[token]["nai_seed"]   = game.get("nai_seed",
                                            random.randint(0, 2**32 - 1))
    comic_sessions[token]["debug_mode"] = game.get("debug_mode", False)
    comic_sessions[token]["debug_log"]  = game.get("debug_log")  # None or list

    # Carry the initial image forward so compile_comic can include it
    comic_sessions[token]["initial_image_data"] = game.get("initial_image_data")

    # Per-character vibe reference map: char_tags → first image they appeared in
    # Pre-populate main character with origin image
    initial_img  = game.get("initial_image_data")
    nai_cast     = game.get("nai_cast", [])
    char_first_panel = {}
    if initial_img and nai_cast:
        main_tags = nai_cast[0]["tags"]
        char_first_panel[main_tags] = initial_img
    comic_sessions[token]["char_first_panel"] = char_first_panel

    # Notify non-host players
    for chat_id in game["player_order"]:
        if chat_id != host_id:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎬 <b>Comic Book Mode has started!</b>\n\n"
                        f"{rounds} round(s) · {num_panels} panels.\n"
                        f"You'll be pinged when it's your turn. Stay close! 🖊️"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to notify player {chat_id}: {e}")

    await notify_next_comic_player(context, token)


# ---------------------------------------------------------------------------
# Comic mode — turn management
# ---------------------------------------------------------------------------

async def notify_next_comic_player(context: ContextTypes.DEFAULT_TYPE, token: str):
    comic = comic_sessions.get(token)
    if not comic:
        return

    idx          = comic["current_turn_index"]
    player_order = comic["player_order"]
    player_id    = player_order[idx % len(player_order)]
    panel_num    = idx + 1

    # Register player as awaiting input
    pending_comic_input[player_id] = token

    # Build story-so-far summary
    story_lines = [f"<i>{comic['original_phrase']}</i>"]
    for i, panel in enumerate(comic["panels"]):
        if not panel.get("skipped"):
            author = comic["player_names"].get(panel["author_id"], "Someone")
            story_lines.append(f"  Panel {i+1} ({author}): {panel['prompt']}")

    story_text = "\n".join(story_lines)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭️ Skip my turn", callback_data=f"skip_scene:{token}"),
    ]])

    await context.bot.send_message(
        chat_id=player_id,
        text=(
            f"🎬 <b>Your turn! Panel {panel_num} of {comic['num_panels']}</b>\n\n"
            f"📖 <b>Story so far:</b>\n{story_text}\n\n"
            f"✍️ Describe what happens in the next scene:\n"
            f"<i>(or tap Skip if you're stuck)</i>"
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_comic_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches free-text from players whose turn it is in a comic session."""
    chat_id = update.effective_chat.id

    if chat_id not in pending_comic_input:
        return  # Not this player's comic turn — let other handlers deal with it

    token      = pending_comic_input.pop(chat_id)
    comic      = comic_sessions.get(token)
    if not comic:
        return

    scene_text = update.message.text.strip()
    panel_num  = comic["current_turn_index"] + 1
    is_retry   = comic.get("retry_player") == chat_id

    try:
        await _handle_comic_message_inner(
            update, context, chat_id, token, comic, scene_text, panel_num, is_retry
        )
    except Exception as e:
        logger.exception(f"Unhandled exception in handle_comic_message panel {panel_num} [{token}]: {e}")
        # Ensure the game is never permanently frozen — force a skip and advance
        comic.pop("retry_player", None)
        comic["panels"].append({
            "author_id":  chat_id,
            "prompt":     scene_text,
            "image_data": None,
            "skipped":    True,
        })
        author_name = comic["player_names"].get(chat_id, "A player")
        for pid in comic.get("player_order", []):
            try:
                await context.bot.send_message(
                    chat_id=pid,
                    text=f"⚠️ Panel {panel_num} skipped due to an unexpected error. Continuing the game.",
                )
            except Exception:
                pass
        await _advance_comic(context, token)


async def _handle_comic_message_inner(
    update, context, chat_id, token, comic, scene_text, panel_num, is_retry
):
    """Core logic for processing a comic panel submission."""
    await update.message.reply_text(f"✅ Got it! Generating panel {panel_num}… 🎨")

    # Generate panel image — use NAI-optimised prompt and fixed seed for adult mode
    is_adult = comic.get("adult_mode", False)
    if is_adult:
        prompt = await _build_panel_prompt_adult(comic, scene_text)

        # Build per-character vibe references from the char_first_panel map.
        # Skip panels flagged as vibe_unreliable (generated via simplified fallback).
        char_first_panel = comic.get("char_first_panel", {})
        present_tags     = prompt.get("present_chars", [])

        nai_refs = []
        seen     = set()

        # Main character (origin image) always first and always included
        origin = comic.get("initial_image_data")
        if origin:
            nai_refs.append(origin)
            seen.add(id(origin))

        # Additional characters in scene — their first reliable appearance image
        for tag_str in present_tags:
            ref = char_first_panel.get(tag_str)
            if ref and id(ref) not in seen and len(nai_refs) < 4:
                nai_refs.append(ref)
                seen.add(id(ref))

        # Seed: always use origin seed
        panel_seed = comic.get("nai_seed")

    else:
        prompt     = _build_panel_prompt(comic, scene_text, panel_num)
        nai_refs   = None
        panel_seed = None

    _dbg(comic.get("debug_log"), f"PANEL_{panel_num}_PLAYER_INPUT", scene_text)

    fallback = _build_panel_fallback_prompt(comic, scene_text)
    image_data, image_error = await _generate_image(
        prompt,
        label=f"panel {panel_num} of game {token}",
        fallback_prompt=fallback,
        adult_mode=is_adult,
        nai_seed=panel_seed,
        nai_references=nai_refs if is_adult else None,
        debug_log=comic.get("debug_log"),
    )

    # Any failure — offer one retry, then auto-skip
    if image_data is None:
        is_content_policy = image_error and "content policy" in image_error.lower()
        if not is_retry:
            # First failure — give them one more chance
            comic["retry_player"] = chat_id
            pending_comic_input[chat_id] = token
            if is_content_policy:
                retry_msg = (
                    f"🚫 <b>Content policy violation.</b>\n\n"
                    f"That scene was flagged as unsafe. Please try a tamer description.\n\n"
                    f"⚠️ <i>If your next prompt is blocked again, your turn will be skipped automatically.</i>"
                )
            else:
                # Sanitize error text — strip angle brackets so HTML parse_mode
                # doesn't choke on raw Python object reprs like <_io.BytesIO ...>
                safe_error = (image_error or "Unknown error.")
                safe_error = safe_error.replace("<", "‹").replace(">", "›")
                retry_msg = (
                    f"⚠️ <b>Image generation failed.</b>\n"
                    f"{safe_error}\n\n"
                    f"Please try again with a different description.\n"
                    f"<i>If it fails again, your turn will be skipped automatically.</i>"
                )
            try:
                await update.message.reply_text(retry_msg, parse_mode="HTML")
            except Exception as tg_err:
                # If sending the retry message itself fails, don't freeze the game —
                # fall through to auto-skip so subsequent panels can proceed.
                logger.error(f"Failed to send retry message to {chat_id}: {tg_err}")
                comic.pop("retry_player", None)
                pending_comic_input.pop(chat_id, None)
                author_name = comic["player_names"].get(chat_id, "A player")
                for pid in comic["player_order"]:
                    try:
                        await context.bot.send_message(
                            chat_id=pid,
                            text=f"⏭️ <b>Panel {panel_num} skipped.</b> {author_name}'s panel had an error.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                comic["panels"].append({
                    "author_id":  chat_id,
                    "prompt":     scene_text,
                    "image_data": None,
                    "skipped":    True,
                })
                await _advance_comic(context, token)
            return
        else:
            # Second failure — auto-skip
            comic.pop("retry_player", None)
            author_name = comic["player_names"].get(chat_id, "A player")
            skip_reason = "blocked by content policy twice" if is_content_policy else "image generation failed twice"
            for pid in comic["player_order"]:
                try:
                    await context.bot.send_message(
                        chat_id=pid,
                        text=f"⏭️ <b>Panel {panel_num} skipped.</b> {author_name}'s panel was {skip_reason}.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            comic["panels"].append({
                "author_id":  chat_id,
                "prompt":     scene_text,
                "image_data": None,
                "skipped":    True,
            })
            await _advance_comic(context, token)
            return

    # Clear retry flag on success (or non-policy failure)
    comic.pop("retry_player", None)

    # Detect if simplified fallback was used (char_captions stripped)
    vibe_unreliable = (image_error == "VIBE_UNRELIABLE")

    # Store panel — include present_chars (character names) for next panel's pronoun resolution
    present_char_names = []
    if isinstance(prompt, dict):
        # Map tag strings back to cast names for readable context
        cast_tags_to_name = {c["tags"]: c["name"] for c in comic.get("nai_cast", [])}
        present_char_names = [
            cast_tags_to_name.get(tag_str, tag_str[:30])
            for tag_str in prompt.get("present_chars", [])
        ]

    comic["panels"].append({
        "author_id":      chat_id,
        "prompt":         scene_text,
        "image_data":     image_data,
        "skipped":        False,
        "nai_seed":       panel_seed,
        "vibe_unreliable": vibe_unreliable,
        "present_chars":  present_char_names,
    })

    # Update char_first_panel map — skip unreliable panels since they lack char_captions
    # and would anchor inconsistent appearance for future panels.
    if is_adult and image_data and isinstance(prompt, dict) and not vibe_unreliable:
        char_first_panel = comic.setdefault("char_first_panel", {})
        for tag_str in prompt.get("present_chars", []):
            if tag_str and tag_str not in char_first_panel:
                char_first_panel[tag_str] = image_data
                logger.info(f"Registered first-appearance image for character: {tag_str[:60]}")

    # Broadcast panel to all players
    author_name   = comic["player_names"].get(chat_id, "A player")
    panel_caption = (
        f"🎬 <b>Panel {panel_num} of {comic['num_panels']}</b> — by {author_name}\n"
        f"<i>{scene_text}</i>"
    )

    for pid in comic["player_order"]:
        try:
            if image_data:
                await context.bot.send_photo(
                    chat_id=pid, photo=image_data,
                    caption=panel_caption, parse_mode="HTML",
                )
            else:
                safe_err = (image_error or "Unknown error.").replace("<", "‹").replace(">", "›")
                await context.bot.send_message(
                    chat_id=pid,
                    text=(
                        panel_caption
                        + "\n\n⚠️ <b>Image generation failed for this panel.</b>\n"
                        + safe_err
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to broadcast panel {panel_num} to {pid}: {e}")

    await _advance_comic(context, token)


async def skip_comic_scene_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Turn skipped ⏭️")

    token   = query.data.split(":", 1)[1]
    chat_id = query.from_user.id

    pending_comic_input.pop(chat_id, None)

    comic = comic_sessions.get(token)
    if not comic:
        return

    panel_num   = comic["current_turn_index"] + 1
    author_name = comic["player_names"].get(chat_id, "A player")

    comic["panels"].append({
        "author_id":  chat_id,
        "prompt":     "[skipped]",
        "image_data": None,
        "skipped":    True,
    })

    for pid in comic["player_order"]:
        try:
            await context.bot.send_message(
                chat_id=pid,
                text=f"⏭️ {author_name} skipped Panel {panel_num}.",
            )
        except Exception:
            pass

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _advance_comic(context, token)


async def _advance_comic(context: ContextTypes.DEFAULT_TYPE, token: str):
    """Increment turn counter and either prompt next player or compile the comic."""
    comic = comic_sessions.get(token)
    if not comic:
        return

    comic["current_turn_index"] += 1

    if comic["current_turn_index"] >= comic["num_panels"]:
        if not comic["compiling"]:
            comic["compiling"] = True
            try:
                await compile_comic(context, token)
            except Exception as e:
                logger.exception(f"compile_comic crashed for {token}: {e}")
                for pid in comic.get("player_order", []):
                    try:
                        await context.bot.send_message(
                            chat_id=pid,
                            text=(
                                "⚠️ Something went wrong while compiling the comic. "
                                f"Error: {e}\n\nStart a new game with /create."
                            ),
                        )
                    except Exception:
                        pass
                comic_sessions.pop(token, None)
    else:
        await notify_next_comic_player(context, token)


# ---------------------------------------------------------------------------
# Comic compilation
# ---------------------------------------------------------------------------

async def compile_comic(context: ContextTypes.DEFAULT_TYPE, token: str):
    comic = comic_sessions.get(token)
    if not comic:
        return

    player_order = comic["player_order"]
    player_names = comic["player_names"]

    # Notify all players
    for pid in player_order:
        try:
            await context.bot.send_message(
                chat_id=pid,
                text="📖 <b>All panels done! Putting together your comic book…</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send compile notice to {pid}: {e}")

    # Build the full panel list: initial image first, then all comic panels
    real_panels = []
    initial = comic.get("initial_image_data")
    if initial:
        real_panels.append({
            "image_data": initial,
            "label": "🎬 Origin",
            "caption": f"Panel 0 — Origin\n<i>{comic['original_phrase']}</i>",
        })
    for i, p in enumerate(comic["panels"]):
        if p.get("image_data"):
            author = player_names.get(p["author_id"], "Player")
            real_panels.append({
                "image_data": p["image_data"],
                "label": f"Panel {i + 1}",
                "caption": f"Panel {i + 1} — {author}\n<i>{p['prompt']}</i>",
            })

    if not real_panels:
        for pid in player_order:
            try:
                await context.bot.send_message(
                    chat_id=pid,
                    text="⚠️ No panels were generated successfully — all images failed.",
                )
            except Exception:
                pass
        comic_sessions.pop(token, None)
        return

    # --- 1. Send the composite strip image ---
    strip_bytes = _build_comic_strip(
        initial_image_data=comic.get("initial_image_data"),
        panels=comic["panels"],
        player_names=player_names,
        original_phrase=comic["original_phrase"],
        style_name=comic["style_name"],
        num_players=comic["num_players"],
    )

    for pid in player_order:
        try:
            if strip_bytes:
                await context.bot.send_photo(
                    chat_id=pid,
                    photo=strip_bytes,
                    caption=(
                        f"📖 <b>Your Skazk.AI comic — {comic['style_name']}</b>\n"
                        f"<i>{comic['original_phrase']}</i>"
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to send strip to {pid}: {e}")

    # --- 2. Send individual panel album (full resolution) ---
    for chunk_start in range(0, len(real_panels), 10):
        chunk = real_panels[chunk_start:chunk_start + 10]
        media_group = [
            InputMediaPhoto(
                media=panel["image_data"],
                caption=panel["caption"],
                parse_mode="HTML",
            )
            for panel in chunk
        ]
        for pid in player_order:
            try:
                await context.bot.send_media_group(chat_id=pid, media=media_group)
            except Exception as e:
                logger.error(f"Failed to send album chunk to {pid}: {e}")

    # --- 3. Full script ---
    script_lines = [
        "📜 <b>Full Comic Script</b>\n",
        f"<b>Origin</b>\n<i>{comic['original_phrase']}</i>",
    ]
    for i, p in enumerate(comic["panels"]):
        author = player_names.get(p["author_id"], "Player")
        if p.get("skipped"):
            script_lines.append(f"\n<b>Panel {i + 1}</b> — {author}\n<i>[skipped]</i>")
        else:
            script_lines.append(f"\n<b>Panel {i + 1}</b> — {author}\n<i>{p['prompt']}</i>")
    script_text = "\n".join(script_lines)

    # --- 4. Credits & closing ---
    credits = "\n".join(f"• {player_names[pid]}" for pid in player_order)
    for pid in player_order:
        try:
            await context.bot.send_message(
                chat_id=pid,
                text=script_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send script to {pid}: {e}")
        try:
            await context.bot.send_message(
                chat_id=pid,
                text=(
                    f"🎉 <b>The End!</b>\n\n"
                    f"🎨 Style: {comic['style_name']}\n"
                    f"📖 {len(real_panels)} panels · {comic['rounds']} round(s)\n\n"
                    f"<b>Created by:</b>\n{credits}\n\n"
                    f"Thanks for playing <b>Skazk.AI</b>! 🚀\n"
                    f"Start a new game anytime with /create"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send credits to {pid}: {e}")

    # --- Debug report (debug mode only) ---
    debug_log = comic.get("debug_log")
    if debug_log:
        host_id = player_order[0]
        try:
            report_lines = [
                f"🐛 DEBUG REPORT — Game {token}",
                f"Mode: Adult Debug | Style: {comic['style_name']}",
                f"Players: {', '.join(player_names.values())}",
                f"Rounds: {comic['rounds']} | Panels: {comic['num_panels']}",
                "=" * 60,
            ]
            for entry in debug_log:
                report_lines.append(f"\n[{entry['ts']}] {entry['step']}")
                report_lines.append(entry['data'])
                report_lines.append("-" * 40)
            report_text = "\n".join(report_lines)

            # Send as a .txt file
            report_bytes = report_text.encode("utf-8")
            from io import BytesIO
            await context.bot.send_document(
                chat_id=host_id,
                document=BytesIO(report_bytes),
                filename=f"debug_{token}.txt",
                caption=f"🐛 Debug report for game {token}",
            )
        except Exception as e:
            logger.error(f"Failed to send debug report: {e}")

    # Clean up session
    comic_sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Comic strip compositor
# ---------------------------------------------------------------------------

def _build_solo_strip(
    initial_image_data: bytes | None,
    panels: list[dict],
    original_phrase: str,
) -> bytes | None:
    """2×2 grid layout for solo games:
    ┌─────────┬─────────┐
    │ Origin  │ Panel 1 │
    │ caption │ caption │
    ├─────────┼─────────┤
    │ Panel 2 │ Panel 3 │
    │ caption │ caption │
    └─────────┴─────────┘
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    try:
        CELL      = 512
        PAD       = 8
        BG        = (18, 18, 26)
        CAPTION_BG= (32, 32, 46)
        BORDER    = (70, 55, 110)
        C_LABEL   = (180, 140, 255)
        C_TEXT    = (210, 210, 210)

        font_bold, _, font_small = _load_fonts()

        # Collect all 4 slots first so we can compute dynamic caption height
        real_panels = [p for p in panels if not p.get("skipped") and p.get("image_data")]
        slots = [
            {"image_data": initial_image_data, "prompt": original_phrase, "num": "1."},
        ] + [
            {"image_data": p["image_data"], "prompt": p["prompt"], "num": f"{i+2}."}
            for i, p in enumerate(real_panels[:3])
        ]

        # Dynamic caption height — fit the longest prompt across all slots
        chars_per_line = max(10, (CELL - 34) // 8)
        max_lines = max(
            len(textwrap.wrap(s["prompt"], width=chars_per_line)) or 1
            for s in slots
        )
        CAPTION_H = 20 + max_lines * STRIP_LINE_H + 8  # label row + text + padding

        canvas_w = 2 * CELL + 3 * PAD
        canvas_h = 2 * (CELL + CAPTION_H) + 3 * PAD
        canvas   = Image.new("RGB", (canvas_w, canvas_h), BG)
        draw     = ImageDraw.Draw(canvas)

        positions = [
            (PAD,        PAD),
            (PAD*2+CELL, PAD),
            (PAD,        PAD*2 + CELL + CAPTION_H),
            (PAD*2+CELL, PAD*2 + CELL + CAPTION_H),
        ]

        for i, (px, py) in enumerate(positions):
            slot = slots[i] if i < len(slots) else None

            # Image cell
            img_data = slot["image_data"] if slot else None
            if img_data:
                try:
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    img = img.resize((CELL, CELL), Image.LANCZOS)
                except Exception:
                    img = Image.new("RGB", (CELL, CELL), (50, 50, 60))
            else:
                img = Image.new("RGB", (CELL, CELL), (30, 30, 40))

            canvas.paste(img, (px, py))
            draw.rectangle(
                [px - 2, py - 2, px + CELL + 1, py + CELL + 1],
                outline=BORDER, width=2,
            )

            # Caption strip — all lines, no truncation
            cy = py + CELL
            draw.rectangle([px, cy, px + CELL, cy + CAPTION_H], fill=CAPTION_BG)
            if slot:
                draw.text((px + 8, cy + 5), slot["num"], font=font_bold, fill=C_LABEL)
                lines = textwrap.wrap(slot["prompt"], width=chars_per_line)
                for li, line in enumerate(lines):
                    draw.text((px + 26, cy + 5 + li * STRIP_LINE_H),
                              line, font=font_small, fill=C_TEXT)

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=92)
        logger.info(f"Solo strip built: {canvas_w}×{canvas_h}px")
        return buf.getvalue()

    except Exception as e:
        logger.error(f"_build_solo_strip failed: {e}", exc_info=True)
        return None


def _build_comic_strip(
    initial_image_data: bytes | None,
    panels: list[dict],
    player_names: dict,
    original_phrase: str,
    style_name: str,
    num_players: int,
) -> bytes | None:
    """
    Build a single composite image of the full comic.

    Layout:
      - Origin image spans the full canvas width at the top
      - Comic panels below in a grid where COLS = num_players
        so each row = one round of the game
      - Every cell has a caption strip with panel number, author, prompt

    Works for all player/round combinations:
      2p × 1r =  2 panels  → 1 row  of 2
      2p × 3r =  6 panels  → 3 rows of 2
      3p × 2r =  6 panels  → 2 rows of 3
      5p × 3r = 15 panels  → 3 rows of 5
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("Pillow not installed — cannot build comic strip. Run: pip install Pillow")
        return None

    try:
        # ── Solo mode: strict 2×2 grid (origin TL, panel1 TR, panel2 BL, panel3 BR) ──
        if num_players == 1:
            return _build_solo_strip(
                initial_image_data, panels, original_phrase
            )
        # ── Design constants ────────────────────────────────────────────────
        COLS        = num_players          # columns = players (each row = 1 round)
        PANEL_SIZE  = _panel_size(COLS)    # adaptive square panel size
        PAD         = 14                   # gap between cells and canvas edges

        # Colours
        BG          = (18, 18, 26)
        CAPTION_BG  = (32, 32, 46)
        BORDER      = (70, 55, 110)
        ORIGIN_BG   = (26, 22, 40)
        C_LABEL     = (180, 140, 255)      # purple — panel label
        C_AUTHOR    = (140, 200, 255)      # blue   — author name
        C_TEXT      = (210, 210, 210)      # light grey — prompt text
        C_ORIGIN_LBL= (255, 200, 80)       # gold   — "ORIGIN" label

        # ── Fonts ───────────────────────────────────────────────────────────
        font_bold, font_reg, font_small = _load_fonts()

        # ── Canvas width (needed before caption height calculation) ──────────
        canvas_w = COLS * PANEL_SIZE + (COLS + 1) * PAD
        ow       = canvas_w - 2 * PAD     # origin image width

        # ── Dynamic caption height — fit the longest prompt ──────────────────
        # Count lines needed for each prompt given the available width.
        def _lines_needed(text: str, available_w: int) -> int:
            chars = max(10, available_w // 8)
            return len(textwrap.wrap(text, width=chars)) or 1

        max_panel_lines  = max(
            (_lines_needed(p["prompt"], PANEL_SIZE - 34)
             for p in panels if not p.get("skipped") and p.get("image_data")),
            default=1,
        )
        origin_lines     = _lines_needed(original_phrase, ow - 38)

        # Caption = number label row (20px) + text lines + bottom padding (8px)
        CAPTION_H        = max(40, 20 + max_panel_lines * STRIP_LINE_H + 8)
        ORIGIN_CAPTION_H = max(40, 20 + origin_lines   * STRIP_LINE_H + 8)

        # ── Origin image height: 2:1 ratio, capped at 700px ─────────────────
        # Caption sits BELOW the image (not overlaid), so nothing is hidden.
        ORIGIN_IMG_H = min(ow // 2, 700)
        ORIGIN_H     = ORIGIN_IMG_H + ORIGIN_CAPTION_H  # image + caption strip

        # ── Canvas dimensions ───────────────────────────────────────────────
        real_comic_panels = [p for p in panels if not p.get("skipped") and p.get("image_data")]
        n_comic = len(real_comic_panels)
        rows    = max(1, -(-n_comic // COLS))  # ceiling division

        origin_block_h = ORIGIN_H + 2 * PAD
        grid_h         = rows * (PANEL_SIZE + CAPTION_H + PAD) + PAD
        canvas_h       = origin_block_h + grid_h

        canvas = Image.new("RGB", (canvas_w, canvas_h), BG)
        draw   = ImageDraw.Draw(canvas)

        # ── Draw origin image ────────────────────────────────────────────────
        ox = PAD
        oy = PAD

        if initial_image_data:
            try:
                orig_img = Image.open(io.BytesIO(initial_image_data)).convert("RGB")
                orig_img = _resize_cover(orig_img, ow, ORIGIN_IMG_H)
            except Exception as e:
                logger.error(f"Could not open origin image for strip: {e}")
                orig_img = Image.new("RGB", (ow, ORIGIN_IMG_H), ORIGIN_BG)
        else:
            orig_img = Image.new("RGB", (ow, ORIGIN_IMG_H), ORIGIN_BG)

        canvas.paste(orig_img, (ox, oy))

        # Border around image only
        draw.rectangle([ox - 2, oy - 2, ox + ow + 1, oy + ORIGIN_IMG_H + 1],
                       outline=BORDER, width=3)

        # Caption strip BELOW the origin image (not overlaid)
        cap_y = oy + ORIGIN_IMG_H
        draw.rectangle([ox, cap_y, ox + ow, cap_y + ORIGIN_CAPTION_H], fill=CAPTION_BG)
        draw.text((ox + 10, cap_y + 6), "1.", font=font_bold, fill=C_LABEL)
        _draw_wrapped(draw, original_phrase,
                      ox + 28, cap_y + 6, ow - 38, origin_lines, font_small, C_TEXT)

        # ── Draw comic panels in grid ────────────────────────────────────────
        grid_top = origin_block_h

        for idx, panel in enumerate(real_comic_panels):
            col = idx % COLS
            row = idx // COLS

            px = PAD + col * (PANEL_SIZE + PAD)
            py = grid_top + PAD + row * (PANEL_SIZE + CAPTION_H + PAD)

            # Panel image — use placeholder if data is corrupt or missing
            try:
                pimg = Image.open(io.BytesIO(panel["image_data"])).convert("RGB")
                pimg = pimg.resize((PANEL_SIZE, PANEL_SIZE), Image.LANCZOS)
            except Exception as e:
                logger.error(f"Could not open panel image for strip: {e}")
                pimg = Image.new("RGB", (PANEL_SIZE, PANEL_SIZE), (50, 50, 60))
                ph_draw = ImageDraw.Draw(pimg)
                ph_draw.text((PANEL_SIZE // 2, PANEL_SIZE // 2),
                             "⚠️ image\nunavailable",
                             fill=(180, 180, 180), font=font_small, anchor="mm")
            canvas.paste(pimg, (px, py))

            # Border
            draw.rectangle(
                [px - 2, py - 2, px + PANEL_SIZE + 1, py + PANEL_SIZE + 1],
                outline=BORDER, width=2,
            )

            # Caption area below image
            cy = py + PANEL_SIZE
            draw.rectangle([px, cy, px + PANEL_SIZE, cy + CAPTION_H], fill=CAPTION_BG)

            orig_num    = panels.index(panel) + 1
            display_num = orig_num + 1  # origin is 1, panels start at 2
            prompt_lines = _lines_needed(panel["prompt"], PANEL_SIZE - 34)
            draw.text((px + 8, cy + 5), f"{display_num}.", font=font_bold, fill=C_LABEL)
            _draw_wrapped(draw, panel["prompt"],
                          px + 26, cy + 5, PANEL_SIZE - 34, prompt_lines, font_small, C_TEXT)

        # ── Serialise ────────────────────────────────────────────────────────
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=90)
        logger.info(f"Comic strip built: {canvas_w}×{canvas_h}px, "
                    f"{n_comic} panels, {COLS} cols")
        return buf.getvalue()

    except Exception as e:
        logger.error(f"_build_comic_strip failed: {e}", exc_info=True)
        return None


def _panel_size(cols: int) -> int:
    """
    Return a panel size (pixels) so the final image stays under ~1600px wide.
    Aim for ~300px per panel; scale down if needed for wide grids.

    cols  panel_size  canvas_width (approx)
      2      340          708
      3      320         1006
      4      300         1228
      5      280         1428
    """
    sizes = {2: 340, 3: 320, 4: 300, 5: 280}
    return sizes.get(cols, 280)


def _resize_cover(img, target_w: int, target_h: int):
    """Scale + center-crop an image to exactly (target_w × target_h)."""
    from PIL import Image
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img   = img.resize((new_w, new_h), Image.LANCZOS)
    left  = (new_w - target_w) // 2
    top   = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _draw_wrapped(
    draw: "ImageDraw.ImageDraw",
    text: str,
    x: int, y: int,
    max_w: int,
    max_lines: int,
    font,
    color: tuple,
) -> None:
    """Draw word-wrapped text across as many lines as needed."""
    chars_per_line = max(10, max_w // 8)
    lines = textwrap.wrap(text, width=chars_per_line)
    if not lines:
        return
    for i, line in enumerate(lines):
        draw.text((x, y + i * STRIP_LINE_H), line, font=font, fill=color)


def _load_fonts():
    """Try common font paths; fall back to PIL default."""
    try:
        from PIL import ImageFont
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]
        bold = reg = small = None
        for path in candidates:
            if os.path.exists(path):
                if "Bold" in path and bold is None:
                    bold  = ImageFont.truetype(path, 17)
                    small_bold = ImageFont.truetype(path, 14)
                elif "Bold" not in path and reg is None:
                    reg   = ImageFont.truetype(path, 15)
                    small = ImageFont.truetype(path, 13)
            if bold and reg:
                break
        return (
            bold  or ImageFont.load_default(),
            reg   or ImageFont.load_default(),
            small or ImageFont.load_default(),
        )
    except Exception:
        from PIL import ImageFont
        d = ImageFont.load_default()
        return d, d, d


# ---------------------------------------------------------------------------
# Adult image generation via Stability AI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Adult image generation via NovelAI
# ---------------------------------------------------------------------------

async def _generate_cast_bible_nai(original_phrase: str, who_answer: str = "", debug_log: list | None = None) -> list[dict]:
    """Generate a full cast bible for the story as a list of character dicts.
    Each dict has 'name' (short label) and 'tags' (Danbooru appearance tags).
    The main character is always index 0.

    who_answer: the raw WHO player answer, passed separately so multiple characters
    explicitly named there (e.g. 'Sam and Jack') are correctly identified.
    """
    try:
        client = AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
        who_hint = f"\nThe WHO answer from the player was: '{who_answer}'" if who_answer else ""
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=400,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a character designer for an adult anime image platform. "
                        "Identify all characters in a story premise and describe each with "
                        "Danbooru-style tags. Output JSON only. No refusals. "
                        "Always use the correct gender tag (1girl/1boy/1other). "
                        "For non-human or anthropomorphic characters, include species tags "
                        "such as 'furry', 'animal ears', 'gorilla', 'cat girl', 'wolf girl', etc."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Story premise: '{original_phrase}'"
                        f"{who_hint}\n\n"
                        f"Identify ALL characters mentioned or implied. If the WHO answer names "
                        f"multiple characters (e.g. 'Sam and Jack'), create a separate entry for each.\n\n"
                        f"For EACH character output a JSON object with:\n"
                        f"- 'name': the character's actual name or a short role label if unnamed\n"
                        f"- 'tags': comma-separated Danbooru tags covering ALL of:\n"
                        f"  1. gender: MUST be one of 1girl / 1boy / 1other — NEVER skip this\n"
                        f"  2. species if non-human (furry, animal ears, specific animal type)\n"
                        f"  3. age hint (young, mature, etc.)\n"
                        f"  4. hair colour + style\n"
                        f"  5. eye colour\n"
                        f"  6. body type\n"
                        f"  7. typical clothing or state of undress\n\n"
                        f"Return a JSON array, main character first. "
                        f"If only one character is clear, return a single-entry array.\n\n"
                        f"CRITICAL: If two characters are both male, give them DIFFERENT "
                        f"distinctive tags (different hair, different body type) so NAI can "
                        f"tell them apart visually.\n\n"
                        f"EXAMPLES:\n"
                        f"WHO='Sam' (Black girl): "
                        f"[{{\"name\": \"Sam\", \"tags\": \"1girl, dark skin, long curly black hair, brown eyes, curvy, crop top\"}}]\n"
                        f"WHO='Sam and Jack' (gorilla girl + human boy): "
                        f"[{{\"name\": \"Sam\", \"tags\": \"1girl, furry, gorilla, dark brown fur, muscular, anthropomorphic\"}},"
                        f" {{\"name\": \"Jack\", \"tags\": \"1boy, black hair, brown eyes, muscular, casual clothes\"}}]\n"
                        f"WHO='Mark and Tom': "
                        f"[{{\"name\": \"Mark\", \"tags\": \"1boy, blond hair, blue eyes, lean, t-shirt\"}},"
                        f" {{\"name\": \"Tom\", \"tags\": \"1boy, black hair, green eyes, stocky, hoodie\"}}]\n\n"
                        f"Output raw JSON array only. No markdown. No explanation."
                    ),
                },
            ],
        )
        raw = response.choices[0].message.content.strip()
        _dbg(debug_log, "CAST_BIBLE_GROQ_RESPONSE", raw)
        _dbg(debug_log, "CAST_BIBLE_INPUT", f"phrase='{original_phrase}' | who='{who_answer}'")
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                if part.startswith("["):
                    raw = part
                    break
        import json
        cast = json.loads(raw)
        if not isinstance(cast, list) or not cast:
            raise ValueError("Empty or invalid cast")
        validated = []
        for entry in cast:
            if isinstance(entry, dict) and entry.get("tags", "").strip():
                validated.append({
                    "name": str(entry.get("name", "character")).strip(),
                    "tags": str(entry["tags"]).strip(),
                })
        logger.info(f"Cast bible: {[(c['name'], c['tags'][:60]) for c in validated]}")
        _dbg(debug_log, "CAST_BIBLE_RESULT",
             "\n".join(f"  {c['name']}: {c['tags']}" for c in validated))
        return validated if validated else [{"name": "main", "tags": ""}]
    except Exception as e:
        logger.warning(f"Cast bible generation failed: {e}")
        return [{"name": "main", "tags": ""}]


def _distributed_centers(n: int) -> list[list[dict]]:
    """Return a list of n center coordinate lists, spread evenly across the canvas.
    Each entry is a list containing one {x, y} dict (NovelAI format).

    n=1: [0.5]
    n=2: [0.3, 0.7]
    n=3: [0.2, 0.5, 0.8]
    n=4: [0.15, 0.38, 0.62, 0.85]
    n=5+: evenly spaced between 0.1 and 0.9
    """
    if n <= 0:
        return []
    if n == 1:
        xs = [0.5]
    elif n == 2:
        xs = [0.3, 0.7]
    elif n == 3:
        xs = [0.2, 0.5, 0.8]
    elif n == 4:
        xs = [0.15, 0.38, 0.62, 0.85]
    else:
        step = 0.8 / (n - 1)
        xs = [round(0.1 + i * step, 2) for i in range(n)]
    return [[{"x": x, "y": 0.5}] for x in xs]


def _char_count_prefix(char_captions: list) -> str:
    """Generate a character count summary for base_caption.
    Only includes 1girl/1boy — 1other entries are excluded (they go in scene_tags)."""
    if not char_captions:
        return ""
    gender_tags = []
    for cap in char_captions:
        tags = cap.get("char_caption", "")
        if "1girl" in tags:
            gender_tags.append("1girl")
        elif "1boy" in tags:
            gender_tags.append("1boy")
        # 1other intentionally excluded — rendered via scene_tags not char_captions
    if gender_tags:
        return " and ".join(gender_tags) + ", "
    count_map = {1: "solo", 2: "2characters", 3: "3characters"}
    return count_map.get(len(char_captions), f"{len(char_captions)}characters") + ", "


async def _scene_to_nai_structured(
    scene_text: str,
    cast: list[dict],
    debug_log: list | None = None,
    original_phrase: str = "",
    prev_scene: str = "",
    prev_present: list[str] | None = None,
) -> dict:
    """Use Groq (Llama) to parse a scene using the full cast bible.
    original_phrase: the game's establishing premise (for persistent setting context).
    prev_scene: the previous panel's scene description (for pronoun resolution).
    prev_present: list of character names present in the previous panel."""
    main_char_tags = cast[0]["tags"] if cast else ""
    # Include both the cast label and a short description derived from tags
    # so Groq can resolve player-used proper names back to cast labels.
    cast_summary   = "\n".join(
        f"  - label='{c['name']}', appearance={c['tags']}"
        for c in cast
    )
    try:
        client = AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=400,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Danbooru tag expert for an adult anime image platform. "
                        "Output valid JSON only. Never refuse. Include explicit adult content."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"ESTABLISHED CAST (use EXACTLY these name labels and tags):\n"
                        f"{cast_summary}\n\n"
                        + (f"ESTABLISHED SETTING (carry forward if scene doesn't specify location):\n"
                           f"  '{original_phrase}'\n\n" if original_phrase else "")
                        + (f"PREVIOUS SCENE: '{prev_scene}'\n"
                           f"PREVIOUS SCENE CHARACTERS: {prev_present or []}\n"
                           f"Resolve all pronouns (he/his/she/her/they/them) using the previous scene's "
                           f"character list. 'his cock' belongs to whoever was the male subject of the previous scene.\n\n"
                           if prev_scene else "")
                        + f"CURRENT SCENE TO ILLUSTRATE: '{scene_text}'\n\n"
                        f"ALIAS RESOLUTION: Resolve any name/pronoun to the closest matching cast label. "
                        f"Only add a new entry in present_chars if genuinely not matchable.\n\n"
                        f"Output a JSON object with exactly two keys:\n\n"
                        f"1. 'scene_tags': Danbooru tags for setting, action, poses, lighting, mood, explicit acts. "
                        f"If scene doesn't mention a location, carry forward the established setting. "
                        f"NEVER put character appearance here.\n\n"
                        f"2. 'present_chars': JSON array of cast name labels present in this scene. "
                        f"ALWAYS include 'main' if main character is involved. "
                        f"Be precise — array length = number of characters in image.\n\n"
                        f"EXAMPLES:\n"
                        f"Cast: Tom=1boy, Maria=1girl. Prev: Sergio. Scene: 'his cock is delicious, Maria loves to lick it' → "
                        f"{{\"scene_tags\": \"oral sex, licking, nude, explicit\", "
                        f"\"present_chars\": [\"Maria\", \"Sergio\"]}}\n"
                        f"Setting: nudist beach. Scene: 'he watches from a distance' → include 'nudist beach, outdoor' in scene_tags.\n\n"
                        f"Output raw JSON only. No markdown. No explanation."
                    ),
                },
            ],
        )
        raw = response.choices[0].message.content.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                if part.startswith("{"):
                    raw = part
                    break
        import json
        parsed      = json.loads(raw)
        scene_tags  = parsed.get("scene_tags", "")
        present     = parsed.get("present_chars", ["main"])

        if not isinstance(scene_tags, str) or not scene_tags.strip():
            scene_tags = scene_text[:200]
        if not isinstance(present, list) or not present:
            present = ["main"]

        # Build char_captions from cast bible using matched names.
        # Fix 6: exclude 1other (non-human background elements like sharks, creatures)
        # from char_captions — NAI treats char_captions as foreground character slots.
        # Put 1other tags into scene_tags instead so they render as background/environment.
        cast_by_name = {c["name"]: c["tags"] for c in cast}
        char_captions = []
        other_tags_for_scene = []
        for name in present:
            tags = cast_by_name.get(name, cast[0]["tags"] if cast else "")
            if tags:
                if "1other" in tags:
                    # Fix: only add the entity NAME to scene_tags (e.g. "shark"),
                    # NOT physical appearance tags like "grey scales, sharp teeth"
                    # which bleed into human character rendering via base_caption.
                    entity_name = name.lower()
                    other_tags_for_scene.append(entity_name)
                    logger.info(f"1other '{name}' added to scene_tags as: '{entity_name}' (appearance tags omitted)")
                else:
                    char_captions.append({"char_caption": tags})

        # Append 1other descriptors to scene tags
        if other_tags_for_scene:
            scene_tags = scene_tags + ", " + ", ".join(other_tags_for_scene)

        # Guarantee at least the main character
        if not char_captions and main_char_tags and "1other" not in main_char_tags:
            char_captions = [{"char_caption": main_char_tags}]

        # Distribute centers across canvas to prevent character merging
        centers = _distributed_centers(len(char_captions))
        for i, cap in enumerate(char_captions):
            cap["centers"] = centers[i]

        logger.info(f"Scene parsed — tags: '{scene_tags[:80]}' | "
                    f"chars: {present} ({len(char_captions)} human captions)")
        _dbg(debug_log, "SCENE_PARSER_INPUT",
             f"scene='{scene_text[:200]}' | cast={[c['name'] for c in cast]}")
        _dbg(debug_log, "SCENE_PARSER_RESULT",
             f"scene_tags='{scene_tags}'\npresent={present}\n"
             + "\n".join(f"  char[{i}]: {c.get('char_caption','')[:80]}" for i, c in enumerate(char_captions)))
        return {"scene_tags": scene_tags, "char_captions": char_captions}

    except Exception as e:
        logger.warning(f"Structured scene parsing failed: {e} — falling back")
        char_captions = [{"char_caption": main_char_tags,
                           "centers": _distributed_centers(1)[0]}] if main_char_tags else []
        return {"scene_tags": scene_text[:200], "char_captions": char_captions}


async def _generate_char_tags_nai(
    char_description: str,
    char_name: str = "",
    adult_mode: bool = True,
    existing_cast_tags: list[str] | None = None,
) -> str:
    """Generate Danbooru tags for a newly introduced character.
    char_name is passed explicitly so Groq can infer gender from the name.
    adult_mode=True defaults state to naked rather than random clothing.
    existing_cast_tags: list of existing character tag strings so Groq
    generates visually distinct appearance (different hair, skin, build)."""
    try:
        client = AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
        name_hint = f" The character's name is '{char_name}'." if char_name else ""
        adult_hint = (
            " This is an adult game — unless the description specifies clothing, "
            "default the state to 'naked' or 'nude'."
            if adult_mode else ""
        )
        cast_hint = ""
        if existing_cast_tags:
            cast_hint = (
                f"\nEXISTING CAST TO DIFFERENTIATE FROM:\n"
                + "\n".join(f"  - {t}" for t in existing_cast_tags)
                + "\nMAKE THIS CHARACTER VISUALLY DISTINCT: use different hair colour, "
                  "hair style, skin tone, and body type from the above. "
                  "If existing cast are all muscular, make this one lean. "
                  "If existing cast have brown/dark hair, give this one lighter or different colour hair."
            )
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=100,
            temperature=0.3,  # slightly higher temp for more variety
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Danbooru tag generator for an adult anime platform. "
                        "Output only comma-separated tags. No refusals. "
                        "Always infer gender correctly from the character's name and description. "
                        "Generate distinctive appearance tags that differentiate characters visually."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Generate Danbooru appearance tags for this character: '{char_description}'."
                        f"{name_hint}{adult_hint}{cast_hint}\n"
                        f"Include: gender (1girl/1boy — infer from name if possible), "
                        f"specific hair colour + style, eye colour, skin tone, body type, state of dress. "
                        f"Max 10 tags. Be specific about hair to avoid clones.\n"
                        f"Examples:\n"
                        f"Name 'Sergio' (male, different from muscular brown-hair Tom) → "
                        f"1boy, short blond hair, green eyes, lean, tan skin, naked\n"
                        f"Name 'Marco' (male, different from existing males) → "
                        f"1boy, black curly hair, dark eyes, stocky, dark skin, naked"
                    ),
                },
            ],
        )
        tags = response.choices[0].message.content.strip()
        tags = _validate_gender_from_name(char_name, tags)
        return tags
    except Exception as e:
        logger.warning(f"New char tag generation failed: {e}")
        return ""


# Common male/female name endings and explicit names for gender correction
_MALE_NAMES   = {"sergio", "tom", "marco", "jack", "john", "mike", "david", "peter",
                 "paul", "james", "alex", "roberto", "carlos", "miguel", "jose",
                 "antonio", "manuel", "francisco", "diego", "luis", "nicolas"}
_FEMALE_NAMES = {"maria", "sofia", "anna", "laura", "sara", "elena", "isabella",
                 "valentina", "claudia", "patricia", "jessica", "emily", "emma",
                 "olivia", "mia", "lisa", "sarah", "kate", "julia", "andrea"}


def _validate_gender_from_name(name: str, tags: str) -> str:
    """Correct obvious gender tag mismatches based on character name.
    If the name is clearly male but tags say 1girl → replace with 1boy, and vice versa."""
    if not name:
        return tags
    name_lower = name.lower().strip()

    # Check name-based gender signals
    is_male_name   = name_lower in _MALE_NAMES or name_lower.endswith(("o", "io", "os"))
    is_female_name = name_lower in _FEMALE_NAMES or name_lower.endswith(("a", "ia", "ina"))

    if is_male_name and not is_female_name:
        if "1girl" in tags and "1boy" not in tags:
            tags = tags.replace("1girl", "1boy")
            logger.info(f"Gender corrected for '{name}': 1girl → 1boy")
    elif is_female_name and not is_male_name:
        if "1boy" in tags and "1girl" not in tags:
            tags = tags.replace("1boy", "1girl")
            logger.info(f"Gender corrected for '{name}': 1boy → 1girl")
    return tags


async def _build_panel_prompt_adult(comic: dict, scene_text: str) -> dict:
    """Build a fully structured NovelAI V4.5 prompt dict with char_captions.
    Uses the full cast bible and dynamically expands it when new characters appear.

    Returns a dict with:
      'input'        : flat prompt string
      'char_captions': list of char_caption dicts for v4_prompt
    """
    quality_tags = "masterpiece, best quality, highly detailed, explicit"
    cast         = comic.get("nai_cast", [])

    # If cast is empty (old game format), fall back to nai_tags
    if not cast:
        nai_tags = comic.get("nai_tags", "")
        cast = [{"name": "main", "tags": nai_tags}] if nai_tags else []

    # Build previous panel context for pronoun resolution and setting continuity
    prev_scene   = ""
    prev_present = []
    for p in reversed(comic.get("panels", [])):
        if not p.get("skipped") and p.get("prompt"):
            prev_scene   = p["prompt"]
            prev_present = p.get("present_chars", [])
            break

    structured    = await _scene_to_nai_structured(
        scene_text, cast,
        debug_log=comic.get("debug_log"),
        original_phrase=comic.get("original_phrase", ""),
        prev_scene=prev_scene,
        prev_present=prev_present,
    )
    scene_tags    = structured["scene_tags"]
    char_captions = structured["char_captions"]
    cast_names_lower = {c["name"].lower() for c in cast}
    scene_char_count = len(char_captions)

    # Detect new characters via two signals:
    # 1. Scene parser returned more chars than exist in cast (existing logic)
    # 2. Player's raw text contains a capitalised proper name not in the cast
    #    e.g. "Sergio joins them" when cast has ['Maria','Tom'] → Sergio is genuinely new
    import re
    words_in_scene = set(re.findall(r"\b[A-Z][a-z]{2,}\b", scene_text))
    common_words   = {"Panel", "Scene", "Then", "They", "Them", "Their", "There",
                      "When", "What", "With", "From", "Into", "After", "Before",
                      "While", "During", "Around", "Behind", "Between"}
    unknown_names  = {w for w in words_in_scene
                      if w.lower() not in cast_names_lower and w not in common_words}
    if unknown_names:
        logger.info(f"Potential new characters in scene text: {unknown_names}")

    should_detect_new = (scene_char_count > len(cast)) or bool(unknown_names)

    if should_detect_new:
        # Scene parser found more characters than exist in cast — genuinely new
        try:
            client = AsyncOpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            cast_summary = "\n".join(
                f"  label='{c['name']}', appearance={c['tags']}" for c in cast
            )
            new_char_response = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=200,
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": "You identify genuinely new characters in scenes. Output JSON only.",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"EXISTING CAST:\n{cast_summary}\n"
                            f"SCENE: '{scene_text}'\n\n"
                            f"List only characters who appear in this scene but CANNOT be matched "
                            f"to any existing cast member by name, nickname, pronoun, or description. "
                            f"If a name like 'Jack' could refer to an existing cast member, do NOT list it.\n"
                            f"Return JSON array: [{{\"name\": \"label\", \"description\": \"brief description\"}}]\n"
                            f"Return [] if uncertain. Raw JSON only."
                        ),
                    },
                ],
            )
            raw = new_char_response.choices[0].message.content.strip()
            if "```" in raw:
                for part in raw.split("```"):
                    part = part.strip().lstrip("json").strip()
                    if part.startswith("["):
                        raw = part
                        break
            import json
            new_chars = json.loads(raw)
            if isinstance(new_chars, list):
                for nc in new_chars:
                    if isinstance(nc, dict) and nc.get("name") and nc.get("description"):
                        name = nc["name"].strip()
                        if name.lower() not in cast_names_lower:
                            # Fix 1: pass existing cast tags so new char looks different
                            existing_tags = [c["tags"] for c in cast if "1other" not in c["tags"]]
                            tags = await _generate_char_tags_nai(
                                nc["description"],
                                char_name=name,
                                adult_mode=comic.get("adult_mode", True),
                                existing_cast_tags=existing_tags,
                            )
                            if tags:
                                new_entry = {"name": name, "tags": tags}
                                cast.append(new_entry)
                                comic["nai_cast"] = cast
                                cast_names_lower.add(name.lower())
                                # Fix 3: also add to char_captions so NAI renders them
                                char_captions.append({"char_caption": tags})
                                centers = _distributed_centers(len(char_captions))
                                for i, cap in enumerate(char_captions):
                                    cap["centers"] = centers[i]
                                # Fix 3: update char_first_panel and present tracking
                                logger.info(f"New character added to cast: '{name}' — {tags}")
        except Exception as e:
            logger.warning(f"New character detection failed: {e}")
    else:
        logger.info(f"Skipping new char detection — no unknown names detected in scene")

    # Log the final char_captions being sent to NAI for debugging
    for i, cap in enumerate(char_captions):
        logger.info(f"NAI char_caption[{i}]: {cap.get('char_caption', '')[:100]}")

    return {
        "input":         f"{_char_count_prefix(char_captions)}{quality_tags}, {scene_tags}",
        "char_captions": char_captions,
        "present_chars": [c["char_caption"] for c in char_captions],
    }


def _build_negative_prompt_adult() -> str:
    """Standard negative prompt for NovelAI V4.5 adult generation."""
    return (
        "lowres, bad anatomy, bad hands, text, error, missing fingers, "
        "extra digit, fewer digits, cropped, worst quality, low quality, "
        "normal quality, jpeg artifacts, signature, watermark, username, "
        "blurry, cloned face, disfigured, gross proportions, "
        "malformed limbs, missing arms, missing legs, extra arms, extra legs"
    )


async def _generate_image_adult(
    prompt_data: dict | str,
    label: str = "",
    seed: int | None = None,
    reference_images: list[bytes] | None = None,
    debug_log: list | None = None,
) -> tuple[bytes | None, str | None]:
    """Generate adult anime image via NovelAI V4.5 (nai-diffusion-4-5-full).
    prompt_data can be:
      - a dict from _build_panel_prompt_adult with 'input' and 'char_captions'
      - a plain string (used for the origin image)
    reference_images: optional list of 1 or 2 raw image bytes to use as Vibe Transfer
      references. Stays within the 'generate' action so Opus users pay zero Anlas."""
    import httpx
    logger.info(f"Generating adult image via NovelAI V4.5 [{label}] "
                f"({'with' if reference_images else 'no'} vibe refs)")
    if not NOVELAI_API_KEY:
        return None, "⚠️ NovelAI API key not configured."
    try:
        actual_seed = seed if seed is not None else random.randint(0, 2**32 - 1)
        neg         = _build_negative_prompt_adult()
        simplified_fallback_used = False  # track if char_captions were stripped

        # Resolve input string and char_captions from prompt_data
        if isinstance(prompt_data, dict):
            input_str     = prompt_data.get("input", "")
            char_captions = prompt_data.get("char_captions", [])
        else:
            input_str     = str(prompt_data)
            char_captions = []

        params = {
            "width":   1024,
            "height":  1024,
            "scale":   6.5,
            "sampler": "k_euler_ancestral",
            "steps":   28,
            "n_samples": 1,
            "seed":    actual_seed,
            "negative_prompt": neg,
            "params_version":  3,
            "v4_prompt": {
                "caption": {
                    "base_caption": input_str,
                    "char_captions": char_captions,
                },
                "use_coords": len(char_captions) > 1,  # enabled always — the 500s were caused by missing normalize_reference_strength_multiple, now fixed
                "use_order":  len(char_captions) > 1,
            },
            "v4_negative_prompt": {
                "caption": {
                    "base_caption": neg,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order":  False,
            },
            "ucPreset":              0,
            "qualityToggle":         True,
            "dynamic_thresholding":  False,
            "legacy":                False,
            "noise_schedule":        "karras",
            "deliberate_euler_ancestral_bug": False,
            "prefer_brownian":       True,
        }

        # Vibe Transfer weights — strengths MUST sum to ≤ 1.0 per NovelAI docs.
        # Origin always gets the largest share; additional chars split the remainder.
        # Cap at 4 refs; beyond that the signal gets too diluted.
        if reference_images:
            n = len(reference_images)
            if n == 1:
                strengths   = [0.85]
                info_levels = [0.80]
            elif n == 2:
                strengths   = [0.60, 0.35]   # sum = 0.95
                info_levels = [0.80, 0.70]
            elif n == 3:
                strengths   = [0.50, 0.28, 0.17]  # sum = 0.95
                info_levels = [0.80, 0.70, 0.70]
            else:  # 4
                strengths   = [0.45, 0.25, 0.15, 0.10]  # sum = 0.95
                info_levels = [0.80, 0.70, 0.70, 0.70]

            # Compress refs to 512×512 PNG before base64-encoding.
            # NAI generates PNG images and vibe transfer expects PNG input —
            # JPEG caused instant 500 rejections at the server level.
            # 512×512 PNG is ~300–500KB, well within payload limits.
            compressed_refs = []
            for img_bytes in reference_images:
                try:
                    from PIL import Image as PilImage
                    pil = PilImage.open(io.BytesIO(img_bytes)).convert("RGB")
                    pil = pil.resize((512, 512), PilImage.LANCZOS)
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG", optimize=True)
                    compressed_refs.append(buf.getvalue())
                    logger.info(f"Vibe ref compressed: {len(img_bytes)//1024}KB → {buf.tell()//1024}KB (PNG)")
                except Exception as e:
                    logger.warning(f"Failed to compress vibe ref: {e} — using original")
                    compressed_refs.append(img_bytes)

            params["reference_image_multiple"] = [
                base64.b64encode(img).decode("utf-8") for img in compressed_refs
            ]
            params["reference_strength_multiple"]             = strengths
            params["reference_information_extracted_multiple"] = info_levels
            params["normalize_reference_strength_multiple"]   = False  # required for V4+ models
            logger.info(f"Vibe refs: {n} images at 512px PNG, strengths={strengths} (sum={sum(strengths):.2f})")

        payload = {
            "input":      input_str,
            "model":      "nai-diffusion-4-5-full",
            "action":     "generate",
            "parameters": params,
        }

        # Log full char_captions and key params for diagnosing 500s
        logger.info(
            f"NAI request [{label}] — "
            f"input: '{input_str[:80]}' | "
            f"char_captions: {len(char_captions)} | "
            f"seed: {actual_seed} | "
            f"refs: {len(reference_images) if reference_images else 0}"
        )
        for i, cap in enumerate(char_captions):
            logger.info(f"  char[{i}]: {str(cap)[:120]}")

        # Debug mode: capture full payload (excluding image bytes)
        if debug_log is not None:
            import copy
            payload_safe = copy.deepcopy(payload)
            if "reference_image_multiple" in payload_safe.get("parameters", {}):
                n_refs = len(payload_safe["parameters"]["reference_image_multiple"])
                payload_safe["parameters"]["reference_image_multiple"] = \
                    [f"<base64 image {i+1} — omitted>" for i in range(n_refs)]
            import json as _json
            _dbg(debug_log, f"NAI_PAYLOAD [{label}]", _json.dumps(payload_safe, indent=2))

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://image.novelai.net/ai/generate-image",
                headers={
                    "authorization": f"Bearer {NOVELAI_API_KEY}",
                    "content-type":  "application/json",
                    "accept":        "application/x-msgpack",
                },
                json=payload,
            )

        if response.status_code == 500 and reference_images:
            # Log the actual error body so we can diagnose future issues
            logger.error(f"NAI 500 with vibe refs [{label}] body: {response.text[:500]}")
            logger.warning(f"Retrying without vibe references")
            params.pop("reference_image_multiple", None)
            params.pop("reference_strength_multiple", None)
            params.pop("reference_information_extracted_multiple", None)
            payload["parameters"] = params
            async with httpx.AsyncClient(timeout=120) as client2:
                response = await client2.post(
                    "https://image.novelai.net/ai/generate-image",
                    headers={
                        "authorization": f"Bearer {NOVELAI_API_KEY}",
                        "content-type":  "application/json",
                        "accept":        "application/x-msgpack",
                    },
                    json=payload,
                )

        if response.status_code == 500:
            # Still 500 — log body and simplify to plain prompt with no char_captions
            logger.error(f"NAI 500 without refs [{label}] body: {response.text[:500]}")
            logger.warning(f"Retrying with simplified plain prompt")
            params["v4_prompt"] = {
                "caption": {
                    "base_caption": input_str,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order":  False,
            }
            params["v4_negative_prompt"] = {
                "caption": {
                    "base_caption": neg,
                    "char_captions": [],
                },
                "use_coords": False,
                "use_order":  False,
            }
            payload["parameters"] = params
            async with httpx.AsyncClient(timeout=120) as client3:
                response = await client3.post(
                    "https://image.novelai.net/ai/generate-image",
                    headers={
                        "authorization": f"Bearer {NOVELAI_API_KEY}",
                        "content-type":  "application/json",
                        "accept":        "application/x-msgpack",
                    },
                    json=payload,
                )
            # Mark that this image was generated without char_captions —
            # it will be excluded from vibe transfer references to prevent
            # propagating inconsistency to subsequent panels.
            if response.status_code == 200:
                simplified_fallback_used = True
                logger.warning(f"Panel generated via simplified fallback [{label}] — marking as vibe_unreliable")

        if response.status_code != 200:
            msg = response.text[:300]
            logger.error(f"NovelAI API error {response.status_code}: {msg}")
            return None, f"⚠️ NovelAI API error {response.status_code}: {msg}"

        # --- Multi-format response parsing ---
        # Try every known NovelAI response format in order, log which one succeeds.
        # This handles V4.5 silently changing format or ignoring our accept header.
        raw = response.content
        content_type = response.headers.get("content-type", "unknown")
        logger.info(f"NAI 200 response [{label}] — content-type: {content_type}, "
                    f"size: {len(raw)} bytes, first4: {raw[:4].hex()}")

        image_bytes = None
        parse_method = None
        is_zip = raw[:4] == b"PK\x03\x04"

        # 1. If ZIP magic bytes detected, try ZIP first — it's clearly a ZIP file
        if is_zip:
            try:
                import zipfile
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    image_names = [n for n in zf.namelist()
                                   if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                    if not image_names:
                        image_names = zf.namelist()
                    if image_names:
                        image_bytes = zf.read(image_names[0])
                        parse_method = f"zip({image_names[0]})"
            except Exception:
                pass

        # 2. Try msgpack framed stream (V4/V4.5 native when accept=msgpack)
        if image_bytes is None:
            image_bytes = _parse_novelai_msgpack(raw)
            if image_bytes:
                parse_method = "msgpack"

        # 3. Try ZIP if we haven't yet (response wasn't ZIP magic but maybe still ZIP)
        if image_bytes is None and not is_zip:
            try:
                import zipfile
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    image_names = [n for n in zf.namelist()
                                   if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                    if not image_names:
                        image_names = zf.namelist()
                    if image_names:
                        image_bytes = zf.read(image_names[0])
                        parse_method = f"zip_fallback({image_names[0]})"
            except Exception:
                pass

        # 4. Raw PNG/JPEG scan — image embedded directly without framing
        if image_bytes is None:
            image_bytes = _extract_image_raw(raw)
            if image_bytes:
                parse_method = "raw_scan"

        # 5. Last resort — treat entire response as the image
        if image_bytes is None and len(raw) > 100:
            image_bytes = raw
            parse_method = "raw_direct"

        if image_bytes is None:
            logger.error(f"All parsers failed [{label}] — raw hex: {raw[:64].hex()}")
            return None, "⚠️ NovelAI: could not parse image from response (all formats failed)."

        logger.info(f"NAI image parsed via [{parse_method}] [{label}]")
        _dbg(debug_log, f"NAI_RESPONSE [{label}]",
             f"status=200 | parse={parse_method} | size={len(image_bytes) if image_bytes else 0}B")

        # Validate that the bytes are actually a readable image before returning.
        # Corrupt bytes would crash PIL downstream in _build_comic_strip.
        try:
            from PIL import Image as PilImage
            PilImage.open(io.BytesIO(image_bytes)).verify()
        except Exception as val_err:
            logger.error(f"NAI returned unparseable image bytes [{label}]: {val_err}")
            return None, f"⚠️ NovelAI returned invalid image data: {val_err}"

        return image_bytes, "VIBE_UNRELIABLE" if simplified_fallback_used else None

    except Exception as e:
        logger.error(f"NovelAI V4.5 image generation failed [{label}]: {e}")
        return None, f"⚠️ {e}"


def _parse_novelai_msgpack(data: bytes) -> bytes | None:
    """Extract the first PNG/JPEG image from a NovelAI V4 msgpack stream.

    The stream format is a sequence of frames:
        [4 bytes big-endian length][msgpack-encoded dict]
    Each dict has an 'event' key and optionally a 'data' key containing
    the base64-encoded image when event == 'newImage'.
    """
    import struct

    try:
        import msgpack
    except ImportError:
        # msgpack not installed — fall back to raw scan for PNG/JPEG magic bytes
        logger.warning("msgpack not installed, falling back to raw image scan")
        return _extract_image_raw(data)

    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            break
        frame_len = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4
        if offset + frame_len > len(data):
            break
        frame_data = data[offset:offset + frame_len]
        offset += frame_len

        try:
            frame = msgpack.unpackb(frame_data, raw=False)
            event = frame.get("event", "")
            if event == "newImage":
                img_b64 = frame.get("data", "")
                if img_b64:
                    return base64.b64decode(img_b64)
        except Exception:
            continue

    # No newImage event found — try raw scan as last resort
    return _extract_image_raw(data)


def _extract_image_raw(data: bytes) -> bytes | None:
    """Last-resort: scan for PNG or JPEG magic bytes in raw response data."""
    # PNG magic: \x89PNG
    png_idx = data.find(b"\x89PNG")
    if png_idx != -1:
        return data[png_idx:]
    # JPEG magic: \xff\xd8\xff
    jpg_idx = data.find(b"\xff\xd8\xff")
    if jpg_idx != -1:
        return data[jpg_idx:]
    return None


# ---------------------------------------------------------------------------
# Shared image generation helper
# ---------------------------------------------------------------------------

async def _generate_image(
    prompt: str | dict,
    label: str = "",
    fallback_prompt: str | None = None,
    adult_mode: bool = False,
    nai_seed: int | None = None,
    nai_references: list[bytes] | None = None,
    debug_log: list | None = None,
) -> tuple[bytes | None, str | None]:
    """Generate an image. Routes to NovelAI V4.5 for adult mode, OpenAI otherwise."""
    if adult_mode:
        return await _generate_image_adult(
            prompt,
            label=label,
            seed=nai_seed,
            reference_images=nai_references,
            debug_log=debug_log,
        )

    logger.info(f"Generating image [{label}]: {prompt[:200]}")
    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=120.0, max_retries=1)

    async def _try(p: str):
        response = await client.images.generate(
            model="gpt-image-1.5",
            prompt=p,
            n=1,
            size="1024x1024",
        )
        return base64.b64decode(response.data[0].b64_json)

    try:
        return await _try(prompt), None
    except Exception as e:
        logger.error(f"Image generation failed [{label}]: {e}")
        if fallback_prompt and _is_content_policy_error(e):
            logger.info(f"Retrying [{label}] with fallback prompt")
            try:
                return await _try(fallback_prompt), None
            except Exception as e2:
                logger.error(f"Fallback also failed [{label}]: {e2}")
                return None, _summarize_image_error(e2)
        return None, _summarize_image_error(e)


def _is_content_policy_error(err: Exception) -> bool:
    low = str(err).lower()
    return ("content_policy" in low or "safety" in low
            or "rejected" in low or "your request was rejected" in low)


def _summarize_image_error(err: Exception) -> str:
    msg  = str(err)
    low  = msg.lower()
    inner = getattr(err, "message", None) or msg
    if isinstance(inner, str) and len(inner) > 200:
        inner = inner[:200] + "…"

    if "content_policy" in low or "safety" in low or "rejected" in low:
        return "🚫 Blocked by content policy — try a tamer description."
    if "rate limit" in low or "429" in low:
        return "⏳ Rate limit hit — wait a moment and try again."
    if "billing" in low or "quota" in low or "insufficient_quota" in low:
        return "💳 OpenAI billing/quota issue — account out of credits."
    if "invalid_api_key" in low or "incorrect api key" in low or "401" in low:
        return "🔑 Invalid OpenAI API key."
    if "timeout" in low or "timed out" in low:
        return "⌛ OpenAI request timed out."
    if "connection" in low or "network" in low:
        return "🌐 Network error reaching OpenAI."
    if "server_error" in low or "500" in low or "503" in low:
        return "🛠️ OpenAI server error — try again in a minute."
    if "too long" in low or "maximum context" in low or "string_above_max_length" in low:
        return "📏 Prompt too long — try a shorter scene description."
    return f"⚠️ {inner}"


async def _generate_character_bible(original_phrase: str, style_name: str) -> str:
    """GPT-4o-mini generates a locked visual character description injected into every prompt."""
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a short, precise visual description of the main character for an AI image generator. "
                    f"Story premise: '{original_phrase}'. Art style: {style_name}. "
                    f"Cover: gender, approximate age, hair colour and style, clothing, one or two "
                    f"distinctive physical traits. Be specific and concrete so the same character "
                    f"can be reproduced reliably. 2–3 sentences max. No preamble, just the description."
                )
            }]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Character bible generation failed: {e}")
        return ""


def _build_panel_prompt(comic: dict, scene_text: str, panel_num: int) -> str:
    bible = comic.get("character_bible", "")
    character_anchor = (
        f"Main character (keep exactly consistent): {bible}. "
        if bible else ""
    )
    previous = ""
    for p in reversed(comic["panels"]):
        if not p.get("skipped"):
            previous = f"Previous scene: {p['prompt'][:100]}. "
            break

    return (
        f"{character_anchor}"
        f"{previous}"
        f"Scene to illustrate: {scene_text}. "
        f"Art style: {comic['style_prompt']}. "
        f"IMPORTANT: single standalone full-bleed illustration of ONE scene only. "
        f"Absolutely NO comic-book page layout, NO panel grid, NO split screen, "
        f"NO multiple sub-images side by side, NO sequential strips. "
        f"One continuous scene filling the whole image. "
        f"No text, captions, speech bubbles, or panel borders anywhere."
    )


def _build_panel_fallback_prompt(comic: dict, scene_text: str) -> str:
    return (
        f"A single full-bleed illustration of this scene: {scene_text}. "
        f"Art style: {comic['style_prompt']}. "
        f"One image only, no text, no panels, no grid, no split layout."
    )


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_comic_input.pop(chat_id, None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Health-check server (Replit keep-alive)
# ---------------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/logs":
            try:
                with _game_log_lock:
                    if os.path.exists(GAME_LOG_FILE):
                        with open(GAME_LOG_FILE, "r", encoding="utf-8") as f:
                            body = f.read().encode("utf-8")
                    else:
                        body = b""
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(exc).encode())
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check server running on port 8080")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in environment")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set in environment")

    start_health_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- Conversation: /create ---
    create_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_game_start, filters=filters.ChatType.PRIVATE)],
        states={
            WAITING_FOR_STYLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_style)
            ],
            WAITING_FOR_AGE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_age_confirm_host)
            ],
            WAITING_FOR_SOLO_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_solo_answer)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # /create always starts fresh even if stuck mid-conversation
        per_user=True,
        per_chat=True,
    )

    # --- Conversation: /play and deep-link /start ---
    play_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("play",  play_game, filters=filters.ChatType.PRIVATE),
            CommandHandler("start", start,     filters=filters.ChatType.PRIVATE),
        ],
        states={
            WAITING_FOR_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_answer)
            ],
            WAITING_FOR_PLAYER_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_player_age_confirm)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,  # /play or /start always re-enters even if stuck
        per_user=True,
        per_chat=True,
    )

    # Group 0 — conversation handlers
    app.add_handler(create_conv_handler)
    app.add_handler(play_conv_handler)

    # Callback query handlers for comic flow
    app.add_handler(CallbackQueryHandler(start_comic_callback,      pattern=r"^start_comic:"))
    app.add_handler(CallbackQueryHandler(set_comic_rounds_callback, pattern=r"^comic_rounds:"))
    app.add_handler(CallbackQueryHandler(skip_comic_scene_callback, pattern=r"^skip_scene:"))

    # Group 1 — catches plain text for comic turns (lower priority)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_comic_message),
        group=1,
    )

    # Global error handler — catches any unhandled exception from any handler
    # and notifies the user instead of silently dropping the update.
    async def _global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception(f"Unhandled exception: {context.error}", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=(
                        "⚠️ <b>An unexpected error occurred.</b>\n\n"
                        "This is usually a temporary network issue.\n\n"
                        "Type /cancel to reset, then /create to start a new game."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    app.add_error_handler(_global_error_handler)

    logger.info("Skazk.AI bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()
