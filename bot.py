import os
import logging
import asyncio
import time
from collections import defaultdict
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, ReactionTypeEmoji
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.error import TelegramError, RetryAfter, TimedOut, NetworkError

from google import genai

# Load environment variables
load_dotenv()

now = time.monotonic()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)

# Model fallback list
MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

LIFEMAXING_GROUP_ID = -1003682955071

TIP_TOPICS = {
    0: "Fitness",
    1: "Nutrition",
    2: "Mobility",
    3: "Posture",
    4: "Sleep",
    5: "Skincare",
    6: "Healthy Habits"
}

# Rate limiting: track request timestamps per user
RATE_LIMIT = 10           # max requests
RATE_WINDOW = 60          # per 60 seconds
user_request_log: dict[int, list[float]] = defaultdict(list)

PROMPT_TEMPLATE = """
You are Max, the official AI assistant for the LIFEMAXING community.

Your expertise includes:
- Fitness
- Exercise
- Mobility
- Posture
- Nutrition
- Diet
- Skincare
- Sleep
- Healthy habits

Guidelines:
- Be friendly and practical.
- Give evidence-based advice.
- Avoid bro-science.
- Do not diagnose medical conditions. instead, tell the user to seek professional help.
- Keep answers concise unless the user asks for details.
- Only greet or introduce yourself when the user sends a greeting (e.g. "hi", "hello", "hey").
- Do not introduce yourself if the user greets you with a question or statement.
- For all other messages, jump straight into the answer without any preamble or introduction.

Formatting Rules:
- Use emojis sparingly.
- Do NOT use Markdown syntax
- Use short paragraphs.
- Use uppercase for headings.
- Keep answers mobile-friendly, specifically for telegram.
- Never write walls of text.
- When giving exercises, provide:
  • Exercise name
  • Sets and reps
  • Form tips

User Question:
{question}
"""

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def daily_tip(context: ContextTypes.DEFAULT_TYPE):

    today = datetime.now().weekday()

    topic = TIP_TOPICS[today]

    prompt = f"""
Generate ONE practical daily tip about {topic}.

Requirements:
- Under 100 words
- Evidence-based
- Actionable
- Mobile-friendly
- Suitable for Telegram
- Do NOT use markdown
- Do NOT use HTML
- Use short paragraphs

Start with:

💪 LIFEMAXING DAILY TIP
"""

    tip = await ask_gemini(prompt)

    await context.bot.send_message(
        chat_id=LIFEMAXING_GROUP_ID,
        text=tip
    )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def is_rate_limited(user_id: int) -> tuple[bool, int]:
    """
    Returns (is_limited, seconds_until_reset).
    Cleans up old timestamps outside the current window before checking.
    """
    now = time.monotonic()
    timestamps = user_request_log[user_id]

    # Drop timestamps older than the window
    user_request_log[user_id] = [t for t in timestamps if now - t < RATE_WINDOW]

    if len(user_request_log[user_id]) >= RATE_LIMIT:
        oldest = user_request_log[user_id][0]
        wait = int(RATE_WINDOW - (now - oldest)) + 1
        return True, wait

    # Log this request
    user_request_log[user_id].append(now)
    return False, 0


# ---------------------------------------------------------------------------
# Gemini with model fallback
# ---------------------------------------------------------------------------

async def ask_gemini(question: str) -> str:
    """Try each model in order, return first successful response."""
    prompt = PROMPT_TEMPLATE.format(question=question)
    last_error = None

    for model in MODELS:
        try:
            logger.info(f"Trying model: {model}")
            response = client.models.generate_content(
                model=model,
                contents=prompt
            )
            logger.info(f"Success with model: {model}")
            return response.text

        except Exception as e:
            logger.warning(f"Model {model} failed: {e}")
            last_error = e
            continue

    logger.error(f"All models failed. Last error: {last_error}")
    return "Sorry, I'm having trouble thinking right now. Try again later."


# ---------------------------------------------------------------------------
# Thinking message helper (private chat)
# ---------------------------------------------------------------------------

async def send_thinking_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Send a 'Thinking...' message and return it so we can delete it later."""
    try:
        return await context.bot.send_message(
            chat_id=chat_id,
            text="🤔 Thinking . . ."
        )
    except TelegramError as e:
        logger.warning(f"Could not send thinking message: {e}")
        return None


async def delete_message_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Delete a message, silently ignoring errors (already deleted, no permission, etc.)."""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as e:
        logger.warning(f"Could not delete message {message_id}: {e}")


# ---------------------------------------------------------------------------
# Typing indicator loop (private chat)
# ---------------------------------------------------------------------------

async def send_typing_while_waiting(chat_id: int, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    """Keep typing indicator alive every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError as e:
            logger.warning(f"Failed to send typing action: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Response fetchers (private vs group)
# ---------------------------------------------------------------------------

async def get_response_with_typing(chat_id: int, question: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Private chat: show typing indicator + thinking message while waiting."""
    stop_event = asyncio.Event()

    thinking_msg = await send_thinking_message(chat_id, context)

    typing_task = asyncio.create_task(
        send_typing_while_waiting(chat_id, context, stop_event)
    )

    try:
        response = await ask_gemini(question)
    finally:
        stop_event.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        if thinking_msg:
            await delete_message_safe(context, chat_id, thinking_msg.message_id)

    return response


async def get_response_with_reaction(message, question: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Group chat: react 👍 + thinking message while waiting, clean up after."""
    # Add 👍 reaction
    try:
        await context.bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")]
        )
    except TelegramError as e:
        logger.warning(f"Failed to set reaction: {e}")

    # Send thinking message in group
    thinking_msg = await send_thinking_message(message.chat_id, context)

    try:
        response = await ask_gemini(question)
    finally:
        # Remove reaction
        try:
            await context.bot.set_message_reaction(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reaction=[]
            )
        except TelegramError as e:
            logger.warning(f"Failed to remove reaction: {e}")

        # Delete thinking message
        if thinking_msg:
            await delete_message_safe(context, message.chat_id, thinking_msg.message_id)

    return response


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 Hi! I'm Max.\n\n"
        "The AI assistant for the LIFEMAXING community.\n\n"
        "Ask me anything about fitness, nutrition, posture, mobility, skincare, and healthy habits.\n\n"
        "Type /help to learn more."
    )

async def testtip(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "💪 Today's tip is on the way..."
    )

    await daily_tip(context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 MAX HELP\n\n"
        
        "ABOUT MAX\n"
        "I'm the official AI assistant of the LIFEMAXING community.\n\n"
        
        "I can help with:\n"
        "• Fitness\n"
        "• Nutrition\n"
        "• Mobility\n"
        "• Posture\n"
        "• Skincare\n"
        "• Healthy habits\n\n"
        
        "HOW TO USE ME\n"
        "Private Chat:\n"
        "• Simply send your question.\n\n"
        
        "Group Chat:\n"
        "• Mention @LifemaxingAI_bot\n"
        "• Or reply to one of my messages.\n\n"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.text:
        return

    text = message.text
    chat_id = message.chat_id

    # Ignore messages from other bots (but allow anonymous admins)
    if message.from_user is None:
        # No from_user = anonymous admin post, allow it
        user_id = message.sender_chat.id  # use group ID as user_id for rate limiting
    elif message.from_user.is_bot:
        if message.sender_chat and message.sender_chat.id == chat_id:
            user_id = message.sender_chat.id  # anonymous admin, allow
        else:
            logger.info(f"Ignored message from bot: {message.from_user.username}")
            return
    else:
        user_id = message.from_user.id

    # Rate limiting check
    limited, wait_seconds = is_rate_limited(user_id)
    if limited:
        await message.reply_text(
            f"⏳ I'm processing a lot of queries right now!\n\nPlease wait {wait_seconds} seconds before sending another message."
        )
        return

    # Private chat
    if message.chat.type == "private":
        response = await get_response_with_typing(chat_id, text, context)
        await message.reply_text(response)
        return

    # Group or supergroup
    bot_username = context.bot.username

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )

    mention = f"@{bot_username}"
    is_mentioned = mention.lower() in text.lower()

    if not is_reply_to_bot and not is_mentioned:
        return

    if is_mentioned:
        text = text.replace(mention, "").strip()

    if not text:
        await message.reply_text(
            "💪 Hi! Ask me anything about fitness, nutrition, posture, sleep, skincare, or healthy habits."
        )
        return

    response = await get_response_with_reaction(message, text, context)
    await message.reply_text(response)


# ---------------------------------------------------------------------------
# Crash / error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs all exceptions, handles known Telegram errors gracefully."""
    error = context.error

    if isinstance(error, RetryAfter):
        # Telegram flood control — wait and note it
        logger.warning(f"Telegram flood control hit. Retry after {error.retry_after}s.")
        return

    if isinstance(error, TimedOut):
        logger.warning("Request to Telegram timed out. Will retry automatically.")
        return

    if isinstance(error, NetworkError):
        logger.warning(f"Network error: {error}. Telegram will retry.")
        return

    if isinstance(error, TelegramError):
        logger.error(f"Telegram error: {error}")
        return

    # Unexpected exceptions — log full traceback
    logger.exception(f"Unexpected error while handling update: {update}", exc_info=error)

    # Optionally notify the user something went wrong
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my end. Please try again in a moment."
            )
        except TelegramError:
            pass  # Don't crash the error handler itself


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testtip", testtip))
    
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # Register global error handler
    app.add_error_handler(error_handler)

    job_queue = app.job_queue

    job_queue.run_daily(
        daily_tip,
        time=dt_time(
            hour=6,
            minute=0,
            tzinfo=ZoneInfo("Asia/Kolkata")
        )
    )    

    logger.info("💪 Max is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True   # ignore messages sent while bot was offline
    )


if __name__ == "__main__":
    main()
