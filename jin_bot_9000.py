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
)
import os
import asyncio
from aiohttp import web

BOT_TOKEN: str = os.getenv("TELEGRAM_TOKEN")
GROUP_CHAT_ID: int = -4897881939
MEME_DIR: Path = Path(os.getenv("MEME_DIR", "memes"))
MEME_FILES: List[Path] = []
CURRENT_INDEX: int = 0
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MEMES_MAX = 5
recent_memes: deque = deque(maxlen=RECENT_MEMES_MAX)
post_interval_minutes = 10
job = None

# Likes storage: key by meme filename to persist likes across reposts
LIKE_TRACKER: Dict[str, Dict[int, str]] = defaultdict(dict)
LIKES_FILE = MEME_DIR / "likes.json"


def load_memes() -> None:
    global MEME_FILES
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    MEME_FILES = sorted([p for p in MEME_DIR.glob("*") if p.suffix.lower() in ALLOWED_EXT])


def save_likes() -> None:
    try:
        if not MEME_DIR.exists():
            MEME_DIR.mkdir(parents=True, exist_ok=True)
        # Convert defaultdict to normal dict for JSON serialization
        with open(LIKES_FILE, "w") as f:
            json.dump({meme: likes for meme, likes in LIKE_TRACKER.items()}, f)
    except Exception as e:
        print(f"❌ Error saving likes: {e}")


def load_likes() -> None:
    global LIKE_TRACKER
    if LIKES_FILE.exists():
        try:
            with open(LIKES_FILE, "r") as f:
                data = json.load(f)
                LIKE_TRACKER = defaultdict(dict, {str(k): v for k, v in data.items()})
        except Exception as e:
            print(f"⚠️ Error loading likes data, starting fresh: {e}")
            LIKE_TRACKER = defaultdict(dict)
    else:
        LIKE_TRACKER = defaultdict(dict)


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
    # Embed the meme filename in callback_data to track likes per meme file
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
    caption = ""
    if likes_summary:
        caption = f"👍 Likes: {likes_summary}"

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
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if not data.startswith("LIKE_"):
        return

    try:
        _, reaction, meme_filename = data.split("|")
    except ValueError:
        # Data format unexpected; ignore
        return

    user_likes = LIKE_TRACKER[meme_filename]
    if user_id in user_likes:
        # User already liked, ignore duplicate
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
    await send_meme(GROUP_CHAT_ID, context)


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global post_interval_minutes, job
    chat_id = update.effective_chat.id
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
    except Exception:
        await update.effective_message.reply_text("Failed to check admin status.")
        return
    if member.status not in ('administrator', 'creator'):
        await update.effective_message.reply_text("❌ Only group admins can set the posting interval.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /setinterval <minutes>\nExample: /setinterval 15")
        return
    minutes = int(context.args[0])
    if minutes < 1 or minutes > 60:
        await update.effective_message.reply_text("Please choose an interval between 1 and 60 minutes.")
        return
    post_interval_minutes = minutes
    if job:
        job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_meme_post, interval=post_interval_minutes * 60, first=5)
    await update.effective_message.reply_text(f"✅ Posting interval updated to every {post_interval_minutes} minutes.")


async def add_meme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        await message.reply_text("Failed to verify admin status.")
        return
    if member.status not in ('administrator', 'creator'):
        await message.reply_text("❌ Only group admins can add memes.")
        return
    if not message.reply_to_message:
        await message.reply_text("Please reply to a meme photo or document message with /add to add it.")
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
            await message.reply_text("Unsupported file type. Please add an image file.")
            return
    else:
        await message.reply_text("Reply to a photo or supported document to add as a meme.")
        return
    save_path = MEME_DIR / file_name
    await file.download_to_drive(str(save_path))
    load_memes()
    await message.reply_text("✅ Added successfully!")


async def handle_http_request(request: web.Request) -> web.Response:
    return web.Response(text="Jin_Bot_9000 is running")


async def main_async():
    global job
    print("🤖 Starting Jin_Bot_9000...")
    load_memes()
    load_likes()
    if not MEME_FILES:
        print("⚠️ Warning: No meme files found! Add some images to the 'memes' folder.")
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_") or BOT_TOKEN == "":
        print("❌ Please set your BOT_TOKEN in the environment variable TELEGRAM_TOKEN! Get your token from @BotFather in Telegram.")
        exit(1)

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("setinterval", set_interval))
    app_bot.add_handler(CommandHandler("add", add_meme))
    app_bot.add_handler(CallbackQueryHandler(handle_button_press))
    job = app_bot.job_queue.run_repeating(scheduled_meme_post, interval=post_interval_minutes * 60, first=10)

    # Setup aiohttp web server for Render port binding
    app_web = web.Application()
    app_web.add_routes([web.get('/', handle_http_request)])

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ HTTP server started on port {port}")

    await app_bot.initialize()
    await app_bot.start()
    print("✅ Telegram bot started")

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("Shutting down...")
    finally:
        await app_bot.stop()
        await runner.cleanup()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
