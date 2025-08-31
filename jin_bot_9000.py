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

# Configure logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_") or BOT_TOKEN == "":
    logger.error("❌ Please set your BOT_TOKEN in the TELEGRAM_TOKEN environment variable!")
    exit(1)

MEME_DIR: Path = Path(os.getenv("MEME_DIR", "memes"))
MEME_FILES: List[Path] = []
CURRENT_INDEX: int = 0
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MEMES_MAX = 5
recent_memes: deque = deque(maxlen=RECENT_MEMES_MAX)
post_interval_minutes = 10
job = None

LIKE_TRACKER: Dict[str, Dict[int, str]] = defaultdict(dict)
LIKES_FILE = MEME_DIR / "likes.json"

GROUP_ID_FILE = Path("group_id.txt")
GROUP_CHAT_ID: Optional[int] = None

GROUP_CHAT_ID_ENV = os.getenv("GROUP_CHAT_ID")
if GROUP_CHAT_ID_ENV:
    try:
        GROUP_CHAT_ID = int(GROUP_CHAT_ID_ENV)
        logger.info(f"Loaded GROUP_CHAT_ID from environment: {GROUP_CHAT_ID}")
    except Exception:
        logger.warning(f"Invalid GROUP_CHAT_ID env variable: {GROUP_CHAT_ID_ENV}. Ignoring.")


def load_memes() -> None:
    global MEME_FILES
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    MEME_FILES = sorted([p for p in MEME_DIR.glob("*") if p.suffix.lower() in ALLOWED_EXT])


def save_likes() -> None:
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIKES_FILE, "w") as f:
            json.dump({meme: likes for meme, likes in LIKE_TRACKER.items()}, f)
    except Exception as e:
        logger.error(f"❌ Error saving likes: {e}")


def load_likes() -> None:
    global LIKE_TRACKER
    if LIKES_FILE.exists():
        try:
            with open(LIKES_FILE, "r") as f:
                data = json.load(f)
                LIKE_TRACKER = defaultdict(dict, {str(k): v for k, v in data.items()})
        except Exception as e:
            logger.warning(f"⚠️ Error loading likes data, starting fresh: {e}")
            LIKE_TRACKER = defaultdict(dict)
    else:
        LIKE_TRACKER = defaultdict(dict)


def save_group_id(group_id: int) -> None:
    try:
        with open(GROUP_ID_FILE, "w") as f:
            f.write(str(group_id))
            logger.info(f"Saved group chat ID {group_id} to file.")
    except Exception as e:
        logger.error(f"❌ Error saving group chat ID: {e}")


def load_group_id() -> Optional[int]:
    if GROUP_ID_FILE.exists():
        try:
            return int(GROUP_ID_FILE.read_text().strip())
        except Exception as e:
            logger.warning(f"⚠️ Error loading group chat ID: {e}")
            return None
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
    available_memes = [m for m in MEME_FILES if m not in recent_memes]
    if not available_memes:
        recent_memes.clear()
        available_memes = MEME_FILES[:]
    meme = random.choice(available_memes)
    recent_memes.append(meme)
    return meme


def create_keyboard(meme_filename: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("❤️", callback_data=f"LIKE_heart|{meme_filename}"),
            InlineKeyboardButton("🔥", callback_data=f"LIKE_love|{meme_filename}"),
            InlineKeyboardButton("😂", callback_data=f"LIKE_haha|{meme_filename}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def format_likes(like_data: Dict[int, str]) -> str:
    counts = {"heart": 0, "love": 0, "haha": 0}
    for like_type in like_data.values():
        if like_type in counts:
            counts[like_type] += 1
    parts = []
    if counts["heart"]:
        parts.append(f"❤️ {counts['heart']}")
    if counts["love"]:
        parts.append(f"🔥 {counts['love']}")
    if counts["haha"]:
        parts.append(f"😂 {counts['haha']}")
    return " | ".join(parts) if parts else ""


async def send_meme(
    chat_id: int, context: ContextTypes.DEFAULT_TYPE, randomize: bool = False
) -> None:
    meme_path = next_meme(randomize=randomize)
    if not meme_path:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ No memes found! Please add some images to the 'memes' folder.",
        )
        return
    meme_filename = meme_path.name
    likes_summary = format_likes(LIKE_TRACKER[meme_filename])
    caption = f"👍 Likes: {likes_summary}" if likes_summary else ""
    try:
        with open(meme_path, "rb") as file:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(file, filename=meme_filename),
                caption=caption,
                reply_markup=create_keyboard(meme_filename),
            )
    except Exception:
        try:
            with open(meme_path, "rb") as file:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(file, filename=meme_filename),
                    caption=caption,
                    reply_markup=create_keyboard(meme_filename),
                )
        except Exception:
            pass


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if not data.startswith("LIKE_"):
        return
    try:
        _, reaction, meme_filename = data.split("|")
    except ValueError:
        return
    user_likes = LIKE_TRACKER[meme_filename]
    if user_id in user_likes:
        return
    user_likes[user_id] = reaction
    save_likes()
    likes_summary = format_likes(user_likes)
    original_caption = query.message.caption.split("\n")[0] if query.message.caption else ""
    new_caption = f"{original_caption}\n\n👍 Likes: {likes_summary}" if likes_summary else original_caption
    try:
        await query.edit_message_caption(caption=new_caption, reply_markup=create_keyboard(meme_filename))
    except Exception:
        pass


async def scheduled_meme_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None:
        logger.warning("Group chat ID not set; skipping scheduled post.")
        return
    await send_meme(GROUP_CHAT_ID, context)


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global post_interval_minutes, job, GROUP_CHAT_ID
    chat_id = update.effective_chat.id
    if GROUP_CHAT_ID is None:
        GROUP_CHAT_ID = chat_id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Group chat ID set to {GROUP_CHAT_ID} by /setinterval command.")
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text="Failed to check admin status.")
        return
    if member.status not in ('administrator', 'creator'):
        await context.bot.send_message(chat_id=chat_id, text="❌ Only group admins can set the posting interval.")
        return
    if not context.args or not context.args[0].isdigit():
        await context.bot.send_message(chat_id=chat_id, text="Usage: /setinterval <minutes>\nExample: /setinterval 15")
        return
    minutes = int(context.args[0])
    if minutes < 1 or minutes > 60:
        await context.bot.send_message(chat_id=chat_id, text="Please choose an interval between 1 and 60 minutes.")
        return
    post_interval_minutes = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_meme_post, interval=post_interval_minutes * 60, first=5)
    await context.bot.send_message(chat_id=chat_id, text=f"✅ Posting interval updated to every {post_interval_minutes} minutes.")


async def add_meme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID
    chat = update.effective_chat
    message = update.effective_message
    if GROUP_CHAT_ID is None:
        GROUP_CHAT_ID = chat.id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Group chat ID set to {GROUP_CHAT_ID} by /add command.")
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        await context.bot.send_message(chat_id=chat.id, text="Failed to verify admin status.")
        return
    if member.status not in ('administrator', 'creator'):
        await context.bot.send_message(chat_id=chat.id, text="❌ Only group admins can add memes.")
        return
    if not message.reply_to_message:
        await context.bot.send_message(chat_id=chat.id, text="Please reply to a meme photo or document message with /add to add it.")
        return
    replied = message.reply_to_message
    file = None
    file_name = None
    if replied.photo:
        file = await replied.photo[-1].get_file()
        file_name = f"{file.file_id}.jpg"
    elif replied.document and replied.document.file_name:
        ext = Path(replied.document.file_name).suffix.lower()
        if ext in ALLOWED_EXT:
            file = await replied.document.get_file()
            file_name = replied.document.file_name
        else:
            await context.bot.send_message(chat_id=chat.id, text="Unsupported file type. Please add an image file.")
            return
    else:
        await context.bot.send_message(chat_id=chat.id, text="Reply to a photo or supported document to add as a meme.")
        return
    save_path = MEME_DIR / file_name
    await file.download_to_drive(str(save_path))
    load_memes()
    await context.bot.send_message(chat_id=chat.id, text="✅ Added successfully!")


async def detect_and_save_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID, job
    chat = update.effective_chat
    if GROUP_CHAT_ID is None and chat and chat.type in ('group', 'supergroup'):
        GROUP_CHAT_ID = chat.id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Detected and saved group chat ID: {GROUP_CHAT_ID}")

        # Start scheduled meme post job if it is not already running
        if job is None:
            job = context.job_queue.run_repeating(
                scheduled_meme_post,
                interval=post_interval_minutes * 60,
                first=5
            )
            logger.info("Scheduled meme post job started after group ID detection.")


async def get_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await context.bot.send_message(chat_id=chat.id, text=f"This chat's ID is: {chat.id}")
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None and chat.type in ('group', 'supergroup'):
        GROUP_CHAT_ID = chat.id
        save_group_id(GROUP_CHAT_ID)
        logger.info(f"Group chat ID set to {GROUP_CHAT_ID} by /getgroupid command.")


async def init_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GROUP_CHAT_ID
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await context.bot.send_message(chat_id=chat.id, text="This command can only be used in a group.")
        return
    GROUP_CHAT_ID = chat.id
    save_group_id(GROUP_CHAT_ID)
    await context.bot.send_message(chat_id=chat.id, text=f"✅ Group chat ID initialized and saved: {GROUP_CHAT_ID}")
    logger.info(f"Group chat ID manually initialized to {GROUP_CHAT_ID} via /initgroup.")


async def send_startup_message(app):
    global GROUP_CHAT_ID
    if GROUP_CHAT_ID is None:
        logger.warning("GROUP_CHAT_ID not set, skipping startup live message.")
        return
    try:
        await app.bot.send_message(GROUP_CHAT_ID, "🤖 Jin_Bot_9000 is online and running!")
        logger.info(f"Sent startup live message to {GROUP_CHAT_ID}")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")


async def handle_http_request(request: web.Request) -> web.Response:
    return web.Response(text="Jin_Bot_9000 is running")


async def handle_send_meme_request(request: web.Request) -> web.Response:
    global GROUP_CHAT_ID
    app = request.app["telegram_app"]
    if GROUP_CHAT_ID is None:
        logger.warning("Group chat ID not set; cannot send meme via HTTP request.")
        return web.Response(status=400, text="Group chat ID not set.")
    await send_meme(GROUP_CHAT_ID, app)
    return web.Response(text="Meme sent to group.")


async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Update received: {update}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /start in chat {update.effective_chat.id} ({update.effective_chat.type})")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🤖 Jin_Bot_9000 is alive and listening!"
    )


async def main_async():
    global job, GROUP_CHAT_ID
    logger.info("🤖 Starting Jin_Bot_9000...")
    load_memes()
    load_likes()
    if not GROUP_CHAT_ID:
        GROUP_CHAT_ID = load_group_id()
    if GROUP_CHAT_ID:
        logger.info(f"Loaded saved group chat ID: {GROUP_CHAT_ID}")
    else:
        logger.info("No saved group chat ID found.")
    if not MEME_FILES:
        logger.warning("⚠️ No meme files found! Add some images to the 'memes' folder.")

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

    app_bot.add_handler(MessageHandler(filters.ALL, log_all_updates), group=-1)

    app_bot.add_handler(CommandHandler("start", start))

    app_bot.add_handler(MessageHandler(filters.ChatType.GROUP | filters.ChatType.SUPERGROUP, detect_and_save_group_id), group=0)
    app_bot.add_handler(CommandHandler("setinterval", set_interval))
    app_bot.add_handler(CommandHandler("add", add_meme))
    app_bot.add_handler(CommandHandler("getgroupid", get_group_id))
    app_bot.add_handler(CommandHandler("initgroup", init_group))
    app_bot.add_handler(CallbackQueryHandler(handle_button_press))

    # Schedule posting job only if group ID already loaded
    global job
    if GROUP_CHAT_ID is not None:
        job = app_bot.job_queue.run_repeating(
            scheduled_meme_post,
            interval=post_interval_minutes * 60,
            first=10
        )
        logger.info("Scheduled meme post job started on startup.")

    app_web = web.Application()
    app_web.add_routes([
        web.get('/', handle_http_request),
        web.get('/send_meme', handle_send_meme_request),
    ])

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"✅ HTTP server started on port {port}")

    app_web["telegram_app"] = app_bot

    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling()

    logger.info("✅ Telegram bot started")

    await send_startup_message(app_bot)

    try:
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutting down...")
    finally:
        await app_bot.updater.stop_polling()
        await app_bot.stop()
        await runner.cleanup()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
