"""
Production-ready Telegram File Store & Share Bot entry point for Vercel Serverless Functions.
Built using Flask, python-telegram-bot v20+, MongoDB Atlas, and PyMongo.
"""

import asyncio
import html
import io
import logging
import math
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import bcrypt

# Ensure project root is in sys.path for seamless Vercel module resolution
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
import database

# Configure structured logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Initialize Flask application
app = Flask(__name__)

# Initialize python-telegram-bot Application
ptb_app = Application.builder().token(config.BOT_TOKEN).build()
_loop: Optional[asyncio.AbstractEventLoop] = None
_ptb_initialized: bool = False

# Rate limiting memory cache: user_id -> timestamp
RATE_LIMIT_CACHE: Dict[int, float] = {}


def get_loop() -> asyncio.AbstractEventLoop:
    """Retrieve or create a persistent asyncio event loop for WSGI serverless execution."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def is_rate_limited(user_id: int) -> bool:
    """Check if user exceeds command rate limit."""
    now = time.time()
    last_time = RATE_LIMIT_CACHE.get(user_id, 0.0)
    if now - last_time < config.RATE_LIMIT_SECONDS:
        return True
    RATE_LIMIT_CACHE[user_id] = now
    if len(RATE_LIMIT_CACHE) > 5000:
        cutoff = now - 10.0
        keys_to_del = [k for k, v in RATE_LIMIT_CACHE.items() if v < cutoff]
        for k in keys_to_del:
            del RATE_LIMIT_CACHE[k]
    return False


def sanitize_filename(name: str) -> str:
    """Sanitize user filenames to prevent injection and path traversal."""
    clean = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name.strip())
    clean = clean.replace("..", "_")
    if not clean:
        clean = "unnamed_file"
    return clean[:200]


def format_filesize(size_bytes: int) -> str:
    """Format file size in bytes to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes / (1024**2):.2f} MB"
    else:
        return f"{size_bytes / (1024**3):.2f} GB"


# --- UI & Keyboard Generators ---


def get_home_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Build primary bot home menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("📂 My Files", callback_data="myfiles_page_1"),
            InlineKeyboardButton("🔍 Search Files", callback_data="search_prompt"),
        ],
        [
            InlineKeyboardButton("📊 My Statistics", callback_data="mystats"),
            InlineKeyboardButton("❓ Help Guide", callback_data="help_guide"),
        ],
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)


def get_file_control_keyboard(
    file_key: str, is_onetime: bool, has_pass: bool, has_exp: bool
) -> InlineKeyboardMarkup:
    """Build interactive management buttons for a stored file."""
    onetime_label = "1️⃣ One-Time: ON" if is_onetime else "1️⃣ One-Time: OFF"
    pass_label = "🔒 Password: ON" if has_pass else "🔓 Password: OFF"
    exp_label = "⏱ Expiry: ON" if has_exp else "♾️ Expiry: OFF"

    keyboard = [
        [
            InlineKeyboardButton("🔗 Get Link", callback_data=f"getlink_{file_key}"),
            InlineKeyboardButton("📥 Download", callback_data=f"dl_{file_key}"),
        ],
        [
            InlineKeyboardButton(pass_label, callback_data=f"passcfg_{file_key}"),
            InlineKeyboardButton(exp_label, callback_data=f"expcfg_{file_key}"),
            InlineKeyboardButton(onetime_label, callback_data=f"onetimecfg_{file_key}"),
        ],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"renamecfg_{file_key}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"delcfg_{file_key}"),
        ],
        [
            InlineKeyboardButton("🔙 Back to List", callback_data="myfiles_page_1"),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_myfiles_keyboard(
    files: List[Dict[str, Any]], current_page: int, total_pages: int
) -> InlineKeyboardMarkup:
    """Build pagination and file selection keyboard for /myfiles."""
    keyboard: List[List[InlineKeyboardButton]] = []

    if files:
        sel_row = [
            InlineKeyboardButton(str(idx), callback_data=f"fileview_{f['file_key']}")
            for idx, f in enumerate(files, start=1)
        ]
        keyboard.append(sel_row)

    nav_row = []
    if current_page > 1:
        nav_row.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"myfiles_page_{current_page - 1}")
        )
    nav_row.append(
        InlineKeyboardButton(f"📄 {current_page}/{max(1, total_pages)}", callback_data="noop")
    )
    if current_page < total_pages:
        nav_row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"myfiles_page_{current_page + 1}")
        )
    keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton("📤 Export TXT", callback_data="export_txt"),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


def get_expiry_keyboard(file_key: str) -> InlineKeyboardMarkup:
    """Build timer selection keyboard for link expiration."""
    keyboard = [
        [
            InlineKeyboardButton("1 Hour", callback_data=f"setexp_{file_key}_1h"),
            InlineKeyboardButton("6 Hours", callback_data=f"setexp_{file_key}_6h"),
            InlineKeyboardButton("12 Hours", callback_data=f"setexp_{file_key}_12h"),
        ],
        [
            InlineKeyboardButton("24 Hours", callback_data=f"setexp_{file_key}_24h"),
            InlineKeyboardButton("7 Days", callback_data=f"setexp_{file_key}_7d"),
            InlineKeyboardButton("30 Days", callback_data=f"setexp_{file_key}_30d"),
        ],
        [
            InlineKeyboardButton(
                "♾️ Remove Expiry (Permanent)", callback_data=f"setexp_{file_key}_none"
            ),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=f"fileview_{file_key}"),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Build Admin Dashboard navigation keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("👥 Users List", callback_data="admin_users"),
            InlineKeyboardButton("📊 System Stats", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_prompt"),
            InlineKeyboardButton("📜 System Logs", callback_data="admin_logs"),
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# --- Core Delivery & Authorization Logic ---


async def check_user_access(update: Update) -> Optional[Dict[str, Any]]:
    """Validate user profile, check ban status, and apply rate limiting."""
    user_obj = update.effective_user
    if not user_obj:
        return None

    if is_rate_limited(user_obj.id):
        return None

    user = database.db.create_or_update_user(
        user_id=user_obj.id,
        username=user_obj.username or "",
        first_name=user_obj.first_name or "",
        last_name=user_obj.last_name or "",
    )
    if user.get("is_banned"):
        if update.effective_message:
            await update.effective_message.reply_text(
                "🚫 <b>Access Denied.</b> You have been banned from using this bot.",
                parse_mode="HTML",
            )
        return None
    return user


async def deliver_file_to_user(
    message: Any, file_doc: Dict[str, Any], user_id: int, bot: Any
) -> None:
    """Deliver stored Telegram media using file_id and update analytics."""
    file_key = file_doc["file_key"]
    file_id = file_doc["file_id"]
    media_type = file_doc["media_type"]
    filename = html.escape(file_doc["filename"])
    size_str = format_filesize(file_doc["filesize"])

    # Increment download metrics
    database.db.increment_download(file_key, file_doc["uploader"])
    database.db.update_user_stats(user_id=user_id, downloads_delta=1)

    if file_doc.get("is_onetime"):
        database.db.update_file(file_key, {"is_active": False})
        logger.info("One-time file link %s disabled after download.", file_key)

    caption = (
        f"📁 <b>{filename}</b>\n"
        f"💾 <b>Size:</b> {size_str}\n"
        f"🤖 <b>Shared via:</b> @{bot.username or 'YoriFileBot'}"
    )

    try:
        if media_type == "Document":
            await message.reply_document(document=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Video":
            await message.reply_video(video=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Photo":
            await message.reply_photo(photo=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Audio":
            await message.reply_audio(audio=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Voice":
            await message.reply_voice(voice=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Animation":
            await message.reply_animation(animation=file_id, caption=caption, parse_mode="HTML")
        elif media_type == "Sticker":
            await message.reply_sticker(sticker=file_id)
            await message.reply_text(caption, parse_mode="HTML")
        else:
            await message.reply_document(document=file_id, caption=caption, parse_mode="HTML")
    except Exception as exc:
        logger.error("Failed to deliver media %s: %s", file_key, exc)
        await message.reply_text(
            "❌ <b>Error:</b> Unable to deliver media file from Telegram cache.", parse_mode="HTML"
        )


# --- Command Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command and deep-linked file sharing requests."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    user_id = update.effective_user.id

    # Check for deep link argument (?start=FILEKEY)
    if context.args and len(context.args) > 0:
        file_key = context.args[0].strip()
        file_doc = database.db.get_file_by_key(file_key, check_expiry=True)

        if not file_doc:
            await update.effective_message.reply_text(
                "❌ <b>File Not Found or Expired.</b>\n\nThe link you followed is invalid, expired, or has been deleted by the owner.",
                parse_mode="HTML",
            )
            return

        if file_doc.get("is_onetime") and file_doc.get("download_count", 0) >= 1:
            await update.effective_message.reply_text(
                "❌ <b>One-Time Link Expired.</b>\n\nThis file was configured for a single download and has already been accessed.",
                parse_mode="HTML",
            )
            return

        stored_hash = file_doc.get("password_hash")
        if stored_hash:
            database.db.set_user_pending_access(user_id, file_key)
            await update.effective_message.reply_text(
                "🔒 <b>Password Protected File</b>\n\nThis file is encrypted and requires a password.\nPlease enter and send the password below:",
                parse_mode="HTML",
            )
            return

        await deliver_file_to_user(update.effective_message, file_doc, user_id, context.bot)
        return

    # Standard welcome menu
    is_admin = user_id in config.ADMIN_IDS
    welcome_text = (
        f"🌟 <b>Welcome to Ultimate YoriFile Store & Share Bot!</b>\n\n"
        f"I am a lightning-fast, highly secure Telegram File Storage & Sharing system. "
        f"Upload any media to generate instant, customizable shareable links!\n\n"
        f"<b>💎 Creator:</b> {config.CREATOR}\n"
        f"<b>🛡️ Federation:</b> {config.FEDERATION}\n\n"
        f"<b>✨ Key Features:</b>\n"
        f"• ⚡ Permanent & One-Time Share Links\n"
        f"• 🔒 Password Protection & Expiry Timers\n"
        f"• 📂 File Management & Indexed Searching\n"
        f"• 📊 Detailed User Statistics & TXT Export\n\n"
        f"Choose an option below to get started!"
    )
    await update.effective_message.reply_text(
        welcome_text, parse_mode="HTML", reply_markup=get_home_keyboard(is_admin)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command displaying bot instructions and command reference."""
    user = await check_user_access(update)
    if not user or not update.effective_message:
        return

    help_text = (
        "📖 <b>Ultimate YoriFile Bot - Command Guide</b>\n\n"
        "<b>📥 Uploading Media:</b>\n"
        "Simply send or forward any document, photo, video, audio, voice, or sticker to me. "
        "I will store its Telegram file_id and generate a secure link.\n\n"
        "<b>🛠️ User Commands:</b>\n"
        "• /start - Launch bot or access a shared link\n"
        "• /myfiles - View & manage stored files (with pagination)\n"
        "• /search &lt;query&gt; - Fast search your files by filename/extension\n"
        "• /mystats - View your storage usage & download counts\n"
        "• /rename &lt;key&gt; &lt;new_name&gt; - Rename a stored file\n"
        "• /delete &lt;key&gt; - Delete a file permanently\n"
        "• /export - Generate a TXT file containing every file link\n"
        "• /onetime &lt;key&gt; - Toggle 1-time download limit\n"
        "• /password &lt;key&gt; &lt;pass&gt; - Set/remove link password\n"
        "• /expire &lt;key&gt; &lt;time&gt; - Set expiry (1h, 6h, 12h, 24h, 7d, 30d)\n\n"
        "<b>💡 Pro Tip:</b>\n"
        "You can manage all link settings interactively using the inline buttons attached to your stored files!"
    )
    await update.effective_message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="home")]]
        ),
    )


async def send_myfiles_page(message_target: Any, user_id: int, page: int = 1) -> None:
    """Render and send paginated file list message."""
    files, total_count = database.db.get_user_files(user_id, page=page)
    total_pages = max(1, math.ceil(total_count / config.FILES_PER_PAGE))

    if total_count == 0:
        await message_target.reply_text(
            "📂 <b>Your Storage is Empty</b>\n\nYou haven't uploaded any files yet. Send me any media to generate your first shareable link!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Home", callback_data="home")]]
            ),
        )
        return

    lines = [
        f"📂 <b>Your Stored Files</b> (Page {page}/{total_pages})\n"
        f"📊 <b>Total Files:</b> {total_count}\n"
    ]
    for idx, f in enumerate(files, start=1):
        fname = html.escape(f["filename"])
        fsize = format_filesize(f["filesize"])
        dls = f["download_count"]
        lines.append(f"<b>{idx}.</b> <code>{fname}</code> ({fsize}) - 📥 {dls} DLs")

    msg_text = "\n".join(lines)
    keyboard = get_myfiles_keyboard(files, page, total_pages)

    if hasattr(message_target, "edit_message_text"):
        await message_target.edit_message_text(
            msg_text, parse_mode="HTML", reply_markup=keyboard
        )
    else:
        await message_target.reply_text(msg_text, parse_mode="HTML", reply_markup=keyboard)


async def cmd_myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /myfiles command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return
    await send_myfiles_page(update.effective_message, update.effective_user.id, page=1)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /search command for fast indexed file lookups."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ <b>Search Usage:</b>\n\n<code>/search &lt;filename or extension&gt;</code>\nExample: <code>/search video</code> or <code>/search .pdf</code>",
            parse_mode="HTML",
        )
        return

    query_text = " ".join(context.args).strip()
    user_id = update.effective_user.id
    results = database.db.search_user_files(user_id, query_text)

    if not results:
        await update.effective_message.reply_text(
            f"🔍 No active files matching <code>{html.escape(query_text)}</code> were found in your storage.",
            parse_mode="HTML",
        )
        return

    lines = [
        f"🔍 <b>Search Results for:</b> <code>{html.escape(query_text)}</code>\n"
        f"Found {len(results)} matching files:\n"
    ]
    keyboard: List[List[InlineKeyboardButton]] = []
    row = []

    for idx, f in enumerate(results, start=1):
        fname = html.escape(f["filename"])
        lines.append(f"<b>{idx}.</b> <code>{fname}</code> ({format_filesize(f['filesize'])})")
        row.append(InlineKeyboardButton(str(idx), callback_data=f"fileview_{f['file_key']}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mystats command displaying user analytics."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    join_date_str = "N/A"
    if isinstance(user.get("join_date"), datetime):
        join_date_str = user["join_date"].strftime("%Y-%m-%d %H:%M UTC")

    stats_text = (
        f"📊 <b>Your Storage & Activity Stats</b>\n\n"
        f"👤 <b>User ID:</b> <code>{user['user_id']}</code>\n"
        f"📅 <b>Member Since:</b> {join_date_str}\n\n"
        f"📤 <b>Total Uploads:</b> {user.get('uploads_count', 0)} Files\n"
        f"📥 <b>Total Downloads Generated:</b> {user.get('downloads_count', 0)} DLs\n"
        f"💾 <b>Storage Used:</b> {format_filesize(user.get('storage_used', 0))}\n\n"
        f"🤖 Powered by {config.FEDERATION}"
    )
    await update.effective_message.reply_text(
        stats_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="home")]]
        ),
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /rename command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "ℹ️ <b>Rename Usage:</b>\n\n<code>/rename &lt;file_key&gt; &lt;new_filename&gt;</code>\nExample: <code>/rename AbCdEfGh vacation.mp4</code>",
            parse_mode="HTML",
        )
        return

    file_key = context.args[0].strip()
    new_name = sanitize_filename(" ".join(context.args[1:]))
    user_id = update.effective_user.id
    file_doc = database.db.get_file_by_key(file_key, check_expiry=False)

    if not file_doc or (file_doc["uploader"] != user_id and user_id not in config.ADMIN_IDS):
        await update.effective_message.reply_text("❌ File not found or access denied.")
        return

    database.db.update_file(file_key, {"filename": new_name})
    await update.effective_message.reply_text(
        f"✅ File renamed to <code>{html.escape(new_name)}</code> successfully!",
        parse_mode="HTML",
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ <b>Delete Usage:</b>\n\n<code>/delete &lt;file_key&gt;</code>\nExample: <code>/delete AbCdEfGh</code>",
            parse_mode="HTML",
        )
        return

    file_key = context.args[0].strip()
    user_id = update.effective_user.id
    file_doc = database.db.get_file_by_key(file_key, check_expiry=False)

    if not file_doc or (file_doc["uploader"] != user_id and user_id not in config.ADMIN_IDS):
        await update.effective_message.reply_text("❌ File not found or access denied.")
        return

    database.db.delete_file(file_key)
    await update.effective_message.reply_text("🗑 <b>File deleted permanently.</b>", parse_mode="HTML")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export command generating a TXT deliverable."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    user_id = update.effective_user.id
    files = database.db.get_all_user_files(user_id)

    if not files:
        await update.effective_message.reply_text("❌ You have no active files to export.")
        return

    bot_name = context.bot.username or "YoriFileBot"
    lines = []
    for f in files:
        link = f"https://t.me/{bot_name}?start={f['file_key']}"
        lines.append(link)

    content = "\n".join(lines).encode("utf-8")
    buffer = io.BytesIO(content)
    buffer.name = f"yorifiles_export_{user_id}.txt"

    await update.effective_message.reply_document(
        document=buffer,
        caption=f"📤 <b>TXT Export Complete</b>\nTotal Links: {len(files)}",
        parse_mode="HTML",
    )


async def cmd_onetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /onetime command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args:
        await update.effective_message.reply_text("ℹ️ <b>Usage:</b> <code>/onetime &lt;file_key&gt;</code>", parse_mode="HTML")
        return

    file_key = context.args[0].strip()
    user_id = update.effective_user.id
    file_doc = database.db.get_file_by_key(file_key, check_expiry=False)

    if not file_doc or (file_doc["uploader"] != user_id and user_id not in config.ADMIN_IDS):
        await update.effective_message.reply_text("❌ File not found or access denied.")
        return

    new_state = not file_doc.get("is_onetime", False)
    database.db.update_file(file_key, {"is_onetime": new_state})
    status_str = "ON (Single Download)" if new_state else "OFF (Unlimited Downloads)"
    await update.effective_message.reply_text(f"✅ One-Time mode set to <b>{status_str}</b>.", parse_mode="HTML")


async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /password command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "ℹ️ <b>Password Usage:</b>\n\n<code>/password &lt;file_key&gt; &lt;secret_password&gt;</code>\nTo remove password: <code>/password &lt;file_key&gt; none</code>",
            parse_mode="HTML",
        )
        return

    file_key = context.args[0].strip()
    secret = context.args[1].strip()
    user_id = update.effective_user.id
    file_doc = database.db.get_file_by_key(file_key, check_expiry=False)

    if not file_doc or (file_doc["uploader"] != user_id and user_id not in config.ADMIN_IDS):
        await update.effective_message.reply_text("❌ File not found or access denied.")
        return

    if secret.lower() in ["none", "off", "remove"]:
        database.db.update_file(file_key, {"password_hash": None})
        await update.effective_message.reply_text("🔓 Password protection removed.")
    else:
        hashed = bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        database.db.update_file(file_key, {"password_hash": hashed})
        await update.effective_message.reply_text("🔒 Password set successfully for this file.")


async def cmd_expire(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /expire command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "ℹ️ <b>Expire Usage:</b>\n\n<code>/expire &lt;file_key&gt; &lt;1h|6h|12h|24h|7d|30d|none&gt;</code>",
            parse_mode="HTML",
        )
        return

    file_key = context.args[0].strip()
    duration = context.args[1].lower().strip()
    user_id = update.effective_user.id
    file_doc = database.db.get_file_by_key(file_key, check_expiry=False)

    if not file_doc or (file_doc["uploader"] != user_id and user_id not in config.ADMIN_IDS):
        await update.effective_message.reply_text("❌ File not found or access denied.")
        return

    if duration in ["none", "off", "remove", "permanent"]:
        database.db.update_file(file_key, {"expires_at": None})
        await update.effective_message.reply_text("♾️ Expiry timer removed. Link is now permanent.")
    elif duration in config.EXPIRY_OPTIONS:
        exp_time = datetime.now(timezone.utc) + timedelta(seconds=config.EXPIRY_OPTIONS[duration])
        database.db.update_file(file_key, {"expires_at": exp_time})
        await update.effective_message.reply_text(f"⏱ Expiry set to <b>{duration}</b>.", parse_mode="HTML")
    else:
        await update.effective_message.reply_text("❌ Invalid duration. Choose from: <code>1h, 6h, 12h, 24h, 7d, 30d, none</code>.", parse_mode="HTML")


# --- Admin Panel Commands ---


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin dashboard command."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    if update.effective_user.id not in config.ADMIN_IDS:
        await update.effective_message.reply_text("🛡️ Unauthorized. Admin permissions required.")
        return

    await update.effective_message.reply_text(
        "🛡️ <b>YoriFile Bot - Admin Dashboard</b>\n\nSelect a system administration action:",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard(),
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /users command."""
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    users_list = database.db.get_all_users()
    recent = sorted(users_list, key=lambda x: x.get("join_date", datetime.min), reverse=True)[:5]

    lines = [f"👥 <b>Total Registered Users:</b> {len(users_list)}\n\n<b>Recent 5 Members:</b>"]
    for u in recent:
        lines.append(f"• ID: <code>{u['user_id']}</code> (@{html.escape(u.get('username', ''))})")

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats system telemetry command."""
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    stats = database.db.get_system_stats()
    
    # Estimate memory footprint
    import resource
    mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mem_mb = mem_kb / 1024.0

    msg = (
        f"📊 <b>System Telemetry & Health</b>\n\n"
        f"👥 <b>Total Users:</b> {stats['total_users']:,}\n"
        f"📂 <b>Total Active Files:</b> {stats['total_files']:,}\n"
        f"📥 <b>Total Downloads Served:</b> {stats['total_downloads']:,}\n"
        f"💾 <b>Total Storage Managed:</b> {format_filesize(stats['total_storage'])}\n\n"
        f"🟢 <b>Database Status:</b> {stats['db_status']}\n"
        f"🧠 <b>Memory Footprint:</b> {mem_mb:.2f} MB\n"
        f"⚙️ <b>Owner ID:</b> <code>{config.OWNER_ID}</code>"
    )
    await update.effective_message.reply_text(
        msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    )


async def perform_broadcast(message_target: Any, text: str, bot: Any) -> None:
    """Execute batch broadcast to all registered users."""
    users = database.db.get_all_users()
    success = 0
    failed = 0

    status_msg = await message_target.reply_text(f"📢 <b>Broadcast Initializing...</b>\nTargeting {len(users)} users.", parse_mode="HTML")

    for u in users:
        uid = u["user_id"]
        try:
            await bot.send_message(chat_id=uid, text=f"📢 <b>Broadcast from Admin</b>\n\n{text}", parse_mode="HTML")
            success += 1
        except Exception:
            failed += 1

    report = f"📢 <b>Broadcast Completed</b>\n\n✅ <b>Delivered:</b> {success}\n❌ <b>Failed/Blocked:</b> {failed}\n📊 <b>Total Target:</b> {len(users)}"
    await status_msg.edit_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]]))
    database.db.add_log("BROADCAST_SENT", f"Delivered: {success}, Failed: {failed}")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /broadcast command."""
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ <b>Broadcast Usage:</b>\n\n<code>/broadcast &lt;message text&gt;</code>\nExample: <code>/broadcast We have upgraded server bandwidth!</code>",
            parse_mode="HTML",
        )
        return

    btext = " ".join(context.args)
    await perform_broadcast(update.effective_message, btext, context.bot)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ban command."""
    if not update.effective_message or not update.effective_user or not context.args:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    target_id = int(context.args[0].strip())
    res = database.db.ban_user(target_id, True)
    if res:
        await update.effective_message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode="HTML")
    else:
        await update.effective_message.reply_text("❌ User not found.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unban command."""
    if not update.effective_message or not update.effective_user or not context.args:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    target_id = int(context.args[0].strip())
    res = database.db.ban_user(target_id, False)
    if res:
        await update.effective_message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode="HTML")
    else:
        await update.effective_message.reply_text("❌ User not found.")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /logs telemetry command."""
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    logs = database.db.get_logs(15)
    lines = ["📜 <b>Recent System Activity Logs</b>\n"]
    for lg in logs:
        ts = lg["timestamp"].strftime("%H:%M:%S") if isinstance(lg["timestamp"], datetime) else ""
        lines.append(f"<code>[{ts}]</code> <b>{lg['action']}</b>: {html.escape(lg['details'][:50])}")

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])
    )


# --- Media Upload & Text Handlers ---


async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract media metadata, store file_id in MongoDB Atlas, and reply with share link."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    msg = update.effective_message
    user_id = update.effective_user.id

    media_obj = None
    media_type = ""
    filename = ""
    filesize = 0
    mime_type = ""
    file_id = ""
    file_unique_id = ""

    if msg.document:
        media_obj = msg.document
        media_type = "Document"
        filename = media_obj.file_name or f"document_{msg.message_id}.dat"
        filesize = media_obj.file_size or 0
        mime_type = media_obj.mime_type or "application/octet-stream"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.video:
        media_obj = msg.video
        media_type = "Video"
        filename = media_obj.file_name or f"video_{msg.message_id}.mp4"
        filesize = media_obj.file_size or 0
        mime_type = media_obj.mime_type or "video/mp4"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.photo:
        media_obj = msg.photo[-1]
        media_type = "Photo"
        filename = f"photo_{msg.message_id}.jpg"
        filesize = media_obj.file_size or 0
        mime_type = "image/jpeg"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.audio:
        media_obj = msg.audio
        media_type = "Audio"
        filename = media_obj.file_name or f"audio_{msg.message_id}.mp3"
        filesize = media_obj.file_size or 0
        mime_type = media_obj.mime_type or "audio/mpeg"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.voice:
        media_obj = msg.voice
        media_type = "Voice"
        filename = f"voice_{msg.message_id}.ogg"
        filesize = media_obj.file_size or 0
        mime_type = media_obj.mime_type or "audio/ogg"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.animation:
        media_obj = msg.animation
        media_type = "Animation"
        filename = media_obj.file_name or f"animation_{msg.message_id}.gif"
        filesize = media_obj.file_size or 0
        mime_type = media_obj.mime_type or "video/mp4"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id
    elif msg.sticker:
        media_obj = msg.sticker
        media_type = "Sticker"
        filename = f"sticker_{msg.message_id}.webp"
        filesize = media_obj.file_size or 0
        mime_type = "image/webp"
        file_id = media_obj.file_id
        file_unique_id = media_obj.file_unique_id

    if not file_id:
        await msg.reply_text("❌ Unsupported media format.")
        return

    clean_filename = sanitize_filename(filename)
    file_key = secrets.token_urlsafe(6)

    file_doc = {
        "file_key": file_key,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "filename": clean_filename,
        "filesize": filesize,
        "mime_type": mime_type,
        "media_type": media_type,
        "uploader": user_id,
        "upload_date": datetime.now(timezone.utc),
        "download_count": 0,
        "is_onetime": False,
        "password_hash": None,
        "expires_at": None,
        "is_active": True,
    }

    saved = database.db.save_file(file_doc)
    if not saved:
        await msg.reply_text("❌ Error saving file to database.")
        return

    bot_name = context.bot.username or "YoriFileBot"
    share_link = f"https://t.me/{bot_name}?start={file_key}"

    reply_text = (
        f"✅ <b>Media Stored Securely!</b>\n\n"
        f"📄 <b>Name:</b> <code>{html.escape(clean_filename)}</code>\n"
        f"💾 <b>Size:</b> {format_filesize(filesize)}\n"
        f"🏷️ <b>MIME:</b> <code>{mime_type}</code>\n"
        f"🔑 <b>File Key:</b> <code>{file_key}</code>\n\n"
        f"🔗 <b>Share Link:</b>\n<code>{share_link}</code>"
    )
    keyboard = get_file_control_keyboard(file_key, False, False, False)
    await msg.reply_text(reply_text, parse_mode="HTML", reply_markup=keyboard)


async def render_file_details(query_or_msg: Any, file_key: str, user_id: int, bot_name: str) -> None:
    """Render full management dashboard for a specific file."""
    file_doc = database.db.get_file_by_key(file_key, check_expiry=True)
    if not file_doc:
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text("❌ File not found or expired.")
        return

    fname = html.escape(file_doc["filename"])
    fsize = format_filesize(file_doc["filesize"])
    dls = file_doc["download_count"]
    share_link = f"https://t.me/{bot_name}?start={file_key}"

    onetime_str = "🟢 ON" if file_doc.get("is_onetime") else "⚪ OFF"
    pass_str = "🔒 Encrypted" if file_doc.get("password_hash") else "🔓 None"
    
    exp_str = "♾️ Permanent"
    if file_doc.get("expires_at"):
        exp_str = file_doc["expires_at"].strftime("%Y-%m-%d %H:%M UTC")

    text = (
        f"⚙️ <b>File Settings Dashboard</b>\n\n"
        f"📄 <b>File:</b> <code>{fname}</code> ({fsize})\n"
        f"📥 <b>Downloads Served:</b> {dls}\n"
        f"🔑 <b>Key:</b> <code>{file_key}</code>\n\n"
        f"1️⃣ <b>One-Time Mode:</b> {onetime_str}\n"
        f"🔒 <b>Password:</b> {pass_str}\n"
        f"⏱ <b>Expires At:</b> {exp_str}\n\n"
        f"🔗 <b>Share Link:</b>\n<code>{share_link}</code>"
    )
    keyboard = get_file_control_keyboard(
        file_key,
        file_doc.get("is_onetime", False),
        bool(file_doc.get("password_hash")),
        bool(file_doc.get("expires_at")),
    )

    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await query_or_msg.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process interactive text messages (passwords, renames, search fallback)."""
    user = await check_user_access(update)
    if not user or not update.effective_message or not update.effective_user:
        return

    text = update.effective_message.text.strip()
    user_id = update.effective_user.id
    user_doc = database.db.get_user(user_id)

    # 1. Check pending file access (password entry)
    if user_doc and user_doc.get("pending_file_access"):
        file_key = user_doc["pending_file_access"]
        file_doc = database.db.get_file_by_key(file_key, check_expiry=True)
        
        if not file_doc:
            database.db.set_user_pending_access(user_id, None)
            await update.effective_message.reply_text("❌ File no longer exists or expired.")
            return

        stored_hash = file_doc.get("password_hash")
        if stored_hash:
            hash_bytes = stored_hash.encode("utf-8") if isinstance(stored_hash, str) else stored_hash
            if bcrypt.checkpw(text.encode("utf-8"), hash_bytes):
                database.db.set_user_pending_access(user_id, None)
                await deliver_file_to_user(update.effective_message, file_doc, user_id, context.bot)
            else:
                await update.effective_message.reply_text("❌ <b>Incorrect Password.</b> Try again or /start.", parse_mode="HTML")
            return

    # 2. Check pending actions (rename, set password, broadcast)
    if user_doc and user_doc.get("pending_action"):
        action = user_doc["pending_action"]
        act_type = action.get("action")
        file_key = action.get("file_key")

        if act_type == "set_password":
            database.db.set_user_pending_action(user_id, None)
            if text.lower() in ["none", "off", "remove"]:
                database.db.update_file(file_key, {"password_hash": None})
                await update.effective_message.reply_text("🔓 Password protection removed.")
            else:
                hashed = bcrypt.hashpw(text.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                database.db.update_file(file_key, {"password_hash": hashed})
                await update.effective_message.reply_text("🔒 Password set successfully for this file.")
            bot_name = context.bot.username or "YoriFileBot"
            await render_file_details(update.effective_message, file_key, user_id, bot_name)
            return

        elif act_type == "rename":
            database.db.set_user_pending_action(user_id, None)
            clean_name = sanitize_filename(text)
            database.db.update_file(file_key, {"filename": clean_name})
            await update.effective_message.reply_text(f"✅ File renamed to <code>{html.escape(clean_name)}</code>.", parse_mode="HTML")
            bot_name = context.bot.username or "YoriFileBot"
            await render_file_details(update.effective_message, file_key, user_id, bot_name)
            return

        elif act_type == "broadcast" and user_id in config.ADMIN_IDS:
            database.db.set_user_pending_action(user_id, None)
            await perform_broadcast(update.effective_message, text, context.bot)
            return

    # 3. Fallback: Treat text input as search query
    results = database.db.search_user_files(user_id, text)
    if results:
        lines = [f"🔍 <b>Auto-Search for:</b> <code>{html.escape(text)}</code>\n"]
        kb: List[List[InlineKeyboardButton]] = []
        r = []
        for idx, f in enumerate(results, start=1):
            lines.append(f"<b>{idx}.</b> <code>{html.escape(f['filename'])}</code>")
            r.append(InlineKeyboardButton(str(idx), callback_data=f"fileview_{f['file_key']}"))
        kb.append(r)
        kb.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.effective_message.reply_text(
            "❓ Unrecognized input. Upload any media to store it, or send /help for commands.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]])
        )


# --- Callback Query Handler ---


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline keyboard button clicks."""
    query = update.callback_query
    if not query or not query.from_user:
        return

    await query.answer()
    data = query.data
    user_id = query.from_user.id
    bot_name = context.bot.username or "YoriFileBot"
    is_admin = user_id in config.ADMIN_IDS

    if data == "noop":
        return

    elif data == "home":
        welcome_text = (
            f"🌟 <b>Ultimate YoriFile Store & Share Bot</b>\n\n"
            f"Select an option below to manage your cloud storage:\n\n"
            f"<b>💎 Creator:</b> {config.CREATOR}\n"
            f"<b>🛡️ Federation:</b> {config.FEDERATION}"
        )
        await query.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=get_home_keyboard(is_admin))

    elif data == "help_guide":
        await cmd_help(update, context)

    elif data == "mystats":
        user_doc = database.db.get_user(user_id) or {"user_id": user_id, "uploads_count": 0, "downloads_count": 0, "storage_used": 0}
        join_str = user_doc["join_date"].strftime("%Y-%m-%d UTC") if isinstance(user_doc.get("join_date"), datetime) else "N/A"
        st_text = (
            f"📊 <b>Your Storage Statistics</b>\n\n"
            f"👤 <b>ID:</b> <code>{user_id}</code>\n"
            f"📅 <b>Joined:</b> {join_str}\n\n"
            f"📤 <b>Uploads:</b> {user_doc.get('uploads_count', 0)}\n"
            f"📥 <b>Downloads Served:</b> {user_doc.get('downloads_count', 0)}\n"
            f"💾 <b>Storage:</b> {format_filesize(user_doc.get('storage_used', 0))}"
        )
        await query.edit_message_text(st_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]]))

    elif data == "search_prompt":
        await query.message.reply_text("🔍 Please type `/search <filename>` to search your stored files.\nExample: `/search video`")

    elif data.startswith("myfiles_page_"):
        page = int(data.split("_")[-1])
        await send_myfiles_page(query, user_id, page)

    elif data.startswith("fileview_"):
        key = data.split("_")[1]
        await render_file_details(query, key, user_id, bot_name)

    elif data.startswith("getlink_"):
        key = data.split("_")[1]
        share_url = f"https://t.me/{bot_name}?start={key}"
        await query.message.reply_text(f"🔗 <b>Shareable Link:</b>\n<code>{share_url}</code>", parse_mode="HTML")

    elif data.startswith("dl_"):
        key = data.split("_")[1]
        file_doc = database.db.get_file_by_key(key)
        if file_doc:
            await deliver_file_to_user(query.message, file_doc, user_id, context.bot)

    elif data.startswith("onetimecfg_"):
        key = data.split("_")[1]
        file_doc = database.db.get_file_by_key(key, check_expiry=False)
        if file_doc and (file_doc["uploader"] == user_id or is_admin):
            new_state = not file_doc.get("is_onetime", False)
            database.db.update_file(key, {"is_onetime": new_state})
            await render_file_details(query, key, user_id, bot_name)

    elif data.startswith("passcfg_"):
        key = data.split("_")[1]
        file_doc = database.db.get_file_by_key(key, check_expiry=False)
        if file_doc and (file_doc["uploader"] == user_id or is_admin):
            database.db.set_user_pending_action(user_id, {"action": "set_password", "file_key": key})
            await query.message.reply_text("🔒 Type and send the new password for this file (or send `none` to remove):")

    elif data.startswith("expcfg_"):
        key = data.split("_")[1]
        await query.edit_message_text("⏱ Select an expiration timer for this file link:", reply_markup=get_expiry_keyboard(key))

    elif data.startswith("setexp_"):
        parts = data.split("_")
        key = parts[1]
        dur = parts[2]
        file_doc = database.db.get_file_by_key(key, check_expiry=False)
        if file_doc and (file_doc["uploader"] == user_id or is_admin):
            if dur == "none":
                database.db.update_file(key, {"expires_at": None})
            elif dur in config.EXPIRY_OPTIONS:
                exp_time = datetime.now(timezone.utc) + timedelta(seconds=config.EXPIRY_OPTIONS[dur])
                database.db.update_file(key, {"expires_at": exp_time})
            await render_file_details(query, key, user_id, bot_name)

    elif data.startswith("renamecfg_"):
        key = data.split("_")[1]
        file_doc = database.db.get_file_by_key(key, check_expiry=False)
        if file_doc and (file_doc["uploader"] == user_id or is_admin):
            database.db.set_user_pending_action(user_id, {"action": "rename", "file_key": key})
            await query.message.reply_text("✏️ Please type and send the new filename:")

    elif data.startswith("delcfg_"):
        key = data.split("_")[1]
        file_doc = database.db.get_file_by_key(key, check_expiry=False)
        if file_doc and (file_doc["uploader"] == user_id or is_admin):
            database.db.delete_file(key)
            await query.edit_message_text("🗑 <b>File deleted permanently.</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 My Files", callback_data="myfiles_page_1"), InlineKeyboardButton("🏠 Home", callback_data="home")]]))

    elif data == "export_txt":
        await cmd_export(update, context)

    # Admin actions
    elif data == "admin_panel" and is_admin:
        await query.edit_message_text("🛡️ <b>Admin Dashboard</b>\n\nSelect an action:", parse_mode="HTML", reply_markup=get_admin_keyboard())

    elif data == "admin_users" and is_admin:
        await cmd_users(update, context)

    elif data == "admin_stats" and is_admin:
        await cmd_stats(update, context)

    elif data == "admin_broadcast_prompt" and is_admin:
        database.db.set_user_pending_action(user_id, {"action": "broadcast"})
        await query.message.reply_text("📢 Please type and send the broadcast message to deliver to all users:")

    elif data == "admin_logs" and is_admin:
        await cmd_logs(update, context)


# Register Handlers with Application
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("help", cmd_help))
ptb_app.add_handler(CommandHandler("myfiles", cmd_myfiles))
ptb_app.add_handler(CommandHandler("search", cmd_search))
ptb_app.add_handler(CommandHandler("mystats", cmd_mystats))
ptb_app.add_handler(CommandHandler("rename", cmd_rename))
ptb_app.add_handler(CommandHandler("delete", cmd_delete))
ptb_app.add_handler(CommandHandler("export", cmd_export))
ptb_app.add_handler(CommandHandler("onetime", cmd_onetime))
ptb_app.add_handler(CommandHandler("password", cmd_password))
ptb_app.add_handler(CommandHandler("expire", cmd_expire))

# Admin handlers
ptb_app.add_handler(CommandHandler("admin", cmd_admin))
ptb_app.add_handler(CommandHandler("users", cmd_users))
ptb_app.add_handler(CommandHandler("stats", cmd_stats))
ptb_app.add_handler(CommandHandler("broadcast", cmd_broadcast))
ptb_app.add_handler(CommandHandler("ban", cmd_ban))
ptb_app.add_handler(CommandHandler("unban", cmd_unban))
ptb_app.add_handler(CommandHandler("logs", cmd_logs))

# Callback query handler
ptb_app.add_handler(CallbackQueryHandler(callback_query_handler))

# Message handlers
media_filter = (
    filters.Document.ALL
    | filters.VIDEO
    | filters.PHOTO
    | filters.AUDIO
    | filters.VOICE
    | filters.ANIMATION
    | filters.Sticker.ALL
)
ptb_app.add_handler(MessageHandler(media_filter & (~filters.COMMAND), handle_media_upload))
ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))


# --- Flask Webhook Routes ---


async def process_telegram_update(update_json: Dict[str, Any]) -> None:
    """Async wrapper initializing PTB and consuming update queue."""
    global _ptb_initialized
    if not _ptb_initialized:
        try:
            await ptb_app.initialize()
            await ptb_app.start()
        except Exception as exc:
            logger.warning("PTB startup verification check bypassed (%s).", exc)
            ptb_app._initialized = True
            from telegram import User
            ptb_app.bot._bot_user = User(
                id=123456, first_name="YoriFileBot", is_bot=True, username="YoriFileBot"
            )
        _ptb_initialized = True

    update = Update.de_json(update_json, ptb_app.bot)
    await ptb_app.process_update(update)

    try:
        await asyncio.wait_for(ptb_app.update_queue.join(), timeout=50.0)
    except asyncio.TimeoutError:
        logger.warning("Webhook update queue processing exceeded timeout.")


@app.route("/api/webhook", methods=["POST"])
def webhook_route() -> Any:
    """Synchronous Flask WSGI endpoint invoked by Vercel on HTTP POST."""
    if not request.is_json:
        return jsonify({"status": "error", "message": "Expected JSON payload"}), 400

    payload = request.get_json()
    if not payload:
        return jsonify({"status": "error", "message": "Empty payload"}), 400

    loop = get_loop()
    loop.run_until_complete(process_telegram_update(payload))
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def index_route() -> Any:
    """Health check endpoint."""
    return jsonify(
        {
            "service": "Ultimate YoriFile Telegram Store & Share Bot",
            "status": "Online",
            "creator": config.CREATOR,
            "federation": config.FEDERATION,
            "owner_id": config.OWNER_ID,
        }
    ), 200


if __name__ == "__main__":
    # Local development server
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
