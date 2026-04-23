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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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

GAME_TIMEOUT_MINUTES = 30
STRIP_LINE_H = 18          # pixel height per text line in comic strip captions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_token(length=6) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


def is_game_expired(game: dict) -> bool:
    return datetime.utcnow() - game["created_at"] > timedelta(minutes=GAME_TIMEOUT_MINUTES)


def build_style_menu() -> str:
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
    await update.message.reply_text(build_style_menu(), parse_mode="HTML")
    return WAITING_FOR_STYLE


async def receive_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit() or not (1 <= int(text) <= 10):
        await update.message.reply_text("❌ Please reply with a number between 1 and 10.")
        return WAITING_FOR_STYLE

    style_index = int(text) - 1
    style_name, style_prompt = STYLES[style_index]
    n = context.user_data["pending_num_players"]

    # Solo mode — auto-join and start asking questions immediately
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
            "solo_answers": [],   # ordered list of all 5 answers
        }

        _join_game(games[token], token, chat_id, update.effective_user, context)

        await update.message.reply_text(
            f"🎮 <b>Solo game started!</b>\n"
            f"🎨 Style: <b>{style_name}</b>\n\n"
            f"You'll answer all 5 questions yourself. Let's go!\n\n"
            f"{ROLE_QUESTIONS[0]}",
            parse_mode="HTML",
        )
        return WAITING_FOR_SOLO_ANSWER

    # Multiplayer — show invite links as before
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
    }

    bot_username = context.bot.username
    deep_link = f"https://t.me/{bot_username}?start={token}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text="📤 Share game invite",
            url=f"https://t.me/share/url?text=Let%27s%20make%20a%20comic%20together%21%20Join%20my%20Skazk.AI%20game%20%E2%80%94%20takes%20just%20minutes%2C%20guaranteed%20to%20be%20ridiculous%20%F0%9F%91%87&url={deep_link}",
        )],
        [InlineKeyboardButton(
            text="▶️ Join this game yourself",
            url=deep_link,
        )],
    ])

    await update.message.reply_text(
        f"✅ Game created!\n\n"
        f"🔑 Token: <code>{token}</code>\n"
        f"👥 Players needed: {n}\n"
        f"🎨 Style: <b>{style_name}</b>\n\n"
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

    role_index = _join_game(game, token, chat_id, update.effective_user, context)

    await update.message.reply_text(
        f"🎮 You joined game <code>{token}</code>!\n\n"
        f"Your role: <b>{ROLES[role_index]}</b>\n\n"
        f"{ROLE_QUESTIONS[role_index]}",
        parse_mode="HTML",
    )
    return WAITING_FOR_ANSWER


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
        await finalize_game(context, token)

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
        await finalize_game(context, token)
        return ConversationHandler.END


# ---------------------------------------------------------------------------
# Finalize regular game → generate image → offer comic mode
# ---------------------------------------------------------------------------

async def finalize_game(context: ContextTypes.DEFAULT_TYPE, token: str):
    game = games[token]
    game["finished"] = True

    ordered_answers = (
        game["solo_answers"]
        if game.get("num_players") == 1
        else [game["answers"][cid] for cid in game["player_order"]]
    )

    who    = ordered_answers[0]
    action = ordered_answers[1] if len(ordered_answers) > 1 else "doing something"
    where  = ordered_answers[2] if len(ordered_answers) > 2 else "somewhere"
    mood   = ordered_answers[3] if len(ordered_answers) > 3 else None
    twist  = ordered_answers[4] if len(ordered_answers) > 4 else None

    phrase = f"{who} is {action} in {where}"
    if mood:
        phrase += f", with a {mood} atmosphere"
    if twist:
        phrase += f", but {twist}"

    # Store for comic continuity
    game["original_phrase"] = phrase

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
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        result_text
                        + "\n\n❌ <b>Image generation failed — the game cannot continue.</b>\n"
                        + (error_msg or "Unknown error.")
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

    # Carry the initial image forward so compile_comic can include it
    comic_sessions[token]["initial_image_data"] = game.get("initial_image_data")

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
    is_retry   = comic.get("retry_player") == chat_id  # True if this is their second attempt

    await update.message.reply_text(f"✅ Got it! Generating panel {panel_num}… 🎨 (this takes ~15 seconds)")

    # Generate panel image
    prompt   = _build_panel_prompt(comic, scene_text, panel_num)
    fallback = _build_panel_fallback_prompt(comic, scene_text)
    image_data, image_error = await _generate_image(
        prompt,
        label=f"panel {panel_num} of game {token}",
        fallback_prompt=fallback,
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
                retry_msg = (
                    f"⚠️ <b>Image generation failed.</b>\n"
                    f"{image_error or 'Unknown error.'}\n\n"
                    f"Please try again with a different description.\n"
                    f"<i>If it fails again, your turn will be skipped automatically.</i>"
                )
            await update.message.reply_text(retry_msg, parse_mode="HTML")
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

    # Store panel
    comic["panels"].append({
        "author_id":  chat_id,
        "prompt":     scene_text,
        "image_data": image_data,
        "skipped":    False,
    })

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
                await context.bot.send_message(
                    chat_id=pid,
                    text=(
                        panel_caption
                        + "\n\n⚠️ <b>Image generation failed for this panel.</b>\n"
                        + (image_error or "Unknown error.")
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

    # Clean up session
    comic_sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Comic strip compositor
# ---------------------------------------------------------------------------

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
            orig_img = Image.open(io.BytesIO(initial_image_data)).convert("RGB")
            orig_img = _resize_cover(orig_img, ow, ORIGIN_IMG_H)
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

            # Panel image
            pimg = Image.open(io.BytesIO(panel["image_data"])).convert("RGB")
            pimg = pimg.resize((PANEL_SIZE, PANEL_SIZE), Image.LANCZOS)
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
# Shared image generation helper
# ---------------------------------------------------------------------------

async def _generate_image(
    prompt: str,
    label: str = "",
    fallback_prompt: str | None = None,
) -> tuple[bytes | None, str | None]:
    """Generate an image. Retry once with fallback_prompt on content-policy error."""
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
            WAITING_FOR_SOLO_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_solo_answer)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
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

    logger.info("Skazk.AI bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()
