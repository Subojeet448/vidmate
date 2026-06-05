#!/usr/bin/env python3
"""
Universal Media Downloader Bot
Developer: HARSHU !!
Version: 3.0 - Ultimate Edition
"""

import os
import re
import json
import logging
import asyncio
import time
import shutil
import csv
import io
import subprocess
import glob
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple
import tempfile
import requests
from urllib.parse import urlparse
from logging.handlers import RotatingFileHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus
import yt_dlp

# ── Logging ───────────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_fh = RotatingFileHandler("logs/errors.log", maxBytes=5 * 1024 * 1024, backupCount=3)
_fh.setLevel(logging.WARNING)
_fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_fh)

# ── Configuration ─────────────────────────────────────────────────────────────────
BOT_TOKEN        = "8766366480:AAENn5hPjWWWeW12SqmmXd0Co1Aa8G50Ukg"
ADMIN_ID         = 6535364725
LOG_CHANNEL_ID          = None  # Set to an int channel ID to enable event forwarding
MAX_FILE_SIZE_MB        = 50    # Telegram bot upload limit in MB
COOLDOWN_SECONDS        = 10    # Seconds between downloads per user
MAX_CONCURRENT_DOWNLOADS = 3    # Global concurrency cap
MAX_PLAYLIST_VIDEOS     = 5     # Max videos downloaded from a playlist
DATA_FILE               = "bot_data.json"

# ── Global state ──────────────────────────────────────────────────────────────────
user_cooldowns: Dict[int, float] = {}
maintenance_mode = False
waiting_for_input: Dict[int, str] = {}
bot_start_time = datetime.now()
user_temp_data: Dict = {}

# ── Download queue state ──────────────────────────────────────────────────────────
_user_locks: Dict[int, asyncio.Lock] = {}       # one lock per user
_global_dl_semaphore: Optional[asyncio.Semaphore] = None  # initialised in main()
_queued_count: int = 0                          # running total for position display

# ── Data persistence ──────────────────────────────────────────────────────────────
def _default_data() -> dict:
    return {
        "users": {},
        "total_downloads": 0,
        "force_channels": [],
        "banned_users": [],
        "banner_url": None,
        "banner_file_id": None,
        "maintenance_mode": False,
        "download_history": [],
    }

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in _default_data().items():
                data.setdefault(key, val)
            return data
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    return _default_data()

def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

# ── Initialize data ───────────────────────────────────────────────────────────────
bot_data       = load_data()
force_channels = bot_data.get("force_channels", [])
BANNER_URL     = bot_data.get("banner_url")
BANNER_FILE_ID = bot_data.get("banner_file_id")
maintenance_mode = bot_data.get("maintenance_mode", False)

# ── Helper utilities ──────────────────────────────────────────────────────────────
def clean_filename(name: str) -> str:
    """Remove @mentions, URLs, weird symbols; normalize whitespace."""
    name = re.sub(r"@\w+", "", name)
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:64] if name else "video"

def detect_platform(url: str) -> str:
    """Return a labelled platform name detected from a URL."""
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "🎬 YouTube"
    if "instagram.com" in u:
        return "📸 Instagram"
    if "tiktok.com" in u:
        return "🎵 TikTok"
    if "twitter.com" in u or "x.com" in u:
        return "🐦 Twitter/X"
    if "facebook.com" in u or "fb.com" in u or "fb.watch" in u:
        return "📘 Facebook"
    return "🌐 Web"

def is_playlist_url(url: str) -> bool:
    """Return True if the URL points to a YouTube playlist."""
    u = url.lower()
    return ("youtube.com" in u or "youtu.be" in u) and ("list=" in u or "playlist" in u)

def record_download_history(user_id: int, url: str, platform: str):
    """Append an entry to the global download history (capped at 1 000)."""
    if "download_history" not in bot_data:
        bot_data["download_history"] = []
    bot_data["download_history"].append({
        "user_id": str(user_id),
        "url": url[:100],
        "platform": platform,
        "timestamp": datetime.now().isoformat(),
    })
    if len(bot_data["download_history"]) > 1000:
        bot_data["download_history"] = bot_data["download_history"][-1000:]
    save_data(bot_data)

async def send_log(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Forward an event log to LOG_CHANNEL_ID when configured."""
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"send_log failed: {e}")

def startup_check():
    """Print a startup report verifying all dependencies."""
    sep = "=" * 50
    print(f"\n{sep}")
    print("  🚀  Universal Media Downloader Bot v3.0")
    print("       Developer: HARSHU !!")
    print(sep)

    ytdlp_ok = ffmpeg_ok = False

    if BOT_TOKEN:
        print("✅  BOT_TOKEN         — found")
    else:
        print("❌  BOT_TOKEN         — MISSING (bot will not start)")

    if ADMIN_ID:
        print(f"✅  ADMIN_ID          — {ADMIN_ID}")
    else:
        print("⚠️  ADMIN_ID          — not set")

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        print("✅  ffmpeg ready      — available")
        ffmpeg_ok = True
    except Exception:
        print("⚠️  ffmpeg            — not found (MP3 conversion may fail)")

    try:
        ver = yt_dlp.version.__version__
        print(f"✅  yt-dlp ready      — v{ver}")
        ytdlp_ok = True
    except Exception:
        print("❌  yt-dlp            — not installed")

    print(f"✅  LOG_CHANNEL_ID    — {LOG_CHANNEL_ID}" if LOG_CHANNEL_ID
          else "ℹ️  LOG_CHANNEL_ID    — not set (event logs disabled)")
    print(f"✅  Queue limit        — {MAX_CONCURRENT_DOWNLOADS} concurrent downloads")
    print(f"✅  Playlist limit     — first {MAX_PLAYLIST_VIDEOS} videos max")
    print(sep)
    if BOT_TOKEN and ytdlp_ok:
        print("  🟢  Bot Online — ✅ polling active")
    else:
        print("  🔴  Bot may not start — check errors above")
    print(f"{sep}\n")

# ── UserManager ───────────────────────────────────────────────────────────────────
class UserManager:

    @staticmethod
    def register_user(user_id: int, username: str = None, full_name: str = None):
        uid = str(user_id)
        if uid not in bot_data["users"]:
            bot_data["users"][uid] = {
                "username": username,
                "full_name": full_name,
                "join_date": datetime.now().isoformat(),
                "total_downloads": 0,
                "last_active": datetime.now().isoformat(),
                "verified": False,
            }
        else:
            bot_data["users"][uid]["last_active"] = datetime.now().isoformat()
            if username:
                bot_data["users"][uid]["username"] = username
            if full_name:
                bot_data["users"][uid]["full_name"] = full_name
        save_data(bot_data)

    @staticmethod
    def increment_downloads(user_id: int):
        uid = str(user_id)
        if uid in bot_data["users"]:
            bot_data["users"][uid]["total_downloads"] += 1
            bot_data["total_downloads"] += 1
            save_data(bot_data)

    @staticmethod
    def set_verified(user_id: int, verified: bool = True):
        uid = str(user_id)
        if uid in bot_data["users"]:
            bot_data["users"][uid]["verified"] = verified
            save_data(bot_data)

    @staticmethod
    def is_verified(user_id: int) -> bool:
        return bot_data["users"].get(str(user_id), {}).get("verified", False)

    @staticmethod
    def is_banned(user_id: int) -> bool:
        return str(user_id) in bot_data.get("banned_users", [])

    @staticmethod
    def ban_user(user_id: int) -> bool:
        uid = str(user_id)
        if uid not in bot_data["banned_users"]:
            bot_data["banned_users"].append(uid)
            save_data(bot_data)
            return True
        return False

    @staticmethod
    def unban_user(user_id: int) -> bool:
        uid = str(user_id)
        if uid in bot_data["banned_users"]:
            bot_data["banned_users"].remove(uid)
            save_data(bot_data)
            return True
        return False

    @staticmethod
    def get_stats():
        total_users = len(bot_data["users"])
        total_downloads = bot_data["total_downloads"]
        today = datetime.now().date()
        active_today = sum(
            1 for u in bot_data["users"].values()
            if datetime.fromisoformat(u["last_active"]).date() == today
        )
        uptime = datetime.now() - bot_start_time
        d = uptime.days
        h = uptime.seconds // 3600
        m = (uptime.seconds % 3600) // 60
        return total_users, total_downloads, active_today, f"{d}d {h}h {m}m"


# ── DownloadManager ───────────────────────────────────────────────────────────────
QUALITY_FORMATS: Dict[str, str] = {
    "360": (
        "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]"
        "/best[height<=360][ext=mp4]/best[height<=360]/best"
    ),
    "480": (
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]"
        "/best[height<=480][ext=mp4]/best[height<=480]/best"
    ),
    "720": (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
        "/best[height<=720][ext=mp4]/best[height<=720]/best"
    ),
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    "mp3":  "bestaudio/best",
}

class DownloadManager:

    @staticmethod
    def _blocking_download(
        url: str, download_dir: str, quality: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Blocking yt-dlp call — always run via asyncio.to_thread."""
        extract_audio = quality == "mp3"
        ydl_opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
            "outtmpl": f"{download_dir}/%(title)s.%(ext)s",
        }

        if extract_audio:
            ydl_opts.update({
                "format": QUALITY_FORMATS["mp3"],
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            ydl_opts["format"] = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
            ydl_opts["merge_output_format"] = "mp4"
            ydl_opts["writethumbnail"] = True

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                title = clean_filename(info.get("title", "video"))
                thumbnail_url = info.get("thumbnail")

                filesize = info.get("filesize") or info.get("filesize_approx") or 0
                if filesize > 200 * 1024 * 1024:
                    return None, "file_too_large", title, None

                ydl.download([url])

                # Find main media file (exclude thumbnail images)
                all_files = list(Path(download_dir).glob("*"))
                media_files = [
                    f for f in all_files
                    if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")
                ]
                chosen = media_files[0] if media_files else (all_files[0] if all_files else None)
                if chosen:
                    return str(chosen), None, title, thumbnail_url
                return None, "unknown", title, None

        except Exception as e:
            err = str(e).lower()
            logger.warning(f"yt-dlp error for {url}: {e}")
            if "private" in err:
                return None, "private", None, None
            if "unsupported" in err:
                return None, "unsupported", None, None
            if "copyright" in err:
                return None, "copyright", None, None
            if "age" in err or "18+" in err:
                return None, "age_restricted", None, None
            if "unavailable" in err or "not available" in err or "video unavailable" in err:
                return None, "unavailable", None, None
            if "geo" in err or "region" in err or "not available in your country" in err:
                return None, "region_blocked", None, None
            if "login" in err or "sign in" in err or "authentication" in err:
                return None, "login_required", None, None
            return None, "unknown", None, None

    async def download(
        self, url: str, download_dir: str, quality: str = "best"
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Async wrapper with 5-minute timeout and 2 auto-retries on transient errors."""
        _retriable = {"unknown", "timeout"}
        last_result: Tuple = (None, "unknown", None, None)

        for attempt in range(3):  # 1 initial + 2 retries
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._blocking_download, url, download_dir, quality),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                result = (None, "timeout", None, None)
            except Exception as e:
                logger.error(f"Download wrapper error: {e}")
                result = (None, "unknown", None, None)

            last_result = result
            error = result[1]

            if error not in _retriable:
                return result          # permanent error or success — stop immediately

            if attempt < 2:
                logger.info(f"Download attempt {attempt + 1} failed ({error}), retrying in {2*(attempt+1)}s…")
                await asyncio.sleep(2 * (attempt + 1))

        return last_result


# ── Initialize managers ───────────────────────────────────────────────────────────
user_manager    = UserManager()
download_manager = DownloadManager()

# ── Subscription helpers ──────────────────────────────────────────────────────────
async def extract_channel_info(text: str) -> tuple:
    text = text.strip()
    if "t.me/+" in text or "telegram.me/+" in text:
        parsed = urlparse(text)
        if "/+" in parsed.path:
            return "private", parsed.path.split("/+")[-1]
    elif "t.me/" in text or "telegram.me/" in text:
        path = urlparse(text).path.strip("/")
        if path and not path.startswith("+"):
            return "public", path
    elif text.startswith("@"):
        return "public", text[1:]
    elif text and not text.startswith("+"):
        return "public", text
    return None, None

async def get_subscription_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    for ch in force_channels:
        if ch.get("type", "public") == "public":
            keyboard.append([InlineKeyboardButton(
                f"📢 Join @{ch['identifier']}",
                url=f"https://t.me/{ch['identifier']}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                "📢 Join Private Channel",
                url=f"https://t.me/+{ch['invite_hash']}"
            )])
    keyboard.append([InlineKeyboardButton("✅ Verify Join",  callback_data="verify_subscription")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh",      callback_data="refresh_subscription")])
    return InlineKeyboardMarkup(keyboard)

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not force_channels:
        return True
    if user_manager.is_verified(user_id):
        return True
    all_joined = True
    for ch in force_channels:
        try:
            if ch.get("type", "public") == "public":
                chat = await context.bot.get_chat(f"@{ch['identifier']}")
                member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user_id)
                if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                    all_joined = False
                    break
        except Exception as e:
            logger.error(f"Subscription check error for {ch}: {e}")
    if all_joined and force_channels:
        user_manager.set_verified(user_id, True)
    return all_joined


# ── Quality keyboard ──────────────────────────────────────────────────────────────
def quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 360p",    callback_data="quality_360"),
         InlineKeyboardButton("💻 480p",    callback_data="quality_480")],
        [InlineKeyboardButton("🖥️ 720p",    callback_data="quality_720"),
         InlineKeyboardButton("🎵 MP3",     callback_data="quality_mp3")],
        [InlineKeyboardButton("❌ Cancel",  callback_data="cancel_download")],
    ])


# ── Core download executor ────────────────────────────────────────────────────────
async def do_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    quality: str,
):
    """
    Full download pipeline with queue system and five-stage progress:
      ⏳ Queue → ⏳ Downloading → 📦 Processing → ⬆️ Uploading → ✅ Done
    Cleans up temp files in all cases.
    """
    global _queued_count
    user = update.effective_user
    chat_id = update.effective_chat.id
    platform = detect_platform(url)

    # ── Per-user queue ─────────────────────────────────────────────────────────
    if user.id not in _user_locks:
        _user_locks[user.id] = asyncio.Lock()
    user_lock = _user_locks[user.id]

    if user_lock.locked():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ *You already have a download in progress.*\nPlease wait for it to finish.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _queued_count += 1
    position = _queued_count
    queue_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ *Added to queue*\n📥 Position: {position}  •  {platform}",
        parse_mode=ParseMode.MARKDOWN,
    )

    temp_dir = tempfile.mkdtemp()
    status_msg = None

    async with user_lock:
        async with _global_dl_semaphore:
            _queued_count = max(0, _queued_count - 1)
            try:
                await queue_msg.delete()
            except Exception:
                pass

            try:
                # Stage 1 ─ Downloading
                status_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ *Downloading...*\n{platform}",
                    parse_mode=ParseMode.MARKDOWN,
                )

                file_path, error, title, thumbnail_url = await download_manager.download(
                    url, temp_dir, quality
                )

                if error:
                    msgs = {
                        "file_too_large":  "❌ *File too large!*\nMaximum source size ~200 MB.",
                        "private":         "❌ *Private content.*\nThis video is not publicly accessible.",
                        "unsupported":     "❌ *Unsupported URL.*\nThis platform or link is not supported.",
                        "copyright":       "❌ *Copyright protected.*\nThis content cannot be downloaded.",
                        "age_restricted":  "❌ *Age-restricted content.*\nSign-in is required to access this.",
                        "unavailable":     "❌ *Video unavailable.*\nIt may have been deleted or made private.",
                        "region_blocked":  "❌ *Region blocked.*\nThis content is not available in this region.",
                        "login_required":  "❌ *Login required.*\nThis content requires a signed-in account.",
                        "timeout":         "❌ *Download timed out.*\nTry a shorter video or lower quality.",
                        "unknown":         "❌ *Download failed.*\nCheck the URL and try again.",
                    }
                    await status_msg.edit_text(
                        msgs.get(error, "❌ *Download failed.*"),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                if not file_path:
                    await status_msg.edit_text(
                        "❌ *No file found after download.*", parse_mode=ParseMode.MARKDOWN
                    )
                    return

                # Stage 2 ─ Processing
                await status_msg.edit_text(f"📦 *Processing...*\n{platform}", parse_mode=ParseMode.MARKDOWN)

                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                clean_title  = title or "video"

                if file_size_mb > MAX_FILE_SIZE_MB:
                    await status_msg.edit_text(
                        f"⚠️ *File too large for Telegram*\n"
                        f"Size: {file_size_mb:.1f} MB  (limit: {MAX_FILE_SIZE_MB} MB)\n"
                        f"Try a lower quality.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                # Fetch thumbnail
                thumb_path: Optional[str] = None
                if thumbnail_url and quality != "mp3":
                    try:
                        resp = requests.get(thumbnail_url, timeout=10)
                        if resp.status_code == 200:
                            thumb_path = os.path.join(temp_dir, "thumb.jpg")
                            with open(thumb_path, "wb") as tf:
                                tf.write(resp.content)
                    except Exception:
                        thumb_path = None

                # Stage 3 ─ Uploading
                await status_msg.edit_text(f"⬆️ *Uploading...*\n{platform}", parse_mode=ParseMode.MARKDOWN)

                user_manager.increment_downloads(user.id)
                record_download_history(user.id, url, platform)

                quality_label = (
                    "🎵 MP3" if quality == "mp3"
                    else f"🎬 {quality}p" if quality in ("360", "480", "720")
                    else "🎬 Best"
                )
                caption = (
                    f"📥 *{clean_title[:60]}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📏 {file_size_mb:.1f} MB  •  {quality_label}  •  {platform}\n"
                    f"📊 Download #{bot_data['total_downloads']:,}"
                )

                with open(file_path, "rb") as f:
                    if quality == "mp3":
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            title=clean_title[:64],
                            performer="Universal Downloader",
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            read_timeout=120,
                            write_timeout=120,
                        )
                    else:
                        thumb_file = open(thumb_path, "rb") if thumb_path else None
                        try:
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=f,
                                thumbnail=thumb_file,
                                caption=caption,
                                parse_mode=ParseMode.MARKDOWN,
                                supports_streaming=True,
                                read_timeout=120,
                                write_timeout=120,
                            )
                        finally:
                            if thumb_file:
                                thumb_file.close()

                # Stage 4 ─ Done
                await status_msg.edit_text("✅ *Done!*", parse_mode=ParseMode.MARKDOWN)
                await asyncio.sleep(1.5)
                await status_msg.delete()

                context.user_data["mp3_mode"] = False

                await send_log(
                    context,
                    f"📥 *Download*\n"
                    f"User: @{user.username or 'N/A'} (`{user.id}`)\n"
                    f"Platform: {platform}  •  Quality: {quality_label}  •  Size: {file_size_mb:.1f} MB",
                )

            except Exception as e:
                logger.error(f"do_download error: {e}")
                try:
                    if status_msg:
                        await status_msg.edit_text(
                            "❌ *An error occurred.*\nPlease try again.",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                except Exception:
                    pass
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)


# ── Playlist downloader ───────────────────────────────────────────────────────────
async def do_playlist_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
):
    """Download the first MAX_PLAYLIST_VIDEOS videos from a YouTube playlist."""
    global _queued_count
    user = update.effective_user
    chat_id = update.effective_chat.id
    platform = "🎬 YouTube"

    if user.id not in _user_locks:
        _user_locks[user.id] = asyncio.Lock()
    user_lock = _user_locks[user.id]

    if user_lock.locked():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ *You already have a download in progress.*\nPlease wait for it to finish.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _queued_count += 1
    position = _queued_count
    queue_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📋 *Playlist queued*\n"
            f"📥 Position: {position}  •  {platform}\n"
            f"⚡ Will download first {MAX_PLAYLIST_VIDEOS} videos…"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    temp_dir = tempfile.mkdtemp()

    async with user_lock:
        async with _global_dl_semaphore:
            _queued_count = max(0, _queued_count - 1)
            try:
                await queue_msg.delete()
            except Exception:
                pass

            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ *Fetching playlist info…*\n{platform}",
                parse_mode=ParseMode.MARKDOWN,
            )

            try:
                def _blocking_playlist():
                    opts = {
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": False,
                        "playliststart": 1,
                        "playlistend": MAX_PLAYLIST_VIDEOS,
                        "format": QUALITY_FORMATS["best"],
                        "merge_output_format": "mp4",
                        "outtmpl": f"{temp_dir}/%(playlist_index)02d - %(title)s.%(ext)s",
                    }
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        playlist_title = info.get("title", "Playlist")
                        return playlist_title

                await status_msg.edit_text(
                    f"⏳ *Downloading playlist…*\n{platform}\n_(first {MAX_PLAYLIST_VIDEOS} videos)_",
                    parse_mode=ParseMode.MARKDOWN,
                )

                try:
                    playlist_title = await asyncio.wait_for(
                        asyncio.to_thread(_blocking_playlist),
                        timeout=600,
                    )
                except asyncio.TimeoutError:
                    await status_msg.edit_text(
                        "❌ *Playlist download timed out.*", parse_mode=ParseMode.MARKDOWN
                    )
                    return
                except Exception as e:
                    logger.error(f"Playlist download error: {e}")
                    await status_msg.edit_text(
                        "❌ *Failed to download playlist.*\nCheck the URL and try again.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                media_files = sorted([
                    f for f in Path(temp_dir).glob("*")
                    if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")
                ])

                if not media_files:
                    await status_msg.edit_text(
                        "❌ *No videos found in this playlist.*", parse_mode=ParseMode.MARKDOWN
                    )
                    return

                count = len(media_files)
                await status_msg.edit_text(
                    f"⬆️ *Uploading {count} video(s)…*\n{platform}", parse_mode=ParseMode.MARKDOWN
                )

                sent = 0
                for i, vid_path in enumerate(media_files, 1):
                    file_size_mb = vid_path.stat().st_size / (1024 * 1024)
                    if file_size_mb > MAX_FILE_SIZE_MB:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⚠️ *Video {i} too large* ({file_size_mb:.1f} MB) — skipped.",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        continue
                    try:
                        with open(vid_path, "rb") as vf:
                            cap = (
                                f"📋 *{clean_filename(playlist_title)[:50]}*\n"
                                f"Video {i}/{count}  •  {file_size_mb:.1f} MB  •  {platform}"
                            )
                            await context.bot.send_video(
                                chat_id=chat_id,
                                video=vf,
                                caption=cap,
                                parse_mode=ParseMode.MARKDOWN,
                                supports_streaming=True,
                                read_timeout=120,
                                write_timeout=120,
                            )
                        sent += 1
                        user_manager.increment_downloads(user.id)
                    except Exception as e:
                        logger.error(f"Playlist upload error video {i}: {e}")

                record_download_history(user.id, url, platform)

                await status_msg.edit_text(
                    f"✅ *Playlist done!*  {sent}/{count} videos sent.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await asyncio.sleep(2)
                await status_msg.delete()

                await send_log(
                    context,
                    f"📋 *Playlist*\n"
                    f"User: @{user.username or 'N/A'} (`{user.id}`)\n"
                    f"Sent: {sent}/{count} videos",
                )

            except Exception as e:
                logger.error(f"do_playlist_download error: {e}")
                try:
                    await status_msg.edit_text(
                        "❌ *An error occurred.*\nPlease try again.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)


# ── /start ────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Ban check
    if user_manager.is_banned(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 *You are banned from using this bot.*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_manager.register_user(user.id, user.username, user.full_name)
    waiting_for_input.pop(user.id, None)

    # Force subscription check
    if force_channels and not await check_subscription(user.id, context):
        await update.message.reply_text(
            "⚠️ *Access Denied!*\n\nJoin the required channels to use this bot:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await get_subscription_keyboard(),
        )
        return

    if not force_channels:
        user_manager.set_verified(user.id, False)

    keyboard = [
        [
            InlineKeyboardButton("📥 Download Video", callback_data="dl_video"),
            InlineKeyboardButton("🎵 Convert MP3",    callback_data="mp3_mode"),
        ],
        [
            InlineKeyboardButton("🎬 YouTube",   callback_data="platform_youtube"),
            InlineKeyboardButton("📸 Instagram", callback_data="platform_instagram"),
        ],
        [
            InlineKeyboardButton("🎵 TikTok",    callback_data="platform_tiktok"),
            InlineKeyboardButton("🐦 Twitter/X", callback_data="platform_twitter"),
        ],
        [
            InlineKeyboardButton("📘 Facebook",  callback_data="platform_facebook"),
            InlineKeyboardButton("📊 My Stats",  callback_data="user_stats"),
        ],
        [
            InlineKeyboardButton("ℹ️ About",      callback_data="about"),
            InlineKeyboardButton("❓ How to Use", callback_data="how_to_use"),
        ],
    ]
    if user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    first_name = user.first_name or "there"
    caption = (
        f"👋 *Hey {first_name}, welcome!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *Universal Media Downloader v3.0*\n\n"
        "📥 *Supported Platforms:*\n"
        "  🎬 YouTube  •  📸 Instagram\n"
        "  🎵 TikTok   •  🐦 Twitter/X\n"
        "  📘 Facebook\n\n"
        "✨ *Features:*\n"
        "  ⚡ Fast downloads  •  🎵 MP3 extraction\n"
        "  💯 Free to use     •  🔒 No login needed\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 *Send a link or choose a platform below!*"
    )

    try:
        if BANNER_FILE_ID:
            await update.message.reply_photo(
                photo=BANNER_FILE_ID, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup,
            )
        elif BANNER_URL:
            await update.message.reply_photo(
                photo=BANNER_URL, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text(
                caption, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Start message error: {e}")
        await update.message.reply_text(
            caption, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )

    # Log new user event
    await send_log(
        context,
        f"👤 *New User*\n"
        f"Name: {user.full_name}\n"
        f"Username: @{user.username or 'N/A'}\n"
        f"ID: `{user.id}`",
    )


# ── button_handler ────────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    # Ban check
    if user_manager.is_banned(user.id) and user.id != ADMIN_ID:
        await query.message.reply_text(
            "🚫 *You are banned from using this bot.*", parse_mode=ParseMode.MARKDOWN
        )
        return

    user_manager.register_user(user.id, user.username, user.full_name)

    # Admin callbacks
    if query.data.startswith("admin_"):
        if user.id != ADMIN_ID:
            await query.message.reply_text(
                "⛔ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN
            )
            return
        await admin_callback(update, context)
        return

    # Force subscription check (non-admin only)
    if user.id != ADMIN_ID and force_channels and not await check_subscription(user.id, context):
        await query.message.reply_text(
            "⚠️ *Access Denied!*\n\nJoin the required channels first!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await get_subscription_keyboard(),
        )
        return

    # ── Quality selection ──────────────────────────────────────────────────────
    if query.data.startswith("quality_"):
        quality = query.data.replace("quality_", "")   # 360 | 480 | 720 | mp3
        url = context.user_data.get("pending_url")
        if not url:
            await query.message.edit_text(
                "❌ *No URL found.*\nPlease send the link again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        context.user_data.pop("pending_url", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await do_download(update, context, url, quality)
        return

    if query.data == "cancel_download":
        context.user_data.pop("pending_url", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # ── Subscription verification ──────────────────────────────────────────────
    if query.data in ("verify_subscription", "refresh_subscription"):
        if await check_subscription(user.id, context):
            try:
                await query.message.delete()
            except Exception:
                pass
            await start(update, context)
        else:
            await query.message.reply_text(
                "❌ *Not joined yet!*\n\nJoin all channels first, then tap Verify.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=await get_subscription_keyboard(),
            )
        return

    # ── User stats ─────────────────────────────────────────────────────────────
    if query.data == "user_stats":
        uid = str(user.id)
        if uid in bot_data["users"]:
            info = bot_data["users"][uid]
            join_date = datetime.fromisoformat(info["join_date"]).strftime("%Y-%m-%d")
            txt = (
                f"📊 *Your Statistics*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 Joined: {join_date}\n"
                f"📥 Downloads: {info['total_downloads']:,}\n"
                f"🆔 User ID: `{user.id}`\n"
                f"👤 Username: @{user.username or 'None'}\n"
                f"✅ Verified: {'Yes' if info.get('verified') else 'No'}\n"
                f"🚫 Banned: {'Yes' if user_manager.is_banned(user.id) else 'No'}"
            )
        else:
            txt = "📊 No stats yet. Send a link to start downloading!"
        kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ── How to use ─────────────────────────────────────────────────────────────
    if query.data == "how_to_use":
        txt = (
            "❓ *How to Use*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "*Option 1 — Quick link:*\n"
            "Paste any video URL and choose your quality.\n\n"
            "*Option 2 — Pick a platform:*\n"
            "Tap a platform button, then send the link.\n\n"
            "*Option 3 — Extract MP3:*\n"
            "Tap 🎵 *Convert MP3*, then send any video link.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📌 *Supported links:*\n"
            "• `youtube.com` / `youtu.be`\n"
            "• `instagram.com`\n"
            "• `tiktok.com`\n"
            "• `twitter.com` / `x.com`\n"
            "• `facebook.com`\n\n"
            "⚠️ *Limit:* Max 50 MB for Telegram delivery.\n"
            "💡 *Tip:* Choose 360p or 480p for faster downloads."
        )
        kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ── About ──────────────────────────────────────────────────────────────────
    if query.data == "about":
        total_users, total_downloads, active_today, uptime = user_manager.get_stats()
        txt = (
            f"ℹ️ *About Bot*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Name: Universal Media Downloader\n"
            f"👨‍💻 Developer: HARSHU\n"
            f"📊 Version: 3.0 Ultimate\n\n"
            f"📈 *Live Stats:*\n"
            f"  👥 Total Users: {total_users:,}\n"
            f"  📥 Downloads: {total_downloads:,}\n"
            f"  🟢 Active Today: {active_today}\n"
            f"  ⏱️ Uptime: {uptime}\n\n"
            f"⚡ Powered by: yt-dlp & python-telegram-bot"
        )
        kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ── MP3 mode ───────────────────────────────────────────────────────────────
    if query.data == "mp3_mode":
        context.user_data["mp3_mode"] = True
        await query.message.reply_text(
            "🎵 *MP3 Mode Active!*\n\n"
            "Send me any video link and I'll extract the audio as MP3.\n"
            "Supported: YouTube, Instagram, TikTok, Twitter, Facebook",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Download Video prompt ──────────────────────────────────────────────────
    if query.data == "dl_video":
        context.user_data["mp3_mode"] = False
        await query.message.reply_text(
            "📥 *Download Video*\n\n"
            "Send me a video link and I'll let you choose quality (360p / 480p / 720p).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Back to main ───────────────────────────────────────────────────────────
    if query.data == "back_to_main":
        try:
            await query.message.delete()
        except Exception:
            pass
        await start(update, context)
        return

    # ── Platform selection ─────────────────────────────────────────────────────
    if query.data.startswith("platform_"):
        platform = query.data.replace("platform_", "").upper()
        context.user_data["platform"] = platform
        context.user_data["mp3_mode"] = False
        await query.message.reply_text(
            f"📥 *{platform} Downloader*\n\nSend me the video link:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return


# ── handle_message ────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id

    # Admin input mode (broadcast, add_channel, set_banner)
    if user_id in waiting_for_input:
        await handle_admin_input(update, context)
        return

    # Ban check
    if user_manager.is_banned(user_id) and user_id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 *You are banned from using this bot.*", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Maintenance mode
    if maintenance_mode and user_id != ADMIN_ID:
        await update.message.reply_text(
            "🔧 *Bot is under maintenance.*\nPlease try again later.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Force subscription
    if force_channels and not await check_subscription(user_id, context):
        await update.message.reply_text(
            "⚠️ *Access Denied!*\n\nJoin the required channels to use this bot.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await get_subscription_keyboard(),
        )
        return

    # URL detection
    text = update.message.text or ""
    url_hints = [".com", ".org", ".net", "http", "www",
                 "youtu", "instagram", "tiktok", "twitter", "facebook"]
    if not any(h in text.lower() for h in url_hints):
        await update.message.reply_text(
            "❌ *Please send a valid video URL!*", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Cooldown (10 seconds)
    now       = time.time()
    remaining = COOLDOWN_SECONDS - (now - user_cooldowns.get(user_id, 0))
    if remaining > 0:
        await update.message.reply_text(
            f"⏳ *Please wait {remaining:.0f}s before your next download.*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_cooldowns[user_id] = now
    url = text.strip()

    # MP3 mode: skip quality picker, download directly
    if context.user_data.get("mp3_mode"):
        await do_download(update, context, url, "mp3")
        return

    # Playlist URL: auto-download all videos, skip quality picker
    if is_playlist_url(url):
        await do_playlist_download(update, context, url)
        return

    # Store URL and show quality selection keyboard
    context.user_data["pending_url"] = url
    await update.message.reply_text(
        "🎬 *Choose Download Quality*\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=quality_keyboard(),
    )

    await send_log(
        context,
        f"🔗 *New Request*\n"
        f"User: @{user.username or 'N/A'} (`{user_id}`)\n"
        f"URL: `{url[:80]}`",
    )


# ── Admin panel ───────────────────────────────────────────────────────────────────
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        target = update.message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text(
                "⛔ *Unauthorized Access!*\nAdmin only.", parse_mode=ParseMode.MARKDOWN
            )
        return

    global maintenance_mode

    total_users, total_downloads, active_today, uptime = user_manager.get_stats()
    verified_users = sum(1 for u in bot_data["users"].values() if u.get("verified"))
    banned_count   = len(bot_data.get("banned_users", []))

    keyboard = [
        [
            InlineKeyboardButton("📢 Broadcast",    callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Stats",        callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("🔧 Maintenance",  callback_data="admin_maintenance"),
            InlineKeyboardButton("➕ Add Channel",   callback_data="admin_add_channel"),
        ],
        [
            InlineKeyboardButton("➖ Remove Channel", callback_data="admin_remove_channel"),
            InlineKeyboardButton("📋 Channels",      callback_data="admin_channels_list"),
        ],
        [
            InlineKeyboardButton("👥 Users List",    callback_data="admin_users_list"),
            InlineKeyboardButton("🖼️ Set Banner",    callback_data="admin_set_banner"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Verif.",  callback_data="admin_reset_verifications"),
            InlineKeyboardButton("📊 Export Users",  callback_data="admin_export_users"),
        ],
        [
            InlineKeyboardButton(f"🚫 Banned ({banned_count})", callback_data="admin_banned_list"),
            InlineKeyboardButton("📋 Dl History",    callback_data="admin_download_history"),
        ],
        [
            InlineKeyboardButton("🔙 Main Menu",     callback_data="back_to_main"),
        ],
    ]

    channels_text = (
        "\n".join(
            f"{i}. @{ch['identifier']}" if ch.get("type") == "public"
            else f"{i}. Private ({ch['invite_hash'][:8]}...)"
            for i, ch in enumerate(force_channels, 1)
        )
        or "None configured"
    )

    text = (
        f"👑 *Admin Panel*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {total_users:,}   ✅ Verified: {verified_users:,}\n"
        f"📥 Downloads: {total_downloads:,}   🟢 Today: {active_today}\n"
        f"⏱️ Uptime: {uptime}\n"
        f"🔧 Maintenance: {'🔴 ON' if maintenance_mode else '🟢 OFF'}\n"
        f"🚫 Banned: {banned_count}\n"
        f"📢 Force Channels: {len(force_channels)}\n"
        f"🖼️ Banner: {'✅ Set' if BANNER_FILE_ID or BANNER_URL else '❌ None'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 *Force Channels:*\n{channels_text}"
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ── Admin callbacks ───────────────────────────────────────────────────────────────
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    global maintenance_mode, force_channels, BANNER_FILE_ID, BANNER_URL, bot_data

    back_btn = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]

    # ── Stats ──────────────────────────────────────────────────────────────────
    if query.data == "admin_stats":
        total_users, total_dl, active_today, uptime = user_manager.get_stats()
        verified = sum(1 for u in bot_data["users"].values() if u.get("verified"))
        banned   = len(bot_data.get("banned_users", []))
        top      = sorted(
            bot_data["users"].items(),
            key=lambda x: x[1].get("total_downloads", 0),
            reverse=True,
        )[:5]
        top_text = "\n".join(
            f"  • @{u.get('username','?')}: {u.get('total_downloads',0)} dl"
            for _, u in top
        ) or "  No data yet"
        txt = (
            f"📊 *Detailed Statistics*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users: {total_users:,}\n"
            f"✅ Verified: {verified:,}\n"
            f"🚫 Banned: {banned}\n"
            f"📥 Downloads: {total_dl:,}\n"
            f"🟢 Active Today: {active_today}\n"
            f"⏱️ Uptime: {uptime}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Top Downloaders:*\n{top_text}"
        )
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    # ── Maintenance toggle ─────────────────────────────────────────────────────
    elif query.data == "admin_maintenance":
        maintenance_mode = not maintenance_mode
        bot_data["maintenance_mode"] = maintenance_mode
        save_data(bot_data)
        await query.answer(f"Maintenance {'enabled' if maintenance_mode else 'disabled'}!")
        await admin_panel(update, context)

    # ── Add channel ────────────────────────────────────────────────────────────
    elif query.data == "admin_add_channel":
        waiting_for_input[update.effective_user.id] = "add_channel"
        await query.message.edit_text(
            "📢 *Add Force Channel*\n\n"
            "Send the channel link or username:\n"
            "• Public: `@username` or `https://t.me/username`\n"
            "• Private: `https://t.me/+invitehash`\n\n"
            "⚠️ I must be admin in the channel!\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]]
            ),
        )

    # ── Remove channel ─────────────────────────────────────────────────────────
    elif query.data == "admin_remove_channel":
        if not force_channels:
            await query.answer("No channels to remove!")
            return
        keyboard = [
            [InlineKeyboardButton(
                f"❌ @{ch['identifier']}" if ch.get("type") == "public"
                else f"❌ Private ({ch['invite_hash'][:8]}...)",
                callback_data=f"remove_channel_{i}",
            )]
            for i, ch in enumerate(force_channels)
        ]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        await query.message.edit_text(
            "📢 *Remove Force Channel*\n\nSelect channel to remove:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data.startswith("remove_channel_"):
        index = int(query.data.split("_")[-1])
        if 0 <= index < len(force_channels):
            removed = force_channels.pop(index)
            bot_data["force_channels"] = force_channels
            for uid in bot_data["users"]:
                bot_data["users"][uid]["verified"] = False
            save_data(bot_data)
            display = (
                f"@{removed['identifier']}" if removed.get("type") == "public"
                else "Private Channel"
            )
            await query.answer(f"Removed {display}!")
            await admin_panel(update, context)

    # ── Channels list ──────────────────────────────────────────────────────────
    elif query.data == "admin_channels_list":
        if not force_channels:
            await query.answer("No channels configured!")
            return
        txt = "📢 *Force Channels List*\n\n"
        for i, ch in enumerate(force_channels, 1):
            if ch.get("type") == "public":
                txt += f"{i}. Public: @{ch['identifier']}\n"
            else:
                txt += f"{i}. Private: `{ch['invite_hash']}`\n"
            txt += f"   Added: {ch.get('added_date', 'Unknown')}\n\n"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    # ── Users list ─────────────────────────────────────────────────────────────
    elif query.data == "admin_users_list":
        total    = len(bot_data["users"])
        verified = sum(1 for u in bot_data["users"].values() if u.get("verified"))
        sorted_u = sorted(
            bot_data["users"].items(),
            key=lambda x: datetime.fromisoformat(x[1]["last_active"]),
            reverse=True,
        )[:10]
        txt = f"👥 *Users (Total: {total}, Verified: {verified})*\n\n*Last 10 Active:*\n"
        banned_list = bot_data.get("banned_users", [])
        for uid, info in sorted_u:
            uname = info.get("username", "N/A")
            dl    = info.get("total_downloads", 0)
            last  = datetime.fromisoformat(info["last_active"]).strftime("%m-%d %H:%M")
            flag  = " 🚫" if uid in banned_list else ""
            txt  += f"• @{uname} — {dl} dl — {last}{flag}\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n(Truncated)"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    # ── Set banner ─────────────────────────────────────────────────────────────
    elif query.data == "admin_set_banner":
        waiting_for_input[update.effective_user.id] = "set_banner"
        await query.message.edit_text(
            "🖼️ *Set Banner Image*\n\nSend a photo directly or an image URL.\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]]
            ),
        )

    # ── Reset verifications ────────────────────────────────────────────────────
    elif query.data == "admin_reset_verifications":
        for uid in bot_data["users"]:
            bot_data["users"][uid]["verified"] = False
        save_data(bot_data)
        await query.answer("All verifications reset!")
        await admin_panel(update, context)

    # ── Export users CSV ───────────────────────────────────────────────────────
    elif query.data == "admin_export_users":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "User ID", "Username", "Full Name",
            "Join Date", "Last Active", "Downloads", "Verified", "Banned",
        ])
        banned_list = bot_data.get("banned_users", [])
        for uid, info in bot_data["users"].items():
            writer.writerow([
                uid,
                info.get("username", ""),
                info.get("full_name", ""),
                info.get("join_date", ""),
                info.get("last_active", ""),
                info.get("total_downloads", 0),
                info.get("verified", False),
                uid in banned_list,
            ])
        await query.message.reply_document(
            document=io.BytesIO(output.getvalue().encode()),
            filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            caption="📊 Users Export",
        )
        await query.answer("Export sent!")

    # ── Broadcast ──────────────────────────────────────────────────────────────
    elif query.data == "admin_broadcast":
        waiting_for_input[update.effective_user.id] = "broadcast"
        await query.message.edit_text(
            "📢 *Broadcast Message*\n\n"
            "Send any message to broadcast to all users.\n"
            "Supports: text, photo, video, audio, document.\n\n"
            f"👥 Recipients: {len(bot_data['users']):,}\n\nType /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]]
            ),
        )

    # ── Banned users list ──────────────────────────────────────────────────────
    elif query.data == "admin_banned_list":
        banned_list = bot_data.get("banned_users", [])
        if not banned_list:
            await query.answer("No banned users!")
            return
        txt = f"🚫 *Banned Users ({len(banned_list)})*\n\n"
        for uid in banned_list[:20]:
            uinfo = bot_data["users"].get(uid, {})
            uname = uinfo.get("username", "N/A")
            txt  += f"• `{uid}` — @{uname}\n"
        if len(banned_list) > 20:
            txt += f"\n...and {len(banned_list) - 20} more"
        txt += f"\n\n💡 Use `/unban <user_id>` to unban."
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    # ── Download history ───────────────────────────────────────────────────────
    elif query.data == "admin_download_history":
        history = bot_data.get("download_history", [])
        total = len(history)
        if not history:
            await query.answer("No download history yet!")
            return
        recent = list(reversed(history[-20:]))  # newest first
        platforms: Dict[str, int] = {}
        for entry in history:
            p = entry.get("platform", "🌐 Web")
            platforms[p] = platforms.get(p, 0) + 1
        breakdown = "  ".join(
            f"{p}: {n}" for p, n in sorted(platforms.items(), key=lambda x: -x[1])
        )
        txt = (
            f"📋 *Download History (last 20 of {total})*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{breakdown}\n\n"
        )
        for entry in recent:
            ts   = datetime.fromisoformat(entry["timestamp"]).strftime("%m-%d %H:%M")
            uid  = entry.get("user_id", "?")
            uname = bot_data["users"].get(uid, {}).get("username", uid)
            plat = entry.get("platform", "?")
            txt += f"• @{uname} — {plat} — {ts}\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n_(Truncated)_"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    # ── Back to admin panel ────────────────────────────────────────────────────
    elif query.data == "admin_panel":
        await admin_panel(update, context)


# ── handle_admin_input ────────────────────────────────────────────────────────────
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    action  = waiting_for_input.get(user_id)
    if not action:
        return

    global BANNER_FILE_ID, BANNER_URL, bot_data, force_channels

    # ── Add channel ────────────────────────────────────────────────────────────
    if action == "add_channel":
        text     = update.message.text or update.message.caption or ""
        ch_type, identifier = await extract_channel_info(text)

        if not ch_type:
            await update.message.reply_text(
                "❌ *Invalid format!*\nSend `@channel` or `https://t.me/+invite`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Duplicate check
        for ch in force_channels:
            if ch.get("type") == "public" and ch.get("identifier") == identifier:
                await update.message.reply_text("❌ *Channel already exists!*", parse_mode=ParseMode.MARKDOWN)
                del waiting_for_input[user_id]
                return
            if ch.get("type") == "private" and ch.get("invite_hash") == identifier:
                await update.message.reply_text("❌ *Channel already exists!*", parse_mode=ParseMode.MARKDOWN)
                del waiting_for_input[user_id]
                return

        try:
            if ch_type == "public":
                chat       = await context.bot.get_chat(f"@{identifier}")
                bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
                if bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                    force_channels.append({
                        "type": "public", "identifier": identifier,
                        "added_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                    bot_data["force_channels"] = force_channels
                    save_data(bot_data)
                    await update.message.reply_text(
                        f"✅ *@{identifier} added as force channel!*", parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text(
                        f"❌ *I'm not admin in @{identifier}!*\nMake me admin first.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
            else:
                force_channels.append({
                    "type": "private", "invite_hash": identifier,
                    "added_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                bot_data["force_channels"] = force_channels
                save_data(bot_data)
                await update.message.reply_text(
                    "✅ *Private channel added!*", parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Add channel error: {e}")
            await update.message.reply_text(
                f"❌ *Error:* `{str(e)[:100]}`", parse_mode=ParseMode.MARKDOWN
            )

        del waiting_for_input[user_id]

    # ── Set banner ─────────────────────────────────────────────────────────────
    elif action == "set_banner":
        if update.message.photo:
            BANNER_FILE_ID = update.message.photo[-1].file_id
            bot_data["banner_file_id"] = BANNER_FILE_ID
            bot_data["banner_url"]     = None
            save_data(bot_data)
            await update.message.reply_text("✅ *Banner updated!*", parse_mode=ParseMode.MARKDOWN)
        elif update.message.text and update.message.text.startswith(("http://", "https://")):
            BANNER_URL = update.message.text
            bot_data["banner_url"]     = BANNER_URL
            bot_data["banner_file_id"] = None
            save_data(bot_data)
            await update.message.reply_text("✅ *Banner URL set!*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(
                "❌ *Send a photo or a valid image URL.*", parse_mode=ParseMode.MARKDOWN
            )
            return
        del waiting_for_input[user_id]

    # ── Broadcast ──────────────────────────────────────────────────────────────
    elif action == "broadcast":
        processing = await update.message.reply_text(
            "📤 *Broadcasting...*\nProgress: 0", parse_mode=ParseMode.MARKDOWN
        )
        message    = update.message
        success    = 0
        failed     = 0
        users_list = [int(uid) for uid in bot_data["users"].keys()]
        total      = len(users_list)

        for i, uid in enumerate(users_list, 1):
            try:
                if message.photo or message.video or message.document \
                        or message.audio or message.voice:
                    await message.copy(
                        chat_id=uid,
                        caption=message.caption_html if message.caption else None,
                        parse_mode=ParseMode.HTML,
                    )
                elif message.text:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=message.text_html or message.text,
                        parse_mode=ParseMode.HTML if message.entities else None,
                    )
                success += 1
            except Exception as e:
                logger.error(f"Broadcast failed for {uid}: {e}")
                failed += 1

            if i % 10 == 0 or i == total:
                try:
                    await processing.edit_text(
                        f"📤 *Broadcasting...*\nProgress: {i}/{total}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.05)

        await processing.delete()
        await update.message.reply_text(
            f"📊 *Broadcast Complete*\n\n"
            f"✅ Success: {success:,}\n"
            f"❌ Failed:  {failed:,}\n"
            f"📢 Total:   {total:,}",
            parse_mode=ParseMode.MARKDOWN,
        )
        del waiting_for_input[user_id]


# ── Command handlers ──────────────────────────────────────────────────────────────
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — admin only"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    total_users, total_dl, active_today, uptime = user_manager.get_stats()
    verified = sum(1 for u in bot_data["users"].values() if u.get("verified"))
    banned   = len(bot_data.get("banned_users", []))
    top      = sorted(
        bot_data["users"].items(),
        key=lambda x: x[1].get("total_downloads", 0),
        reverse=True,
    )[:5]
    top_text = "\n".join(
        f"  • @{u.get('username','?')}: {u.get('total_downloads',0)} dl"
        for _, u in top
    ) or "  No data yet"
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {total_users:,}   ✅ Verified: {verified:,}\n"
        f"🚫 Banned: {banned}\n"
        f"📥 Downloads: {total_dl:,}\n"
        f"🟢 Active Today: {active_today}\n"
        f"⏱️ Uptime: {uptime}\n"
        f"🔧 Maintenance: {'🔴 ON' if maintenance_mode else '🟢 OFF'}\n"
        f"📢 Force Channels: {len(force_channels)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *Top Downloaders:*\n{top_text}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast — admin only"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    waiting_for_input[update.effective_user.id] = "broadcast"
    await update.message.reply_text(
        "📢 *Broadcast Message*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Send the message to broadcast to all users.\n\n"
        f"👥 Recipients: {len(bot_data['users']):,}\n\nType /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban <user_id> — admin only"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID — must be a number.", parse_mode=ParseMode.MARKDOWN)
        return
    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Cannot ban the admin!", parse_mode=ParseMode.MARKDOWN)
        return
    if user_manager.ban_user(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", "Unknown")
        await update.message.reply_text(
            f"🚫 *Banned*\nID: `{target_id}`\nUsername: @{uname}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_log(context, f"🚫 *Ban*\nAdmin banned `{target_id}` (@{uname})")
    else:
        await update.message.reply_text(
            f"⚠️ User `{target_id}` is already banned.", parse_mode=ParseMode.MARKDOWN
        )


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban <user_id> — admin only"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID — must be a number.", parse_mode=ParseMode.MARKDOWN)
        return
    if user_manager.unban_user(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", "Unknown")
        await update.message.reply_text(
            f"✅ *Unbanned*\nID: `{target_id}`\nUsername: @{uname}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_log(context, f"✅ *Unban*\nAdmin unbanned `{target_id}` (@{uname})")
    else:
        await update.message.reply_text(
            f"⚠️ User `{target_id}` is not banned.", parse_mode=ParseMode.MARKDOWN
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — cancel current admin input operation"""
    user_id = update.effective_user.id
    if user_id in waiting_for_input:
        del waiting_for_input[user_id]
        await update.message.reply_text("✅ *Operation cancelled!*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ *No active operation to cancel.*", parse_mode=ParseMode.MARKDOWN)


# ── Error handler ─────────────────────────────────────────────────────────────────
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb_str = "".join(
        traceback.format_exception(type(context.error), context.error, context.error.__traceback__)
    )
    logger.error(f"Unhandled exception:\n{tb_str}")

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ *An error occurred.*\nPlease try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception:
        pass

    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"🔴 *Unhandled Error*\n```\n{tb_str[:1000]}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


# ── main ──────────────────────────────────────────────────────────────────────────
def main():
    startup_check()

    async def _post_init(app):
        """Initialise asyncio objects inside the event loop."""
        global _global_dl_semaphore
        _global_dl_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        logger.info(f"Download semaphore initialised (limit={MAX_CONCURRENT_DOWNLOADS})")

    async def _post_shutdown(app):
        """Clean up lingering temp dirs on graceful shutdown."""
        count = 0
        for d in glob.glob("/tmp/tmp*"):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                count += 1
        logger.info(f"Shutdown: cleaned {count} temp dirs")
        print(f"\n✅ Bot offline — {count} temp dirs cleaned.\n")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Command handlers
    application.add_handler(CommandHandler("start",     start))
    application.add_handler(CommandHandler("admin",     admin_panel))
    application.add_handler(CommandHandler("stats",     stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("ban",       ban_command))
    application.add_handler(CommandHandler("unban",     unban_command))
    application.add_handler(CommandHandler("cancel",    cancel_command))

    # Inline button callbacks
    application.add_handler(CallbackQueryHandler(button_handler))

    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE,
        handle_message,
    ))

    # Global error handler
    application.add_error_handler(error_handler)

    logger.info("🚀 Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
