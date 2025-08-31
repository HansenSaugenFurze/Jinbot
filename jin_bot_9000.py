import os
import json
import logging
import asyncio
import random
from pathlib import Path
from typing import Dict, Optional, List
from collections import defaultdict, deque

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from aiohttp import web

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config and Constants
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
    logger.error("❌ Please set a valid BOT_TOKEN in TELEGRAM_TOKEN env variable.")
    exit(1)

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").strip()  # e.g. https://yourapp.onrender.com
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else None
PORT = int(os.getenv("PORT", "10000"))

MEME_DIR = Path(os.getenv("MEME_DIR", "memes"))
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MEMES_MAX = 5

# Globals
memes: List[Path] = []
current_index = 0
recent_memes = deque(maxlen=RECENT_MEMES_MAX)
like_tracker: Dict[str, Dict[int, str]] = defaultdict(dict)
likes_file = MEME_DIR / "likes.json"
group_id_file = Path("group_id.txt")
group_chat_id: Optional[int] = None
post_interval = 10
job = None


# Utility functions

def load_memes():
    global memes
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    memes = sorted([
        f for f in MEME_DIR.iterdir() if f.suffix.lower() in ALLOWED_EXT and f.is_file()
    ])
    logger.info(f"Loaded {len(memes)} memes.")


def load_likes():
    global like_tracker
    if likes_file.exists():
        try:
            with open(likes_file) as f:
                data = json.load(f)
                like_tracker = defaultdict(
                    dict,
                    {
                        k: {int(u): v for u, v in val.items()}
                        for k, val in data.items()
                    },
                )
            logger.info("Loaded likes data.")
        except Exception as e:
            logger.warning(f"Failed loading likes data: {e}")
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
        logger.error(f"Failed saving likes data: {e}")


def save_group_id(chat_id: int):
    try:
        with open(group_id_file, "w") as f:
            f.write(str(chat_id))
        logger.info(f"Saved group id {chat_id}.")
    except Exception as e:
        logger.error(f"Failed saving group id: {e}")


def load_group_id() -> Optional[int]:
    if group_id_file.exists():
        try:
            cid = int(group_id_file.read_text().strip())
            logger.info(f"Loaded group id {cid}.")
            return cid
        except Exception:
            logger.warning("Failed loading group id.")
    return None


def next_meme(randomize=False) -> Optional[Path]:
    global current_index
    if not memes:
        return None
    if randomize:
        candidates = [m for m in memes if m not in recent_memes]
        if not candidates:
            recent_memes.clear()
            candidates = list(memes)
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
        InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}")
    ]
    return InlineKeyboardMarkup([buttons])


# Handlers

async def send_meme(chat_id: int, context: Optional[ContextTypes.DEFAULT]):
    meme = next_meme()
    if not meme:
        if context:
            await context.bot.send_message(chat_id, "No memes available.")
        logger.warning("No memes available for posting.")
        return

    filename = meme.name
    like_text = format_likes(like_tracker.get(filename, {}))
    caption = f"👍 {like_text}" if like_text else None

    try:
        with meme.open('rb') as f:
            await context.bot.send_photo(chat_id, photo=InputFile(f, filename=filename),
                                         caption=caption, reply_markup=build_keyboard(filename))
    except Exception:
        try:
            with meme.open('rb') as f:
                await context.bot.send_document(chat_id, document=InputFile(f, filename=filename),
                                                caption=caption, reply_markup=build_keyboard(filename))
        except Exception as e:
            logger.error(f"Failed sending meme '{filename}': {e}")


async def start_handler(update: Update, context: ContextTypes.DEFAULT):
    await context.bot.send_message(update.effective_chat.id, "Bot is online and ready.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = data.split("|")
    except ValueError:
        return

    user_id = query.from_user.id
    if user_id in like_tracker[filename]:
        # User already liked
        return

    like_tracker[filename][user_id] = emoji
    save_likes()

    like_text = format_likes(like_tracker[filename])
    caption_base = query.message.caption.split("\n")[0] if query.message.caption else ""
    new_caption = f"{caption_base}\n\n👍 {like_text}" if like_text else caption_base

    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=build_keyboard(filename))
    except Exception as e:
        logger.warning(f"Failed to edit message caption: {e}")


async def scheduled_post(context: ContextTypes.DEFAULT):
    if group_chat_id is None:
        logger.warning("Group ID not set; skipping scheduled posting.")
        return
    await send_meme(group_chat_id, context)


async def set_interval_handler(update: Update, context: ContextTypes.DEFAULT):
    global post_interval, job

    chat_id = update.effective_chat.id
    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)

    if member.status not in ("administrator", "creator"):
        await context.bot.send_message(chat_id, "You must be an admin to set interval.")
        return

    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return

    minutes = int(context.args[0])
    if not (1 <= minutes <= 60):
        await context.bot.send_message(chat_id, "Interval must be between 1 and 60 minutes.")
        return

    post_interval = minutes

    if job:
        job.schedule_removal()

    job = context.job_queue.run_repeating(
        scheduled_post,
        interval=post_interval * 60,
        first=5
    )

    await context.bot.send_message(chat_id, f"Posting interval set to every {post_interval} minutes.")


async def add_meme_handler(update: Update, context: ContextTypes.DEFAULT):
    chat_id = update.effective_chat.id
    message = update.message

    if not message or not message.reply_to_message:
        await context.bot.send_message(chat_id, "Reply to a photo or document with /add to add meme.")
        return

    reply = message.reply_to_message
    file = None
    filename = None

    if reply.photo:
        file = await reply.photo[-1].get_file()
        filename = f"{file.file_id}.jpg"
    elif reply.document and reply.document.file_name and Path(reply.document.file_name).suffix.lower() in ALLOWED_EXT:
        file = await reply.document.get_file()
        filename = reply.document.file_name
    else:
        await context.bot.send_message(chat_id, "Unsupported file type.")
        return

    save_path = MEME_DIR / filename
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id, "Meme added successfully.")


async def detect_and_save_id(update: Update, context: ContextTypes.DEFAULT):
    global group_chat_id, job
    chat = update.effective_chat
    if group_chat_id is None and chat.type in ("group", "supergroup"):
        group_chat_id = chat.id
        save_group_id(group_chat_id)
        logger.info(f"Saved group chat ID: {group_chat_id}")

        if job is None:
            job = context.job_queue.run_repeating(
                scheduled_post,
                interval=post_interval * 60,
                first=10,
            )
            logger.info("Started scheduled post job due to new group ID.")


async def get_group_id_handler(update: Update, context: ContextTypes.DEFAULT):
    chat = update.effective_chat
    await context.bot.send_message(chat.id, f"Group ID: {chat.id}")

    global group_chat_id
    if group_chat_id is None:
        group_chat_id = chat.id
        save_group_id(group_chat_id)


async def init_group_handler(update: Update, context: ContextTypes.DEFAULT):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await context.bot.send_message(chat.id, "This command can only be used in groups.")
        return

    global group_chat_id
    group_chat_id = chat.id
    save_group_id(group_chat_id)
    await context.bot.send_message(chat.id, f"Group initialized with ID: {group_chat_id}")


async def webhook_handler(request: web.Request):
    app = request.app["bot_app"]
    try:
        json_data = await request.json()
        update = Update.de_json(json_data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Exception in webhook_handler: {e}")
    return web.Response(text="OK")


async def health_check(request: web.Request):
    return web.Response(text="OK")


async def send_meme_http(request: web.Request):
    app = request.app["bot_app"]
    if group_chat_id is None:
        return web.Response(status=400, text="Group ID is not set")
    await send_meme(group_chat_id, None)
    return web.Response(text="Sent meme")


async def main():
    global group_chat_id, job

    load_memes()
    load_likes()
    group_chat_id = load_group_id()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_and_save_id))
    app.add_handler(CommandHandler("setinterval", set_interval_handler))
    app.add_handler(CommandHandler("add", add_meme_handler))
    app.add_handler(CommandHandler("getgroupid", get_group_id_handler))
    app.add_handler(CommandHandler("initgroup", init_group_handler))

    # Schedule posting if group is known
    if group_chat_id:
        job = app.job_queue.run_repeating(scheduled_post, interval=post_interval * 60)
        logger.info("Started scheduled posting job")

    # Setup webserver and routes
    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_check),
        web.post(WEBHOOK_PATH, webhook_handler),
        web.get("/send_meme", send_meme_http)
    ])

    web_app["bot_app"] = app

    runner = web.AppRunner(web_app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webserver running on port {port}")

    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        logger.error("WEBHOOK_URL not set. Please set WEBHOOK_BASE env variable correctly.")

    await app.initialize()
    await app.start()
    logger.info("Bot started.")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Bot shutting down...")

    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
