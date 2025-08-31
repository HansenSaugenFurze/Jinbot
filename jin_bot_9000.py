import random
import json
from pathlib import Path
from typing import List, Optional, Dict
from collections import defaultdict, deque
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import os
import asyncio
from aiohttp import web
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_") or BOT_TOKEN == "":
    logger.error("❌ Please set your BOT_TOKEN in the TELEGRAM_TOKEN environment variable!")
    exit()

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL_BASE = os.getenv("WEBHOOK_URL")
WEBHOOK_URL = f"{WEBHOOK_URL_BASE}{WEBHOOK_PATH}" if WEBHOOK_URL_BASE else None

MEME_DIR: Path = Path(os.getenv("MEME_DIR", "memes"))
MEME_FILES: List[Path] = []
CURRENT_INDEX: int = 0
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MEMES_MAX = 5
recent_memes: deque = deque(maxlen=RECENT_MEMES_MAX)  # Fixed typo here
post_interval_minutes = 10
job = None

LIKE_TRACKER: Dict[str, Dict[int, str]] = defaultdict(dict)
LIKES_FILE = MEME_DIR / "likes.json"

GROUP_ID_FILE = Path("group_id.txt")
GROUP_CHAT_ID: Optional[int] = None

GROUP_ID_ENV = os.getenv("GROUP_ID")
if GROUP_ID_ENV:
    try:
        GROUP_CHAT_ID = int(GROUP_ID_ENV)
        logger.info(f"Loaded group chat ID from environment: {GROUP_CHAT_ID}")
    except Exception:
        logger.warning("Invalid GROUP_ID environment variable; ignoring.")


def load_memes() -> None:
    global MEME_FILES
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    MEME_FILES = sorted(p for p in MEME_DIR.glob("*") if p.suffix.lower() in ALLOWED_EXT)


def save_likes() -> None:
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIKES_FILE, "w") as f:
            json.dump(dict(LIKE_TRACKER), f)
    except Exception as e:
        logger.error(f"Failed to save likes: {e}")


def load_likes() -> None:
    global LIKE_TRACKER
    if LIKES_FILE.exists():
        try:
            with open(LIKES_FILE, "r") as f:
                data = json.load(f)
                LIKE_TRACKER = defaultdict(dict, {k: {int(user): v for user, v in val.items()} for k, val in data.items()})
        except Exception as e:
            logger.warning(f"Failed to load likes, starting fresh: {e}")
            LIKE_TRACKER = defaultdict(dict)
    else:
        LIKE_TRACKER = defaultdict(dict)


def save_group_id(group_id: int) -> None:
    try:
        with open(GROUP_ID_FILE, "w") as f:
            f.write(str(group_id))
            logger.info(f"Saved group chat ID: {group_id}")
    except Exception as e:
        logger.error(f"Failed to save group chat ID: {e}")


def load_group_id() -> Optional[int]:
    if GROUP_ID_FILE.exists():
        try:
            return int(GROUP_ID_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to load group chat ID: {e}")
    return None


def next_meme(randomize: bool = False) -> Optional[Path]:
    global CURRENT_INDEX
    if not MEME_FILES:
        return None
    if not randomize:
        meme = MEME_FILES[CURRENT_INDEX % len(MEME_FILES)]
        CURRENT_INDEX = (CURRENT_INDEX + 1) % len(MEME_FILES)
        recent_memes.append(meme)
        return meme
    available = [m for m in MEME_FILES if m not in recent_memes]
    if not available:
        recent_memes.clear()
        available = MEME_FILES[:]
    meme = random.choice(available)
    recent_memes.append(meme)
    return meme


def create_keyboard(filename: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{filename}"),
            InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{filename}"),
            InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def format_likes(likes: Dict[int, str]) -> str:
    counts = {"heart": 0, "love": 0, "haha": 0}
    for emoji in likes.values():
        if emoji in counts:
            counts[emoji] += 1
    parts = []
    if counts["heart"]:
        parts.append(f"❤️ {counts['heart']}")
    if counts["love"]:
        parts.append(f"🔥 {counts['love']}")
    if counts["haha"]:
        parts.append(f"😂 {counts['haha']}")
    return " | ".join(parts)


async def send_meme(chat_id: int, context: ContextTypes.DEFAULT_TYPE, randomize: bool = False) -> None:
    meme_path = next_meme(randomize)
    if not meme_path:
        await context.bot.send_message(chat_id, "❌ No memes found!")
        return
    filename = meme_path.name
    likes_text = format_likes(LIKE_TRACKER[filename])
    caption = f"👍 {likes_text}" if likes_text else None

    try:
        with open(meme_path, "rb") as f:
            await context.bot.send_photo(
                chat_id,
                photo=InputFile(f, filename=filename),
                caption=caption,
                reply_markup=create_keyboard(filename),
            )
    except Exception:
        try:
            with open(meme_path, "rb") as f:
                await context.bot.send_document(
                    chat_id,
                    document=InputFile(f, filename=filename),
                    caption=caption,
                    reply_markup=create_keyboard(filename),
                )
        except Exception:
            pass


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data
    if not data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = data.split("|")
    except ValueError:
        return
    user_id = query.from_user.id
    if user_id in LIKE_TRACKER[filename]:
        return
    LIKE_TRACKER[filename][user_id] = emoji
    save_likes()
    likes_text = format_likes(LIKE_TRACKER[filename])
    caption_base = query.message.caption.split("\n")[0] if query.message.caption else ""
    new_caption = f"{caption_base}\n\n👍 {likes_text}" if likes_text else caption_base
    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=create_keyboard(filename))
    except Exception:
        pass


async def scheduled_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None:
        logger.warning("Group chat ID not set, skipping scheduled post.")
        return
    await send_meme(GROUP_CHAT_ID, context)


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global post_interval_minutes, job
    chat_id = update.effective_chat.id

    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if member.status not in ('administrator', 'creator'):
        await context.bot.send_message(chat_id, "❌ Only admins can set the interval.")
        return

    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return

    minutes = int(context.args[0])
    if not 1 <= minutes <= 60:
        await context.bot.send_message(chat_id, "Interval must be between 1 and 60 minutes.")
        return

    post_interval_minutes = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_post, interval=post_interval_minutes * 60)
    await context.bot.send_message(chat_id, f"Posting interval set to {post_interval_minutes} minutes.")


async def add_meme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id, "Reply to a photo or document with /add to add a meme.")
        return
    replied = update.message.reply_to_message

    file = None
    filename = None
    if replied.photo:
        file = await replied.photo[-1].get_file()
        filename = f"{file.file_id}.jpg"
    elif replied.document and replied.document.file_name and Path(replied.document.file_name).suffix.lower() in ALLOWED_EXT:
        file = await replied.document.get_file()
        filename = replied.document.file_name
    else:
        await context.bot.send_message(chat_id, "Unsupported file type. Please reply with an image.")
        return

    save_path = MEME_DIR / filename
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id, "✅ Meme added!")


async def detect_and_save_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID, job
    chat = update.effective_chat
    if GROUP_CHAT_ID is None and chat.type in ('group', 'supergroup'):
        GROUP_CHAT_ID = chat.id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Saved group chat ID: {GROUP_CHAT_ID}")

        if job is None:
            job = context.job_queue.run_repeating(
                scheduled_post,
                interval=post_interval_minutes * 60,
                first=5
            )
            logger.info("Scheduled posting job started after group ID detection.")


async def get_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await context.bot.send_message(chat.id, f"Group chat ID: {chat.id}")
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None and chat.type in ('group', 'supergroup'):
        GROUP_CHAT_ID = chat.id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Saved group chat ID: {GROUP_CHAT_ID}")


async def init_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ('group', 'supergroup'):
        await context.bot.send_message(chat.id, "This command can only be used in a group.")
        return
    global GROUP_CHAT_ID
    GROUP_CHAT_ID = chat.id
    save_group_id(GROUP_CHAT_ID)
    await context.bot.send_message(chat.id, f"Group chat ID initialized: {GROUP_CHAT_ID}")
    logger.info(f"Group chat ID manually initialized: {GROUP_CHAT_ID}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id, "🤖 Jin_Bot_9000 is online and ready!")


async def webhook_handler(request: web.Request) -> web.Response:
    app = request.app['bot_app']
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}")
    return web.Response(text="OK")


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="Bot is alive.")


async def send_meme_http(request: web.Request) -> web.Response:
    app = request.app['bot_app']
    if GROUP_CHAT_ID is None:
        return web.Response(status=400, text="Group chat ID not set")
    await send_meme(GROUP_CHAT_ID, None)
    return web.Response(text="Sent meme.")


async def main() -> None:
    global job
    load_memes()
    load_likes()
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None:
        GROUP_CHAT_ID = load_group_id()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, lambda u, c: logger.info(f"Update: {u}")), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.GROUP | filters.ChatType.SUPERGROUP, detect_and_save_group_id))
    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("add", add_meme))
    app.add_handler(CommandHandler("getgroupid", get_group_id))
    app.add_handler(CommandHandler("initgroup", init_group))
    app.add_handler(CallbackQueryHandler(handle_button_press))

    if GROUP_CHAT_ID is not None:
        job = app.job_queue.run_repeating(scheduled_post, interval=post_interval_minutes*60)
        logger.info("Started scheduled posting job")

    webapp = web.Application()
    webapp.add_routes([
        web.get("/", health_check),
        web.post(WEBHOOK_PATH, webhook_handler),
        web.get("/send_meme", send_meme_http),
    ])
    webapp['bot_app'] = app

    runner = web.AppRunner(webapp)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        logger.error("WEBHOOK_URL not set")

    await app.initialize()
    await app.start()

    logger.info("Bot started")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Shutting down bot")
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
