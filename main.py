import logging
import random
import string
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
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

# Global game state
games: dict = {}

# ConversationHandler states
WAITING_FOR_STYLE = 1
WAITING_FOR_ANSWER = 2

ROLES = ["WHO", "WHAT ARE THEY DOING", "WHERE", "MOOD", "TWIST"]
ROLE_QUESTIONS = [
    "🎭 You are the WHO. Who is the main character? (reply with a person or character)",
    "🎬 You are the ACTION. What are they doing? (reply with an action or activity)",
    "📍 You are the WHERE. Where does it happen? (reply with a location or place)",
    "🌫️ You are the MOOD. What is the atmosphere or tone? (reply with a mood or feeling, e.g. eerie, joyful, tense)",
    "🌀 You are the TWIST. Add an unexpected detail! (reply with something surprising or bizarre)",
]

STYLES = [
    ("Comic Book", "comic book style, vibrant colors, bold outlines, halftone dots, action panels"),
    ("Watercolor", "soft watercolor illustration, pastel tones, flowing washes, delicate brushstrokes"),
    ("Pixel Art", "retro pixel art, 16-bit style, chunky pixels, game sprite aesthetic"),
    ("Oil Painting", "classical oil painting, rich textures, dramatic lighting, old masters style"),
    ("Anime", "anime style, clean linework, expressive characters, colorful cel shading"),
    ("Noir", "black and white noir, high contrast, dramatic shadows, film noir atmosphere"),
    ("Surrealist", "surrealist dreamscape, Salvador Dali inspired, melting reality, bizarre imagery"),
    ("Cyberpunk", "cyberpunk aesthetic, neon lights, dark dystopian city, futuristic glowing elements"),
    ("Children's Book", "children's book illustration, cute, warm, simple shapes, friendly characters"),
    ("Renaissance", "Renaissance painting style, classical composition, chiaroscuro lighting, museum quality"),
]

GAME_TIMEOUT_MINUTES = 30


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


async def create_game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This bot only works in private chats.")
        return ConversationHandler.END

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /create N (where N is between 2 and 5)")
        return ConversationHandler.END

    n = int(args[0])
    if n < 2 or n > 5:
        await update.message.reply_text("❌ Number of players must be between 2 and 5.")
        return ConversationHandler.END

    context.user_data["pending_num_players"] = n

    await update.message.reply_text(build_style_menu(), parse_mode="HTML")
    return WAITING_FOR_STYLE


async def receive_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit() or not (1 <= int(text) <= 10):
        await update.message.reply_text("⚠️ Please reply with a number between 1 and 10.")
        return WAITING_FOR_STYLE

    style_index = int(text) - 1
    style_name, style_prompt = STYLES[style_index]
    n = context.user_data.get("pending_num_players", 2)

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
        "style_name": style_name,
        "style_prompt": style_prompt,
    }

    await update.message.reply_text(
        f"✅ Game created!\n\n"
        f"🔑 Token: <code>{token}</code>\n"
        f"👥 Players needed: {n}\n"
        f"🎨 Style: <b>{style_name}</b>\n\n"
        f"Share this token with {n - 1} other player(s).\n"
        f"Everyone (including you) should use the command below.\n\n"
        f"⏳ This game expires in {GAME_TIMEOUT_MINUTES} minutes.",
        parse_mode="HTML",
    )
    await update.message.reply_text(f"/play {token}")
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
        await update.message.reply_text("🚫 Game is complete. No more players can join.")
        return ConversationHandler.END

    # Assign role
    role_index = len(game["player_order"])
    game["player_order"].append(chat_id)
    game["roles"][chat_id] = role_index

    # Store player display name
    user = update.effective_user
    if user.username:
        display_name = f"@{user.username}"
    else:
        display_name = user.first_name
        if user.last_name:
            display_name += f" {user.last_name}"
    game["player_names"][chat_id] = display_name

    # Store token in user_data for the conversation
    context.user_data["current_token"] = token

    await update.message.reply_text(
        f"🎮 You joined game <code>{token}</code>!\n\n"
        f"Your role: <b>{ROLES[role_index]}</b>\n\n"
        f"{ROLE_QUESTIONS[role_index]}",
        parse_mode="HTML",
    )

    return WAITING_FOR_ANSWER


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

    # Check if all players have answered
    if len(game["answers"]) == game["num_players"]:
        await finalize_game(context, token)

    return ConversationHandler.END


async def finalize_game(context: ContextTypes.DEFAULT_TYPE, token: str):
    game = games[token]
    game["finished"] = True

    # Build prompt in role order
    ordered_answers = []
    for chat_id in game["player_order"]:
        ordered_answers.append(game["answers"][chat_id])

    who = ordered_answers[0]
    action = ordered_answers[1] if len(ordered_answers) > 1 else "doing something"
    where = ordered_answers[2] if len(ordered_answers) > 2 else "somewhere"
    mood = ordered_answers[3] if len(ordered_answers) > 3 else None
    twist = ordered_answers[4] if len(ordered_answers) > 4 else None

    phrase = f"{who} is {action} in {where}"
    if mood:
        phrase += f", with a {mood} atmosphere"
    if twist:
        phrase += f", but {twist}"

    mood_part = f" Mood: {mood}." if mood else ""
    twist_part = f" Unexpected twist: {twist}." if twist else ""
    style_prompt = game.get("style_prompt", STYLES[0][1])
    style_name = game.get("style_name", STYLES[0][0])

    image_prompt = (
        f"{phrase}.{mood_part}{twist_part} "
        f"{style_prompt}."
    )

    logger.info(f"Generating image for game {token} [{style_name}]: {phrase}")

    # Generate image with OpenAI
    image_data = None
    error_msg = None
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.images.generate(
            model="dall-e-3",
            prompt=image_prompt,
            n=1,
            size="1024x1024",
            response_format="b64_json",
        )
        image_b64 = response.data[0].b64_json
        image_data = base64.b64decode(image_b64)
    except Exception as e:
        logger.error(f"OpenAI image generation failed: {e}")
        error_msg = str(e)

    # Build name lookup
    names = game.get("player_names", {})
    player_ids = game["player_order"]

    def name(index):
        if index < len(player_ids):
            return f" ({names.get(player_ids[index], 'Player')})"
        return ""

    # Send result to all players
    result_text = (
        f"🎉 <b>Game complete!</b>\n\n"
        f"🎨 Style: <b>{style_name}</b>\n\n"
        f"📖 <b>The story:</b>\n"
        f"<i>{phrase}</i>\n\n"
        f"🎭 WHO: {who}{name(0)}\n"
        f"🎬 ACTION: {action}{name(1)}\n"
        f"📍 WHERE: {where}{name(2)}"
        + (f"\n🌫️ MOOD: {mood}{name(3)}" if mood else "")
        + (f"\n🌀 TWIST: {twist}{name(4)}" if twist else "")
    )

    for chat_id in game["player_order"]:
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
                    text=result_text + f"\n\n⚠️ Image generation failed: {error_msg}",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to send result to {chat_id}: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


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


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in environment")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set in environment")

    start_health_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    create_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_game_start, filters=filters.ChatType.PRIVATE)],
        states={
            WAITING_FOR_STYLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_style)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    play_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("play", play_game, filters=filters.ChatType.PRIVATE)],
        states={
            WAITING_FOR_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_answer)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(create_conv_handler)
    app.add_handler(play_conv_handler)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
