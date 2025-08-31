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
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from aiohttp import web

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables and settings
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE"):
    logger.error("❌ Please set a valid BOT_TOKEN in TELEGRAM_TOKEN env var")
    exit(1)

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE}{WEBHOOK_PATH}" if WEBHOOK_BASE else None

PORT = int(os.getenv("PORT", "10000"))
MEME_DIR = Path(os.getenv("MEME_DIR", "memes"))
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MEMS_MAX = 5
post_interval = 10

# Globals
memes: List[Path] = []
current_index = 0
recent_memes = deque(maxlen=RECENT_MEMS_MAX)
like_tracker: Dict[str, List[str]] = defaultdict(list)  # List of emojis per meme, multiple per user allowed
likes_file = MEME_DIR / "likes.json"
group_file = Path("group_id.txt")
group_chat_id: Optional[int] = None
job = None

# Load memes
def load_memes():
    global memes
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    memes = sorted([p for p in MEME_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT])
    logger.info(f"Loaded {len(memes)} memes.")

# Load likes
def load_likes():
    global like_tracker
    if likes_file.exists():
        try:
            with open(likes_file) as f:
                data = json.load(f)
                like_tracker = defaultdict(list, data)
            logger.info("Loaded likes data.")
        except Exception as e:
            logger.warning("Failed loading likes data, starting fresh: %s", e)
            like_tracker = defaultdict(list)
    else:
        like_tracker = defaultdict(list)

# Save likes
def save_likes():
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        with open(likes_file, 'w') as f:
            json.dump(like_tracker, f)
        logger.info("Likes saved.")
    except Exception as e:
        logger.error("Failed to save likes data: %s", e)

# Save group ID
def save_group_id(chat_id):
    try:
        with open(group_file, 'w') as f:
            f.write(str(chat_id))
        logger.info("Saved group chat ID: %d", chat_id)
    except Exception as e:
        logger.error("Failed to save group chat ID: %s", e)

# Load group ID
def load_group_id():
    if group_file.exists():
        try:
            cid = int(group_file.read_text().strip())
            logger.info("Loaded group chat ID: %d", cid)
            return cid
        except Exception as e:
            logger.warning("Failed to load group chat ID: %s", e)
    return None

# Select next meme
def next_meme(randomize=False):
    global current_index
    if not memes:
        logger.warning("No memes available")
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

# Format likes to string
def format_likes(likes: List[str]):
    counts = {"heart": 0, "love": 0, "haha": 0}
    for emoji in likes:
        if emoji in counts:
            counts[emoji] += 1
    parts = []
    for key, emoji_char in [("heart", "❤️"), ("love", "🔥"), ("haha", "😂")]:
        if counts[key]:
            parts.append(f"{emoji_char} {counts[key]}")
    if parts:
        total = sum(counts.values())
        return f"{' | '.join(parts)} (Total: {total})"
    else:
        return "No likes yet. Be the first!"

# Create inline keyboard
def build_keyboard(filename):
    buttons = [
        InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{filename}"),
        InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{filename}"),
        InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}"),
    ]
    return InlineKeyboardMarkup([buttons])

# Send meme
async def send_meme(chat_id, context, randomize=False):
    meme = next_meme(randomize)
    if not meme:
        if context:
            await context.bot.send_message(chat_id, "No memes available.")
        logger.warning("No memes available to send.")
        return
    filename = meme.name
    likes = like_tracker.get(filename, [])
    likes_text = format_likes(likes)
    caption = f"👍 Likes: {likes_text}"
    try:
        with meme.open('rb') as f:
            await context.bot.send_photo(chat_id, photo=InputFile(f, filename=filename), caption=caption, reply_markup=build_keyboard(filename))
    except Exception:
        try:
            with meme.open('rb') as f:
                await context.bot.send_document(chat_id, document=InputFile(f, filename=filename), caption=caption, reply_markup=build_keyboard(filename))
        except Exception as e:
            logger.error("Failed to send meme %s: %s", filename, e)

# Handle likes
async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = query.data.split("|")
    except Exception:
        return

    # Allow multiple likes per user -- just append emoji
    like_tracker.setdefault(filename, []).append(emoji)
    save_likes()

    likes = like_tracker[filename]
    likes_text = format_likes(likes)

    base_caption = query.message.caption.split("\n")[0] if query.message.caption else ''
    new_caption = f"{base_caption}\n\n👍 Likes: {likes_text}"

    try:
        await query.edit_message_caption(new_caption, reply_markup=build_keyboard(filename))
    except Exception as e:
        logger.warning("Failed to update likes caption: %s", e)

# Scheduled post
async def scheduled_post(context):
    chat_id = context.job.data if context.job else None
    if not chat_id:
        logger.warning("No group chat ID set, skipping scheduled post")
        return
    await send_meme(chat_id, context)

# Set interval command
async def set_interval(update, context):
    global post_interval, job
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ["administrator", "creator"]:
        await context.bot.send_message(chat_id, "Only admins can set the posting interval.")
        return
    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return
    minutes = int(context.args[0])
    if not (1 <= minutes <= 60):
        await context.bot.send_message(chat_id, "Please select a value between 1 and 60.")
        return
    post_interval = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_post, interval=minutes * 60, data=group_chat_id)
    await context.bot.send_message(chat_id, f"✅ Post interval set to every {minutes} minute(s).")

# Add meme command
async def add_meme(update, context):
    chat_id = update.effective_chat.id
    message = update.message
    if not message or not message.reply_to_message:
        await context.bot.send_message(chat_id, "Please reply to a photo or document with /add.")
        return
    replied = message.reply_to_message
    file = None
    filename = None
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

# Detect group chat ID on messages
async def detect_group_id(update, context):
    global group_chat_id, job
    chat = update.effective_chat
    if group_chat_id is None and chat.type in ["group", "supergroup"]:
        group_chat_id = chat.id
        save_group_id(group_chat_id)
        if job is None and context.job_queue:
            job = context.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
            logger.info("Started scheduled posts; group chat ID detected.")

# Get group chat ID
async def get_group_id(update, context):
    chat = update.effective_chat
    await context.bot.send_message(chat.id, f"Group chat ID is: {chat.id}")
    global group_chat_id
    if group_chat_id is None and chat.type in ["group", "supergroup"]:
        group_chat_id = chat.id
        save_group_id(group_chat_id)

# Manually initialize group
async def init_group(update, context):
    chat = update.effective_chat
    if chat.type not in ["group", "supergroup"]:
        await context.bot.send_message(chat.id, "This command can only be used in groups.")
        return
    global group_chat_id
    group_chat_id = chat.id
    save_group_id(group_chat_id)
    await context.bot.send_message(chat.id, f"✅ Group initialized with ID {group_chat_id}")

# Webhook handler
async def webhook_handler(request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Failed handling webhook: {e}")
    return web.Response(text="OK")

# Health check endpoint
async def health_check(request):
    return web.Response(text="OK")

# Endpoint to send meme manually
async def send_meme_endpoint(request):
    app = request.app["bot_app"]
    if not group_chat_id:
        return web.Response(status=400, text="Group chat ID not set")
    await send_meme(group_chat_id, None)
    return web.Response(text="Sent")

# Main async function
async def main():
    global group_chat_id, job

    load_memes()
    load_likes()

    group_chat_id = load_group_id()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", lambda u,c: c.bot.send_message(u.effective_chat.id, "Bot is online!")))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_group_id))
    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("add", add_meme))
    app.add_handler(CommandHandler("getgroupid", get_group_id))
    app.add_handler(CommandHandler("init_group", init_group))

    if group_chat_id:
        job = app.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
        logger.info("Started scheduled posting task")

    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_check),
        web.post(webhook_path, webhook_handler),
        web.get("/send_meme", send_meme_endpoint),
    ])

    web_app["bot_app"] = app

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    logger.info(f"Server started on port {os.getenv('PORT', '10000')}")

    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set: {WEBHOOK_URL}")
    else:
        logger.error("Webhook URL not set! Please set WEBHOOK_BASE env")

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
