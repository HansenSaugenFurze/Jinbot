import os
import json
import logging
import asyncio
import random
from pathlib import Path
from typing import List, Optional, Dict
from collections import defaultdict, deque

from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from aiohttp import web

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Variables and Configurations
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_") or BOT_TOKEN == "":
    logger.error("❌ Please set your valid BOT_TOKEN in the TELEGRAM_TOKEN environment variable.")
    exit(1)

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip()  # Your app base URL, no trailing slash
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else None
PORT = int(os.getenv("PORT", "10000"))

MEME_DIR = Path(os.getenv("MEME_DIR", "memes"))
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # Properly defined here

RECENT_MEMES_MAX = 5  # Number of recent memes to avoid repeats
post_interval_minutes = 10

# Global variables
memes: List[Path] = []
current_index = 0
recent_memes = deque(maxlen=RECENT_MEMES_MAX)
like_tracker: Dict[str, Dict[int, str]] = defaultdict(dict)
likes_file = MEME_DIR / "likes.json"

group_id_file = Path("group_id.txt")
group_chat_id: Optional[int] = None

job = None  # For scheduled job handle


# Utility Functions
def load_memes():
    global memes
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    memes = sorted(
        [p for p in MEME_DIR.iterdir() if p.suffix.lower() in ALLOWED_EXT and p.is_file()]
    )
    logger.info(f"Loaded {len(memes)} meme(s).")


def load_likes():
    global like_tracker
    if likes_file.exists():
        try:
            with open(likes_file, "r") as f:
                data = json.load(f)
                # Convert string keys to int keys inside nested dicts
                like_tracker = defaultdict(
                    dict,
                    {
                        k: {int(user_id): emoji for user_id, emoji in v.items()}
                        for k, v in data.items()
                    },
                )
            logger.info("Loaded likes data.")
        except Exception as e:
            logger.warning(f"Failed to load likes data: {e}. Initializing empty likes.")
            like_tracker = defaultdict(dict)
    else:
        like_tracker = defaultdict(dict)


def save_likes():
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        with open(likes_file, "w") as f:
            json.dump(like_tracker, f)
        logger.info("Saved likes data.")
    except Exception as e:
        logger.error(f"Failed to save likes data: {e}")


def save_group_id(chat_id: int):
    try:
        with open(group_id_file, "w") as f:
            f.write(str(chat_id))
        logger.info(f"Saved group chat ID: {chat_id}")
    except Exception as e:
        logger.error(f"Failed to save group chat ID: {e}")


def load_group_id() -> Optional[int]:
    if group_id_file.exists():
        try:
            cid = int(group_id_file.read_text().strip())
            logger.info(f"Loaded saved group chat ID: {cid}")
            return cid
        except Exception as e:
            logger.warning(f"Failed to load group chat ID: {e}")
    return None


def next_meme(randomize=False) -> Optional[Path]:
    global current_index
    if not memes:
        return None
    if randomize:
        candidates = [m for m in memes if m not in recent_memes]
        if not candidates:
            recent_memes.clear()
            candidates = memes.copy()
        choice = random.choice(candidates)
    else:
        choice = memes[current_index % len(memes)]
        current_index += 1
    recent_memes.append(choice)
    return choice


def format_likes(likes: Dict[int, str]) -> str:
    counts = {"heart": 0, "love": 0, "haha": 0}
    for v in likes.values():
        if v in counts:
            counts[v] += 1
    parts = []
    if counts["heart"]:
        parts.append(f"❤️ {counts['heart']}")
    if counts["love"]:
        parts.append(f"🔥 {counts['love']}")
    if counts["haha"]:
        parts.append(f"😂 {counts['haha']}")
    return " | ".join(parts)


def build_keyboard(filename: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{filename}"),
        InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{filename}"),
        InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}"),
    ]
    return InlineKeyboardMarkup([buttons])


# Handlers
async def send_meme(chat_id: int, context: Optional[ContextTypes.DEFAULT_TYPE], randomize=False):
    meme_path = next_meme(randomize)
    if meme_path is None:
        if context:
            await context.bot.send_message(chat_id, "No memes found.")
        logger.warning("No memes to send.")
        return
    filename = meme_path.name
    likes_text = format_likes(like_tracker.get(filename, {}))
    caption = f"👍 {likes_text}" if likes_text else None

    try:
        with open(meme_path, "rb") as f:
            await context.bot.send_photo(
                chat_id,
                photo=InputFile(f, filename=filename),
                caption=caption,
                reply_markup=build_keyboard(filename),
            )
    except Exception:
        try:
            with open(meme_path, "rb") as f:
                await context.bot.send_document(
                    chat_id,
                    document=InputFile(f, filename=filename),
                    caption=caption,
                    reply_markup=build_keyboard(filename),
                )
        except Exception as e:
            logger.error(f"Failed to send meme {filename}: {e}")


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(update.effective_chat.id, "Bot is online and ready.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = data.split("|")
    except ValueError:
        return
    if query.from_user.id in like_tracker[filename]:
        # User already liked
        return
    like_tracker[filename][query.from_user.id] = emoji
    save_likes()

    likes_text = format_likes(like_tracker[filename])
    base_caption = query.message.caption.split("\n")[0] if query.message.caption else ""
    new_caption = f"{base_caption}\n\n👍 {likes_text}" if likes_text else base_caption
    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=build_keyboard(filename))
    except Exception as e:
        logger.warning(f"Failed to edit message caption: {e}")


async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    if group_chat_id is None:
        logger.warning("Group chat ID not set. Skipping scheduled post.")
        return
    await send_meme(group_chat_id, context)


async def set_interval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global post_interval_minutes, job
    chat_id = update.effective_chat.id

    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await context.bot.send_message(chat_id, "You must be administrator to set interval.")
        return

    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return

    minutes = int(context.args[0])
    if not (1 <= minutes <= 60):
        await context.bot.send_message(chat_id, "Interval must be between 1 and 60 minutes.")
        return

    post_interval_minutes = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(
        scheduled_post,
        interval=post_interval_minutes * 60,
        first=5,
    )
    await context.bot.send_message(chat_id, f"Posting interval set to {post_interval_minutes} minutes.")


async def add_meme_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.message

    if not msg or not msg.reply_to_message:
        await context.bot.send_message(chat_id, "Reply to a photo or document with /add to add a meme.")
        return

    replied = msg.reply_to_message
    file = None
    filename = None

    if replied.photo:
        file = await replied.photo[-1].get_file()
        filename = f"{file.file_id}.jpg"
    elif replied.document and replied.document.file_name and Path(replied.document.file_name).suffix.lower() in ALLOWED_EXT:
        file = await replied.document.get_file()
        filename = replied.document.file_name
    else:
        await context.bot.send_message(chat_id, "Unsupported file type. Please reply with an image or supported document.")
        return

    save_path = MEME_DIR / filename
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id, "Meme added successfully.")


async def detect_and_save_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global group_chat_id, job
    chat = update.effective_chat
    if group_chat_id is None and chat.type in ("group", "supergroup"):
        group_chat_id = chat.id
        save_group_id(group_chat_id)
        logger.info(f"Detected and saved group chat id: {group_chat_id}")

        if job is None:
            job = context.job_queue.run_repeating(
                scheduled_post,
                interval=post_interval_minutes * 60,
                first=10,
            )
            logger.info("Started scheduled job after group id detected.")


async def get_group_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await context.bot.send_message(chat.id, f"Group chat ID: {chat.id}")
    global group_chat_id
    if group_chat_id is None and chat.type in ("group", "supergroup"):
        group_chat_id = chat.id
        save_group_id(group_chat_id)


async def init_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await context.bot.send_message(chat.id, "This command must be used in a group.")
        return
    global group_chat_id
    group_chat_id = chat.id
    save_group_id(group_chat_id)
    await context.bot.send_message(chat.id, f"Group chat initialized with ID: {group_chat_id}")


async def webhook_handler(request: web.Request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Error handling update: {e}")
    return web.Response(text="OK")


async def health_check(request: web.Request):
    return web.Response(text="Bot is healthy.")


async def send_meme_http(request: web.Request):
    app = request.app["bot_app"]
    if group_chat_id is None:
        return web.Response(status=400, text="Group chat ID not set.")
    await send_meme(group_chat_id, None)
    return web.Response(text="Sent meme.")


async def main():
    global group_chat_id, job

    # Setup
    load_memes()
    load_likes()

    group_chat_id = load_group_id()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_and_save_id))
    app.add_handler(CommandHandler("setinterval", set_interval_handler))
    app.add_handler(CommandHandler("add", add_meme_handler))
    app.add_handler(CommandHandler("getgroupid", get_group_id_handler))
    app.add_handler(CommandHandler("init_group", init_group_handler))

    if group_chat_id:
        job = app.job_queue.run_repeating(scheduled_post, interval=post_interval_minutes * 60)
        logger.info("Scheduled posting job started.")

    # Web server setup
    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_check),
        web.post(WEBHOOK_PATH, webhook_handler),
        web.get("/send_meme", send_meme_http),
    ])
    web_app["bot_app"] = app

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

    # Webhook setup
    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        logger.error("WEBHOOK_URL not set. Please set WEBHOOK_BASE environment variable without trailing slash.")

    await app.initialize()
    await app.start()
    logger.info("Bot started.")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Shutting down bot.")

    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
