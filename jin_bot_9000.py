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

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config & env vars
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
    logger.error("❌ Please set a valid BOT_TOKEN in TELEGRAM_TOKEN env variable")
    exit(1)

WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
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
like_tracker: Dict[str, List[str]] = defaultdict(list)  # Store list of emojis per meme
likes_file = MEME_DIR / "likes.json"
group_file = Path("group_id.txt")
group_chat_id: Optional[int] = None
job = None

# Load memes function
def load_memes():
    global memes
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    memes = [p for p in MEME_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT]
    memes.sort()
    logger.info(f"Loaded {len(memes)} memes.")

# Load likes from JSON file
def load_likes():
    global like_tracker
    if likes_file.exists():
        try:
            with open(likes_file, "r") as f:
                data = json.load(f)
                like_tracker = defaultdict(list, data)
            logger.info("Loaded likes data.")
        except Exception as e:
            logger.warning(f"Failed to load likes data, starting fresh: {e}")
            like_tracker = defaultdict(list)
    else:
        like_tracker = defaultdict(list)

# Save likes to JSON file
def save_likes():
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        with open(likes_file, "w") as f:
            json.dump(like_tracker, f)
        logger.info("Saved likes data.")
    except Exception as e:
        logger.error(f"Failed to save likes data: {e}")

# Save group chat ID
def save_group_id(chat_id: int):
    try:
        with open(group_file, "w") as f:
            f.write(str(chat_id))
        logger.info(f"Saved group chat ID: {chat_id}")
    except Exception as e:
        logger.error(f"Failed to save group chat ID: {e}")

# Load group chat ID
def load_group_id() -> Optional[int]:
    if group_file.exists():
        try:
            cid = int(group_file.read_text().strip())
            logger.info(f"Loaded group chat ID: {cid}")
            return cid
        except Exception as e:
            logger.warning(f"Failed to load group chat ID: {e}")
    return None

# Get next meme
def next_meme(randomize=False) -> Optional[Path]:
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

# Format likes for display
def format_likes(likes: List[str]) -> str:
    counts = {"heart": 0, "love": 0, "haha": 0}
    for emoji in likes:
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

# Build inline keyboard
def build_keyboard(filename: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{filename}"),
        InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{filename}"),
        InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{filename}"),
    ]
    return InlineKeyboardMarkup([buttons])

# Send meme to chat
async def send_meme(chat_id: int, context: Optional[ContextTypes.DEFAULT_TYPE], randomize=False):
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
            await context.bot.send_photo(chat_id, photo=InputFile(f, filename=filename),
                                         caption=caption, reply_markup=build_keyboard(filename))
    except Exception:
        try:
            with meme.open('rb') as f:
                await context.bot.send_document(chat_id, document=InputFile(f, filename=filename),
                                               caption=caption, reply_markup=build_keyboard(filename))
        except Exception as e:
            logger.error(f"Failed to send meme {filename}: {e}")

# Callback for like buttons
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("LIKE_"):
        return
    try:
        _, emoji, filename = query.data.split("|")
    except ValueError:
        return
    # Allow multiple likes per user: just append emoji without restrictions
    like_tracker.setdefault(filename, []).append(emoji)
    save_likes()
    likes = like_tracker[filename]
    like_text = format_likes(likes)
    caption_base = query.message.caption.split("\n")[0] if query.message.caption else ""
    new_caption = f"{caption_base}\n\n👍 Likes: {like_text}"
    try:
        await query.edit_message_caption(new_caption, reply_markup=build_keyboard(filename))
    except Exception as e:
        logger.warning(f"Failed to update likes caption: {e}")

# Scheduled posting
async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data if context.job else None
    if not chat_id:
        logger.warning("Group chat ID not set. Skipping scheduled post.")
        return
    await send_meme(chat_id, context)

# /setinterval command
async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global post_interval, job
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ("administrator", "creator"):
        await context.bot.send_message(chat_id, "Only admins can set posting intervals.")
        return
    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id, "Usage: /setinterval <minutes>")
        return
    minutes = int(context.args[0])
    if not 1 <= minutes <= 60:
        await context.bot.send_message(chat_id, "Please choose a value between 1 and 60.")
        return
    post_interval = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
    await context.bot.send_message(chat_id, f"✅ Posting interval set to every {post_interval} minutes.")

# /add command
async def add_meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.message
    if not msg or not msg.reply_to_message:
        await context.bot.send_message(chat_id, "Reply to a photo or document message with /add.")
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
        await context.bot.send_message(chat_id, "Unsupported file type.")
        return
    save_path = MEME_DIR / filename
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id, "✅ Meme added successfully.")

# Auto-detect and save group ID
async def detect_and_save_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global group_chat_id, job
    chat = update.effective_chat
    if group_chat_id is None and chat.type in ("group", "supergroup"):
        group_chat_id = chat.id
        save_group_id(group_chat_id)
        if not job:
            job = context.job_queue.run_repeating(scheduled_post, interval=post_interval * 60, data=group_chat_id)
            logger.info("Started scheduled post job after detecting group chat")

# /getgroupid command
async def get_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, f"Group ID: {chat_id}")
    global group_chat_id
    if group_chat_id is None and update.effective_chat.type in ("group", "supergroup"):
        group_chat_id = chat_id
        save_group_id(group_chat_id)

# /init command
async def init_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await context.bot.send_message(update.effective_chat.id, "This command can only be used in groups.")
        return
    global group_chat_id
    group_chat_id = update.effective_chat.id
    save_group_id(group_chat_id)
    await context.bot.send_message(group_chat_id, "✅ Group initialized for posting memes.")

# Webhook handler
async def webhook_handler(request: web.Request):
    app = request.app["bot_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}")
    return web.Response(text="OK")

# Health check
async def health_check(request: web.Request):
    return web.Response(text="OK")

# Endpoint to manually send meme
async def send_meme_endpoint(request: web.Request):
    app = request.app["bot_app"]
    if not group_chat_id:
        return web.Response(status=400, text="Group ID not set")
    await send_meme(group_chat_id, None)
    return web.Response(text="Sent")

# Main function
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
        logger.info("Started scheduled posting job")

    web_app = web.Application()
    web_app.add_routes([
        web.get("/", health_check),
        web.post(WEBHOOK_PATH, webhook_handler),
        web.get("/send_meme", send_meme_endpoint),
    ])

    web_app["bot_app"] = app

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Server started at port {PORT}")

    if WEBHOOK_URL:
        await app.bot.delete_webhook()
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info(f"Webhook set: {WEBHOOK_URL}")
    else:
        logger.error("Webhook URL not set; please set WEBHOOK_BASE env")

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
