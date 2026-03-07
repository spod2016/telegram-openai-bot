import logging
import random
import string
import base64
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

# ConversationHandler state
WAITING_FOR_ANSWER = 1

ROLES = ["WHO", "WHAT ARE THEY DOING", "WHERE", "MOOD", "TWIST"]
ROLE_QUESTIONS = [
    "🎭 You are the WHO. Who is the main character? (reply with a person or character)",
    "🎬 You are the ACTION. What are they doing? (reply with an action or activity)",
    "📍 You are the WHERE. Where does it happen? (reply with a location or place)",
    "🌫️ You are the MOOD. What is the atmosphere or tone? (reply with a mood or feeling, e.g. eerie, joyful, tense)",
    "🌀 You are the TWIST. Add an unexpected detail! (reply with something surprising or bizarre)",
]
GAME_TIMEOUT_MINUTES = 30


def generate_token(length=6) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


def is_game_expired(game: dict) -> bool:
    return datetime.utcnow() - game["created_at"] > timedelta(minutes=GAME_TIMEOUT_MINUTES)


async def create_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This bot only works in private chats.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /create N (where N is between 2 and 5)")
        return

    n = int(args[0])
    if n < 2 or n > 5:
        await update.message.reply_text("❌ Number of players must be between 2 and 5.")
        return

    token = generate_token()
    while token in games:
        token = generate_token()

    games[token] = {
        "token": token,
        "num_players": n,
        "answers": {},       # chat_id -> answer text
        "roles": {},         # chat_id -> role index
        "player_order": [],  # ordered list of chat_ids
        "created_at": datetime.utcnow(),
        "finished": False,
    }

    await update.message.reply_text(
        f"✅ Game created!\n\n"
        f"🔑 Token: <code>{token}</code>\n"
        f"👥 Players needed: {n}\n\n"
        f"Share this token with {n - 1} other player(s).\n"
        f"Everyone (including you) should use the command below.\n\n"
        f"⏳ This game expires in {GAME_TIMEOUT_MINUTES} minutes.",
        parse_mode="HTML",
    )
    await update.message.reply_text(f"/play {token}")


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
    image_prompt = (
        f"Comic book style illustration: {phrase}.{mood_part}{twist_part} "
        "Vibrant colors, bold outlines, action-packed, dynamic composition."
    )

    logger.info(f"Generating image for game {token}: {phrase}")

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
            response_format="b64_json"
        )
        # gpt-image-1 returns base64 by default
        image_b64 = response.data[0].b64_json
        image_data = base64.b64decode(image_b64)
    except Exception as e:
        logger.error(f"OpenAI image generation failed: {e}")
        error_msg = str(e)

    # Send result to all players
    result_text = (
        f"🎉 <b>Game complete!</b>\n\n"
        f"📖 <b>The story:</b>\n"
        f"<i>{phrase}</i>\n\n"
        f"🎭 WHO: {who}\n"
        f"🎬 ACTION: {action}\n"
        f"📍 WHERE: {where}"
        + (f"\n🌫️ MOOD: {mood}" if mood else "")
        + (f"\n🌀 TWIST: {twist}" if twist else "")
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

    # Clean up game after a delay (keep it for any late messages)
    # Games naturally expire via is_game_expired check


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def private_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ This bot only works in private chats.")


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in environment")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set in environment")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
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

    app.add_handler(CommandHandler("create", create_game, filters=filters.ChatType.PRIVATE))
    app.add_handler(conv_handler)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()