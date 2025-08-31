import os
import json
import logging
import asyncio
import random
from pathlib import Path
from typing import Dict, Optional, List
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

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration and environment variables
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
    logger.error("❌ Please set a valid BOT_TOKEN in TELEGRAM_TOKEN environment variable")
    exit(1)

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip('/')  # e.g. https://yourapp.com (without trailing slash)
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else None
PORT = int(os.getenv("PORT", "10000"))

MEME_DIR = Path(os.getenv("MEME_DIR", "memes"))
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

RECENT_MEMES_MAX = 5
post_interval = 10  # minutes

# Globals
memes: List[Path] = []
current_index = 0
recent_memes = deque(maxlen=RECENT_MEMES_MAX)
like_tracker: Dict[str, Dict[int, str]] = defaultdict(dict)
likes_file = MEME_DIR / "likes.json"
group_file = Path("group_id.txt")
group_chat_id: Optional[int] = None
job = None


def load_memes():
    global memes
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    memes = sorted([f for f in MEME_DIR.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXT])
    logger.info(f"Loaded {len(memes)} memes.")


def load_likes():
    global like_tracker
    if likes_file.exists():
        try:
            with open(likes_file, "r") as f:
                data = json.load(f)
                # Convert user ids to int keys
                like_tracker = defaultdict(dict, {
                    k: {int(uid): v for uid, v in val.items()} for k, val in data.items()
                })
            logger.info("Loaded likes data.")
        except Exception as e:
            logger.warning(f"Failed to load likes data: {e}. Starting fresh.")
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


def save_group_id(chat_id):
    try:
        with open(group_file, "w") as f:
            f.write(str(chat_id))
        logger.info(f"Saved group chat ID: {chat_id}")
    except Exception as e:
        logger.error(f"Failed to save group chat ID: {e}")


def load_group_id() -> Optional[int]:
    if group_file.exists():
        try:
            cid = int(group_file.read_text().strip())
            logger.info(f"Loaded group chat ID: {cid}")
            return cid
        except Exception as e:
            logger.warning(f"Failed to load group chat ID: {e}")
    return None


def next_meme(randomize=False):
    global current_index
    if not memes:
        logger.warning("No memes found.")
        return None
    if randomize:
        candidates = [m for m in memes if m not in recent_memes]
        if not candidates:
            recent_memes.clear()
            candidates = memes[:]
        choice = random.choice(candidates)
    else:
        choice = memes[current_index % len(memes)]
        current_index += 1
    recent_memes.append(choice)
    return choice


def format_likes(likes):
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
    if parts:
        total = sum(counts.values())
        return f"{' | '.join(parts)} (Total: {total})"
    else:
        return "No likes yet. Be the first to like!"


def build_keyboard(filename):
    buttons = [
        InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{filename}"),
        InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{filename}"),
        InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}"),
    ]
    return InlineKeyboardMarkup([buttons])


async def send_meme(chat_id, context, randomize=False):
    meme = next_meme(randomize)
    if not meme:
        if context:
            await context.bot.send_message(chat_id, "No memes available.")
        logger.warning("No memes available to send.")
        return
    filename = meme.name
    like_text = format_likes(like_tracker.get(filename, {}))
    caption = f"👍 Likes: {like_text}"
    try:
        with meme.open('rb') as f:
            await context.bot.send_photo(chat_id, photo=InputFile(f, filename=filename), caption=caption, reply_markup=build_keyboard(filename))
    except Exception:
        try:
            with meme.open('rb') as f:
                await context.bot.send_document(chat_id, document=InputFile(f, filename=filename), caption=caption, reply_markup=build_keyboard(filename))
        except Exception as e:
            logger.error(f"Failed to send meme {filename}: {e}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = query.data.split("|")
    except ValueError:
        return
    user_id = query.from_user.id
    if user_id in like_tracker.get(filename, {}):
        return  # User already liked
    like_tracker.setdefault(filename, {})[user_id] = emoji
    save_likes()
    like_text = format_likes(like_tracker[filename])
    base_caption = query.message.caption.split("\n")[0] if query.message.caption else ''
    new_caption = f"{base_caption}\n\n👍 Likes: {like_text}"
    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=build_keyboard(filename))
    except Exception as e:
        logger.warning(f"Failed to update likes: {e}")


async def scheduled_post(context):
    chat_id = context.job.data if context.job and context.job.data else group_chat_id
    if not chat_id:
        logger.warning("Group chat ID not set; skipping scheduled post")
        return
    await send_meme(chat_id, context)


async def set_interval(update, context):
    global post_interval, job
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ('administrator', 'creator'):
        await context.bot.send_message(chat_id, "Only admins can set interval.")
        return
    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return
    minutes = int(context.args[0])
    if not (1 <= minutes <= 60):
        await context.bot.send_message(chat_id, "Please choose a value between 1 and 60.")
        return
    post_interval = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
    await context.bot.send_message(chat_id, f"✅ Posting interval set to {post_interval} minutes.")


async def add_meme(update, context):
    chat_id = update.effective_chat.id
    if not update.message or not update.message.reply_to_message:
        await context.bot.send_message(chat_id, "Reply to a photo or document message with /add")
        return
    replied = update.message.reply_to_message
    if replied.photo:
        file = await replied.photo[-1].get_file()
        filename = f"{file.file_id}.jpg"
    elif replied.document and replied.document.file_name and Path(replied.document.file_name).suffix.lower() in ALLOWED_EXT:
        file = await replied.document.get_file()
        filename = replied.document.file_name
    else:
        await context.bot.send_message(chat_id, "Unsupported file type.")
        return
    save_path = MEME_DIR / filename
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id, "✅ Meme added successfully.")


async def detect_and_save_id(update, context):
    global group_chat_id, job
    chat = update.effective_chat
    if group_chat_id is None and chat.type in ('group', 'supergroup'):
        group_chat_id = chat.id
        save_group_id(group_chat_id)
        if not job:
            job = context.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
            logger.info("Started scheduled post job after detecting group chat")


async def get_group_id(update, context):
    chat = update.effective_chat
    await context.bot.send_message(chat.id, f"Group chat ID: {chat.id}")
    global group_chat_id
    if group_chat_id is None and chat.type in ('group', 'supergroup'):
        group_chat_id = chat.id
        save_group_id(group_chat_id)


async def init_group(update, context):
    chat = update.effective_chat
    if chat.type not in ('group', 'supergroup'):
        await context.bot.send_message(chat.id, "This command can only be used in groups")
        return
    global group_chat_id
    group_chat_id = chat.id
    save_group_id(group_chat_id)
    await context.bot.send_message(chat.id, f"Group initialized with id {group_chat_id}")


async def webhook_handler(request):
    app = request.app['bot_app']
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Failed processing update: {e}")
    return web.Response(text="OK")


async def health_check(request):
    return web.Response(text="OK")


async def send_meme_endpoint(request):
    app = request.app['bot_app']
    if not group_chat_id:
        return web.Response(status=400, text="Group chat ID is not set")
    await send_meme(group_chat_id, None)
    return web.Response(text="Sent")


async def main():
    global group_chat_id, job

    load_memes()
    load_likes()
    group_chat_id = load_group_id()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", lambda u, c: c.bot.send_message(u.effective_chat.id, "Bot is online!")))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_and_save_id))
    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("add", add_meme))
    app.add_handler(CommandHandler("getgroupid", get_group_id))
    app.add_handler(CommandHandler("init_group", init_group))

    if group_chat_id:
        job = app.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
        logger.info("Started scheduled post job")

    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_check),
        web.post(WEBHOOK_PATH, webhook_handler),
        web.get("/send_meme", send_meme_endpoint),
    ])

    web_app['bot_app'] = app

    runner = web.AppRunner(web_app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Server listening on port {port}")

    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set: {WEBHOOK_URL}")
    else:
        logger.error("WEBHOOK_URL not set! Check your environment variable.")

    await app.initialize()
    await app.start()
    logger.info("Bot started")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Shutdown signal received.")

    await app.stop()
    await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
