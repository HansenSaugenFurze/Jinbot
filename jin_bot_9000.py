import random
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

BOT_TOKEN: str = "8480173527:AAEN4mrOOp8PilajpOKwCWWmP93MpyXm_h0"
GROUP_CHAT_ID: int = -4897881939
MEME_DIR: Path = Path("memes")
MEME_FILES: List[Path] = []
CURRENT_INDEX: int = 0
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
LIKE_TRACKER: Dict[int, Dict[int, str]] = defaultdict(dict)
RECENT_MEMES_MAX = 5
recent_memes: deque = deque(maxlen=RECENT_MEMES_MAX)
post_interval_minutes = 10
job = None

def load_memes() -> None:
    global MEME_FILES
    if not MEME_DIR.exists():
        MEME_DIR.mkdir(parents=True, exist_ok=True)
    MEME_FILES = sorted([p for p in MEME_DIR.glob("*") if p.suffix.lower() in ALLOWED_EXT])

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

def create_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("❤️", callback_data="LIKE_heart"),
            InlineKeyboardButton("🔥", callback_data="LIKE_love"),
            InlineKeyboardButton("😂", callback_data="LIKE_haha"),
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
    try:
        with open(meme_path, "rb") as file:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(file, filename=meme_path.name),
                caption="",
                reply_markup=create_keyboard(),
            )
    except Exception:
        try:
            with open(meme_path, "rb") as file:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(file, filename=meme_path.name),
                    caption="",
                    reply_markup=create_keyboard(),
                )
        except Exception:
            pass

async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    message_id = query.message.message_id
    data = query.data
    if data.startswith("LIKE_"):
        reaction = data.split("_")[1]
        user_likes = LIKE_TRACKER[message_id]
        if user_id in user_likes:
            return
        user_likes[user_id] = reaction
        likes_summary = format_likes(user_likes)
        original_caption = query.message.caption.split("\n")[0] if query.message.caption else ""
        new_caption = f"{original_caption}\n\n👍 Likes: {likes_summary}" if likes_summary else original_caption
        try:
            await query.edit_message_caption(caption=new_caption, reply_markup=create_keyboard())
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
    except:
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
    job.schedule_removal()
    job = context.job_queue.run_repeating(scheduled_meme_post, interval=post_interval_minutes * 60, first=5)
    await update.effective_message.reply_text(f"✅ Posting interval updated to every {post_interval_minutes} minutes.")

async def add_meme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except:
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
    await message.reply_text("✅Added successfully!")

def main() -> None:
    global job
    print("🤖 Starting Jin_Bot_9000...")
    load_memes()
    if not MEME_FILES:
        print("⚠️ Warning: No meme files found! Add some images to the 'memes' folder.")
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        print("❌ Please set your BOT_TOKEN in the code! Get your token from @BotFather in Telegram.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("add", add_meme))
    app.add_handler(CallbackQueryHandler(handle_button_press))
    job = app.job_queue.run_repeating(scheduled_meme_post, interval=post_interval_minutes * 60, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
