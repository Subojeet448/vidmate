#!/usr/bin/env python3
"""
Universal Media Downloader Bot
Developer: MANDAL !!
Version: 4.0 - Ultimate Edition (Fixed)
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
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, List
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
from telegram.request import HTTPXRequest
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
BOT_TOKEN                = "8766366480:AAENn5hPjWWWeW12SqmmXd0Co1Aa8G50Ukg"
MAIN_ADMIN_ID            = 6535364725   # permanent, cannot be removed
CONTACT_USERNAME         = "@MANDAL4482"
LOG_CHANNEL_ID           = None
MAX_FILE_SIZE_MB         = 50
COOLDOWN_SECONDS         = 10
MAX_CONCURRENT_DOWNLOADS = 3
MAX_PLAYLIST_VIDEOS      = 5
DATA_FILE                = "bot_data.json"
DB_FILE                  = "bot_limits.db"

# Default per-user limit (MB). 0 = unlimited
DEFAULT_LIMIT_MB = 0

# ── Global state ──────────────────────────────────────────────────────────────────
user_cooldowns: Dict[int, float] = {}
maintenance_mode = False
waiting_for_input: Dict[int, str] = {}
bot_start_time = datetime.now()
user_temp_data: Dict = {}

_user_locks: Dict[int, asyncio.Lock] = {}
_global_dl_semaphore: Optional[asyncio.Semaphore] = None
_queued_count: int = 0

# ── SQLite for limits & usage ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_limits (
            user_id    INTEGER PRIMARY KEY,
            limit_mb   REAL    DEFAULT 0,
            used_mb    REAL    DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_get_limit(user_id: int) -> Tuple[float, float]:
    """Returns (limit_mb, used_mb). 0 limit = unlimited."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT limit_mb, used_mb FROM user_limits WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return DEFAULT_LIMIT_MB, 0.0

def db_set_limit(user_id: int, limit_mb: float):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_limits (user_id, limit_mb, used_mb, updated_at)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET limit_mb=excluded.limit_mb, updated_at=excluded.updated_at
    """, (user_id, limit_mb, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_add_usage(user_id: int, mb: float):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_limits (user_id, limit_mb, used_mb, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            used_mb = used_mb + excluded.used_mb,
            updated_at = excluded.updated_at
    """, (user_id, DEFAULT_LIMIT_MB, mb, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def db_reset_usage(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE user_limits SET used_mb=0, updated_at=? WHERE user_id=?",
              (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

def db_check_limit(user_id: int, file_mb: float) -> Tuple[bool, float, float]:
    """Returns (allowed, limit_mb, used_mb). allowed=True if within limit."""
    limit_mb, used_mb = db_get_limit(user_id)
    if limit_mb == 0:
        return True, 0, used_mb
    if used_mb + file_mb > limit_mb:
        return False, limit_mb, used_mb
    return True, limit_mb, used_mb

# ── Data persistence ──────────────────────────────────────────────────────────────
def _default_data() -> dict:
    return {
        "users": {},
        "total_downloads": 0,
        "force_channels": [],
        "banned_users": [],
        "admin_ids": [],
        "banner_url": None,
        "banner_file_id": None,
        "maintenance_mode": False,
        "download_history": [],
        "zip_enabled": False,
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

bot_data         = load_data()
force_channels   = bot_data.get("force_channels", [])
BANNER_URL       = bot_data.get("banner_url")
BANNER_FILE_ID   = bot_data.get("banner_file_id")
maintenance_mode = bot_data.get("maintenance_mode", False)
zip_enabled      = bot_data.get("zip_enabled", False)

# ── Admin helpers ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in bot_data.get("admin_ids", [])

def add_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return False
    admins = bot_data.setdefault("admin_ids", [])
    if user_id not in admins:
        admins.append(user_id)
        save_data(bot_data)
        return True
    return False

def remove_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return False
    admins = bot_data.get("admin_ids", [])
    if user_id in admins:
        admins.remove(user_id)
        save_data(bot_data)
        return True
    return False

# ── Helper utilities ──────────────────────────────────────────────────────────────
def clean_filename(name: str) -> str:
    name = re.sub(r"@\w+", "", name)
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:64] if name else "video"

def detect_platform(url: str) -> str:
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
    u = url.lower()
    return ("youtube.com" in u or "youtu.be" in u) and ("list=" in u or "playlist" in u)

def record_download_history(user_id: int, url: str, platform: str):
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
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"send_log failed: {e}")

def format_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    return f"{mb:.1f} MB"

def _get_direct_url(url: str, height: int) -> Optional[str]:
    """Get direct download URL using yt-dlp without downloading."""
    extract_audio = (height == 0)
    if extract_audio:
        fmt = "bestaudio/best"
    else:
        fmt = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]"
            f"/best"
        )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": fmt,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # For merged formats, get the best video url
            if "requested_formats" in info:
                return info["requested_formats"][0].get("url")
            return info.get("url")
    except Exception as e:
        logger.error(f"_get_direct_url error: {e}")
        return None

def estimate_wait(duration_seconds: int) -> str:
    if duration_seconds <= 60:
        return "~1 min"
    elif duration_seconds <= 300:
        return "~5 min"
    elif duration_seconds <= 600:
        return "~10 min"
    else:
        return "~15+ min"

def startup_check():
    sep = "=" * 50
    print(f"\n{sep}")
    print("  🚀  Universal Media Downloader Bot v4.0")
    print(f"       Developer: MANDAL !!  ({CONTACT_USERNAME})")
    print(sep)

    if BOT_TOKEN:
        print("✅  BOT_TOKEN         — found")
    else:
        print("❌  BOT_TOKEN         — MISSING")

    print(f"✅  MAIN_ADMIN_ID     — {MAIN_ADMIN_ID}")
    extra = bot_data.get("admin_ids", [])
    print(f"✅  Extra Admins      — {len(extra)}")

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
        print("✅  ffmpeg ready      — available")
    except Exception:
        print("⚠️  ffmpeg            — not found")

    try:
        ver = yt_dlp.version.__version__
        print(f"✅  yt-dlp ready      — v{ver}")
    except Exception:
        print("❌  yt-dlp            — not installed")

    print(sep + "\n")

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
class DownloadManager:

    @staticmethod
    def _fetch_formats(url: str) -> dict:
        """
        FIX #3: Added platform-specific fallback qualities when formats list is empty.
        Blocking: fetch video info + all available formats.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            title       = info.get("title", "video")
            duration    = info.get("duration", 0)
            thumbnail   = info.get("thumbnail")
            raw_formats = info.get("formats", [])

            seen_heights = set()
            quality_list = []

            for f in reversed(raw_formats):
                h = f.get("height")
                if not h:
                    continue
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                if vcodec == "none":
                    continue
                has_audio = acodec != "none"
                ext = f.get("ext", "mp4")
                # FIX #7: filesize is already in bytes here, not MB — correct
                fs = f.get("filesize") or f.get("filesize_approx") or 0

                if h not in seen_heights:
                    seen_heights.add(h)
                    quality_list.append({
                        "height":    h,
                        "ext":       ext,
                        "filesize":  fs,   # bytes
                        "has_audio": has_audio,
                        "label":     f"{h}p",
                    })

            quality_list.sort(key=lambda x: x["height"], reverse=True)

            # FIX #3: If no video formats found, add common fallback heights
            if not quality_list:
                logger.warning(f"No video formats found for {url}, adding fallback qualities")
                for h in [1080, 720, 480, 360]:
                    quality_list.append({
                        "height":    h,
                        "ext":       "mp4",
                        "filesize":  0,
                        "has_audio": True,
                        "label":     f"{h}p",
                    })

            # Always offer MP3
            quality_list.append({
                "height":    0,
                "ext":       "mp3",
                "filesize":  0,
                "has_audio": True,
                "label":     "MP3 🎵",
            })

            return {
                "title":     title,
                "duration":  duration,
                "thumbnail": thumbnail,
                "formats":   quality_list,
                "ok":        True,
            }
        except Exception as e:
            logger.error(f"_fetch_formats error: {e}")
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _blocking_download(
        url: str, download_dir: str, height: int
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        FIX #1: Removed double extract_info call — now single ydl.download() call
        which internally fetches info + downloads in one pass.
        FIX #4: Real file size check AFTER download instead of estimate.
        """
        extract_audio = (height == 0)
        ydl_opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
            "outtmpl": f"{download_dir}/%(title)s.%(ext)s",
            "socket_timeout": 60,
            "retries": 5,
            "fragment_retries": 5,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            "concurrent_fragment_downloads": 4,
        }

        if extract_audio:
            ydl_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            ydl_opts["format"] = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={height}]+bestaudio"
                f"/best[height<={height}][ext=mp4]"
                f"/best[height<={height}]"
                f"/best"
            )
            ydl_opts["merge_output_format"] = "mp4"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }]

        title_holder = [None]
        thumbnail_holder = [None]

        # FIX #1: Single extract_info call with download=True
        # Use a progress hook to capture title/thumbnail without a second API call
        def _info_hook(d):
            if d.get("status") == "downloading":
                info = d.get("info_dict", {})
                if title_holder[0] is None:
                    title_holder[0] = info.get("title")
                if thumbnail_holder[0] is None:
                    thumbnail_holder[0] = info.get("thumbnail")

        ydl_opts["progress_hooks"] = [_info_hook]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Single call: fetches info + downloads
                info = ydl.extract_info(url, download=True)
                # extract_info with download=True returns full info dict
                title         = clean_filename(info.get("title", "video"))
                thumbnail_url = info.get("thumbnail")

                # FIX #4: Check real downloaded file size, not the pre-download estimate
                all_files   = list(Path(download_dir).glob("*"))
                media_files = [
                    f for f in all_files
                    if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")
                ]
                chosen = media_files[0] if media_files else (all_files[0] if all_files else None)

                if chosen:
                    real_size = os.path.getsize(str(chosen))
                    # Hard cap: 2GB (Telegram document limit)
                    if real_size > 2000 * 1024 * 1024:
                        return None, "file_too_large", title, None
                    return str(chosen), None, title, thumbnail_url

                return None, "unknown", title, None

        except Exception as e:
            err = str(e).lower()
            logger.warning(f"yt-dlp error for {url}: {e}")
            if "private" in err:                    return None, "private",        None, None
            if "unsupported" in err:                return None, "unsupported",    None, None
            if "copyright" in err:                  return None, "copyright",      None, None
            if "age" in err or "18+" in err:        return None, "age_restricted", None, None
            if "unavailable" in err or "not available" in err or "video unavailable" in err:
                                                    return None, "unavailable",    None, None
            if "geo" in err or "region" in err or "not available in your country" in err:
                                                    return None, "region_blocked", None, None
            if "login" in err or "sign in" in err or "authentication" in err:
                                                    return None, "login_required", None, None
            if "too many" in err or "rate limit" in err or "429" in err:
                                                    return None, "rate_limited",   None, None
            # Log full error for unknown cases
            logger.error(f"yt-dlp unknown error full: {e}")
            return None, "unknown", None, None

    async def fetch_formats(self, url: str) -> dict:
        """Async wrapper for _fetch_formats."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_formats, url),
                timeout=60,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def download(
        self, url: str, download_dir: str, height: int = -1
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Async wrapper with 5-min timeout and 2 retries. height=0 means mp3."""
        _retriable = {"unknown", "timeout"}
        last_result: Tuple = (None, "unknown", None, None)

        for attempt in range(3):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._blocking_download, url, download_dir, height),
                    timeout=600,  # 10 min for long videos
                )
            except asyncio.TimeoutError:
                result = (None, "timeout", None, None)
            except Exception as e:
                logger.error(f"Download wrapper error: {e}")
                result = (None, "unknown", None, None)

            last_result = result
            if result[1] not in _retriable:
                return result
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

        return last_result


# ── Initialize managers ───────────────────────────────────────────────────────────
user_manager     = UserManager()
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
                chat   = await context.bot.get_chat(f"@{ch['identifier']}")
                member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user_id)
                if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                    all_joined = False
                    break
        except Exception as e:
            logger.error(f"Subscription check error for {ch}: {e}")
    if all_joined and force_channels:
        user_manager.set_verified(user_id, True)
    return all_joined


# ── Dynamic quality keyboard ──────────────────────────────────────────────────────
def build_quality_keyboard(formats: List[dict], url_key: str) -> InlineKeyboardMarkup:
    """
    FIX #7: filesize stored as bytes — divide by 1024*1024 to get MB. Was previously
    dividing already-bytes value by 1024*1024, which is correct. Bug was that in the
    old code `fs` was being passed through as bytes but the comment said MB — now
    clearly documented that fs = bytes throughout.
    """
    buttons = []
    row = []
    for fmt in formats:
        label  = fmt["label"]
        height = fmt["height"]
        fs     = fmt.get("filesize", 0)   # bytes
        # Correct: bytes -> MB
        size_str = f" ({format_size(fs / 1024 / 1024)})" if fs else ""
        btn_txt  = f"{label}{size_str}"
        row.append(InlineKeyboardButton(btn_txt, callback_data=f"dldyn_{height}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_download")])
    return InlineKeyboardMarkup(buttons)


# ── Core download executor ────────────────────────────────────────────────────────
async def do_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    height: int,
):
    global _queued_count
    user    = update.effective_user
    chat_id = update.effective_chat.id
    platform = detect_platform(url)

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
    position  = _queued_count
    queue_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ *Added to queue*\n📥 Position: {position}  •  {platform}",
        parse_mode=ParseMode.MARKDOWN,
    )

    temp_dir = tempfile.mkdtemp()

    # FIX #6: status_msg initialised to None before the semaphore block
    # so the finally/except block can safely reference it
    status_msg = None

    async with user_lock:
        async with _global_dl_semaphore:
            _queued_count = max(0, _queued_count - 1)
            try:
                await queue_msg.delete()
            except Exception:
                pass

            try:
                # FIX #6: status_msg assigned inside try so any send failure
                # is caught; later error handling checks `if status_msg`
                status_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ *Downloading...*\n{platform}",
                    parse_mode=ParseMode.MARKDOWN,
                )

                file_path, error, title, thumbnail_url = await download_manager.download(
                    url, temp_dir, height
                )

                if error:
                    msgs = {
                        "file_too_large":  "❌ *File too large!*\nMaximum source size ~200 MB.",
                        "private":         "❌ *Private content.*",
                        "unsupported":     "❌ *Unsupported URL.*",
                        "copyright":       "❌ *Copyright protected.*",
                        "age_restricted":  "❌ *Age-restricted content.*",
                        "unavailable":     "❌ *Video unavailable.*",
                        "region_blocked":  "❌ *Region blocked.*",
                        "login_required":  "❌ *Login required.*",
                        "timeout":         "❌ *Download timed out.*\nTry a shorter video or lower quality.",
                        "rate_limited":    "❌ *Rate limited by platform.*\nWait a few minutes and try again.",
                        "unknown":         "❌ *Download failed.*\nCheck the URL and try again.",
                    }
                    if status_msg:
                        await status_msg.edit_text(
                            msgs.get(error, "❌ *Download failed.*"),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    return

                if not file_path:
                    if status_msg:
                        await status_msg.edit_text(
                            "❌ *No file found after download.*", parse_mode=ParseMode.MARKDOWN
                        )
                    return

                if status_msg:
                    await status_msg.edit_text(
                        f"📦 *Processing...*\n{platform}", parse_mode=ParseMode.MARKDOWN
                    )

                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                clean_title  = title or "video"

                # Limit check
                allowed, limit_mb, used_mb = db_check_limit(user.id, file_size_mb)
                if not allowed:
                    if status_msg:
                        await status_msg.edit_text(
                            f"🚫 *Download Limit Reached!*\n"
                            f"Your limit: {format_size(limit_mb)}\n"
                            f"Used: {format_size(used_mb)}\n"
                            f"This file: {format_size(file_size_mb)}\n\n"
                            f"Contact admin {CONTACT_USERNAME} to increase your limit.",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    return

                if file_size_mb > MAX_FILE_SIZE_MB:
                    # ── Large file: give direct download link ─────────────────
                    if zip_enabled:
                        if status_msg:
                            await status_msg.edit_text(
                                f"🔗 *File is {file_size_mb:.1f} MB — too large for Telegram*\n"
                                f"⏳ Getting direct download link…",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        direct_url = await asyncio.to_thread(
                            _get_direct_url, url, height
                        )
                        quality_label = "🎵 MP3" if height == 0 else f"🎬 {height}p"
                        if direct_url:
                            link_text = (
                                f"📥 *{clean_title[:60]}*\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"📦 Size: {file_size_mb:.1f} MB  •  {quality_label}  •  {platform}\n\n"
                                f"⚠️ *File is too large for Telegram bot*\n"
                                f"👇 *Direct download link:*\n"
                                f"`{direct_url[:500]}`\n\n"
                                f"_Link expires in few hours — download karo abhi!_"
                            )
                            kb = [[InlineKeyboardButton("⬇️ Download Now", url=direct_url[:2048])]]
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=link_text,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=InlineKeyboardMarkup(kb),
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"⚠️ *File too large ({file_size_mb:.1f} MB)*\n"
                                    f"Direct link bhi nahi mila.\n"
                                    f"Please try a lower quality."
                                ),
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        if status_msg:
                            try:
                                await status_msg.delete()
                            except Exception:
                                pass
                        record_download_history(user.id, url, platform)
                        await send_log(
                            context,
                            f"🔗 *Direct Link Sent*\n"
                            f"User: @{user.username or 'N/A'} (`{user.id}`)\n"
                            f"Platform: {platform}  •  Size: {file_size_mb:.1f} MB",
                        )
                        return
                    else:
                        # Zip OFF — normal error
                        if status_msg:
                            await status_msg.edit_text(
                                f"⚠️ *File too large for Telegram*\n"
                                f"Size: {file_size_mb:.1f} MB  (limit: {MAX_FILE_SIZE_MB} MB)\n"
                                f"Try a lower quality.",
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        return
                    # ── end large file feature ────────────────────────────────

                # Thumbnail
                thumb_path: Optional[str] = None
                if thumbnail_url and height != 0:
                    try:
                        resp = requests.get(thumbnail_url, timeout=10)
                        if resp.status_code == 200:
                            thumb_path = os.path.join(temp_dir, "thumb.jpg")
                            with open(thumb_path, "wb") as tf:
                                tf.write(resp.content)
                    except Exception:
                        thumb_path = None

                if status_msg:
                    await status_msg.edit_text(
                        f"⬆️ *Uploading...*\n{platform}", parse_mode=ParseMode.MARKDOWN
                    )

                user_manager.increment_downloads(user.id)
                db_add_usage(user.id, file_size_mb)
                record_download_history(user.id, url, platform)

                quality_label = (
                    "🎵 MP3" if height == 0
                    else f"🎬 {height}p"
                )
                caption = (
                    f"📥 *{clean_title[:60]}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📏 {file_size_mb:.1f} MB  •  {quality_label}  •  {platform}\n"
                    f"📊 Download #{bot_data['total_downloads']:,}"
                )

                sent_msg = None
                with open(file_path, "rb") as f:
                    if height == 0:
                        sent_msg = await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            title=clean_title[:64],
                            performer="Universal Downloader",
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=30,
                            pool_timeout=300,
                        )
                    else:
                        thumb_file = open(thumb_path, "rb") if thumb_path else None
                        try:
                            sent_msg = await context.bot.send_video(
                                chat_id=chat_id,
                                video=f,
                                thumbnail=thumb_file,
                                caption=caption,
                                parse_mode=ParseMode.MARKDOWN,
                                supports_streaming=True,
                                read_timeout=300,
                                write_timeout=300,
                                connect_timeout=30,
                                pool_timeout=300,
                            )
                        finally:
                            if thumb_file:
                                thumb_file.close()

                if status_msg:
                    await status_msg.edit_text("✅ *Done!*", parse_mode=ParseMode.MARKDOWN)
                await asyncio.sleep(1.5)
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                context.user_data["mp3_mode"] = False

                if sent_msg:
                    async def _delete_later(msg):
                        await asyncio.sleep(300)
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    asyncio.create_task(_delete_later(sent_msg))

                await send_log(
                    context,
                    f"📥 *Download*\n"
                    f"User: @{user.username or 'N/A'} (`{user.id}`)\n"
                    f"Platform: {platform}  •  Quality: {quality_label}  •  Size: {file_size_mb:.1f} MB",
                )

            except Exception as e:
                logger.error(f"do_download error: {e}", exc_info=True)
                err_text = (
                    f"❌ *An error occurred.*\n"
                    f"`{str(e)[:200]}`\n\n"
                    f"Please try again or try lower quality."
                )
                if status_msg:
                    try:
                        await status_msg.edit_text(err_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass
                else:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=err_text,
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
    global _queued_count
    user     = update.effective_user
    chat_id  = update.effective_chat.id
    platform = "🎬 YouTube"

    if user.id not in _user_locks:
        _user_locks[user.id] = asyncio.Lock()
    user_lock = _user_locks[user.id]

    if user_lock.locked():
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ *You already have a download in progress.*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _queued_count += 1
    position  = _queued_count
    queue_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📋 *Playlist queued*\n"
            f"📥 Position: {position}  •  {platform}\n"
            f"⚡ Will download first {MAX_PLAYLIST_VIDEOS} videos…"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    temp_dir   = tempfile.mkdtemp()
    status_msg = None  # FIX #6: initialise before semaphore

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
                        "format": (
                            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                            "/bestvideo+bestaudio/best[ext=mp4]/best"
                        ),
                        "merge_output_format": "mp4",
                        "outtmpl": f"{temp_dir}/%(playlist_index)02d - %(title)s.%(ext)s",
                    }
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        return info.get("title", "Playlist")

                if status_msg:
                    await status_msg.edit_text(
                        f"⏳ *Downloading playlist…*\n{platform}\n_(first {MAX_PLAYLIST_VIDEOS} videos)_",
                        parse_mode=ParseMode.MARKDOWN,
                    )

                try:
                    playlist_title = await asyncio.wait_for(
                        asyncio.to_thread(_blocking_playlist), timeout=600
                    )
                except asyncio.TimeoutError:
                    if status_msg:
                        await status_msg.edit_text(
                            "❌ *Playlist download timed out.*", parse_mode=ParseMode.MARKDOWN
                        )
                    return
                except Exception as e:
                    logger.error(f"Playlist download error: {e}")
                    if status_msg:
                        await status_msg.edit_text(
                            "❌ *Failed to download playlist.*", parse_mode=ParseMode.MARKDOWN
                        )
                    return

                media_files = sorted([
                    f for f in Path(temp_dir).glob("*")
                    if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")
                ])

                if not media_files:
                    if status_msg:
                        await status_msg.edit_text(
                            "❌ *No videos found in this playlist.*", parse_mode=ParseMode.MARKDOWN
                        )
                    return

                count = len(media_files)
                if status_msg:
                    await status_msg.edit_text(
                        f"⬆️ *Uploading {count} video(s)…*\n{platform}", parse_mode=ParseMode.MARKDOWN
                    )

                sent = 0
                for i, vid_path in enumerate(media_files, 1):
                    file_size_mb = vid_path.stat().st_size / (1024 * 1024)
                    allowed, limit_mb, used_mb = db_check_limit(user.id, file_size_mb)
                    if not allowed:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🚫 *Video {i} blocked — limit reached.* ({format_size(used_mb)}/{format_size(limit_mb)})",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        continue
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
                            sent_msg = await context.bot.send_video(
                                chat_id=chat_id,
                                video=vf,
                                caption=cap,
                                parse_mode=ParseMode.MARKDOWN,
                                supports_streaming=True,
                                read_timeout=120,
                                write_timeout=120,
                            )
                        sent += 1
                        db_add_usage(user.id, file_size_mb)
                        user_manager.increment_downloads(user.id)
                        async def _del(m):
                            await asyncio.sleep(300)
                            try: await m.delete()
                            except: pass
                        asyncio.create_task(_del(sent_msg))
                    except Exception as e:
                        logger.error(f"Playlist upload error video {i}: {e}")

                record_download_history(user.id, url, platform)
                if status_msg:
                    await status_msg.edit_text(
                        f"✅ *Playlist done!*  {sent}/{count} videos sent.\n_(Auto-deletes in 5 min)_",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                await asyncio.sleep(2)
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"do_playlist_download error: {e}")
                if status_msg:
                    try:
                        await status_msg.edit_text(
                            "❌ *An error occurred.*", parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        pass
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)


# ── /start ────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    FIX #5: start() can be triggered from both update.message AND callback_query.
    We detect the source and reply accordingly so update.message is never None-dereferenced.
    """
    user = update.effective_user

    if user_manager.is_banned(user.id) and not is_admin(user.id):
        text = "🚫 *You are banned from using this bot.*"
        if update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        elif update.callback_query:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    user_manager.register_user(user.id, user.username, user.full_name)
    waiting_for_input.pop(user.id, None)

    if force_channels and not await check_subscription(user.id, context):
        sub_kb = await get_subscription_keyboard()
        deny_text = "⚠️ *Access Denied!*\n\nJoin the required channels to use this bot:"
        if update.message:
            await update.message.reply_text(deny_text, parse_mode=ParseMode.MARKDOWN, reply_markup=sub_kb)
        elif update.callback_query:
            await update.callback_query.message.reply_text(deny_text, parse_mode=ParseMode.MARKDOWN, reply_markup=sub_kb)
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
        [
            InlineKeyboardButton(
                f"📞 Contact Admin {CONTACT_USERNAME}",
                url=f"https://t.me/{CONTACT_USERNAME.lstrip('@')}"
            ),
        ],
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    first_name   = user.first_name or "there"
    caption = (
        f"👋 *Hey {first_name}, welcome!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *Universal Media Downloader v4.0*\n\n"
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

    # FIX #5: Use effective_message to handle both message and callback_query sources
    target = update.message or (update.callback_query and update.callback_query.message)

    try:
        if BANNER_FILE_ID:
            await target.reply_photo(
                photo=BANNER_FILE_ID, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup,
            )
        elif BANNER_URL:
            await target.reply_photo(
                photo=BANNER_URL, caption=caption,
                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup,
            )
        else:
            await target.reply_text(
                caption, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Start message error: {e}")
        try:
            await target.reply_text(
                caption, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
            )
        except Exception:
            pass

    await send_log(
        context,
        f"👤 *New User*\nName: {user.full_name}\nUsername: @{user.username or 'N/A'}\nID: `{user.id}`",
    )


# ── button_handler ────────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # Query too old (Render free tier spin-up delay)

    user = query.from_user

    if user_manager.is_banned(user.id) and not is_admin(user.id):
        await query.message.reply_text(
            "🚫 *You are banned from using this bot.*", parse_mode=ParseMode.MARKDOWN
        )
        return

    user_manager.register_user(user.id, user.username, user.full_name)

    if query.data.startswith("admin_") or query.data.startswith("remove_channel_") \
            or query.data.startswith("set_limit_") or query.data.startswith("manage_admin_") \
            or query.data == "admin_toggle_zip":
        if not is_admin(user.id):
            await query.message.reply_text(
                "⛔ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN
            )
            return
        await admin_callback(update, context)
        return

    if user.id not in [MAIN_ADMIN_ID] + bot_data.get("admin_ids", []):
        if force_channels and not await check_subscription(user.id, context):
            await query.message.reply_text(
                "⚠️ *Access Denied!*\n\nJoin the required channels first!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=await get_subscription_keyboard(),
            )
            return

    # Dynamic quality selection
    if query.data.startswith("dldyn_"):
        height = int(query.data.replace("dldyn_", ""))
        url    = context.user_data.get("pending_url")
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
        await do_download(update, context, url, height)
        return

    if query.data == "cancel_download":
        context.user_data.pop("pending_url", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # Subscription verification
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

    # User stats
    if query.data == "user_stats":
        uid = str(user.id)
        limit_mb, used_mb = db_get_limit(user.id)
        if uid in bot_data["users"]:
            info = bot_data["users"][uid]
            join_date = datetime.fromisoformat(info["join_date"]).strftime("%Y-%m-%d")
            limit_str = (
                f"{format_size(used_mb)} / {format_size(limit_mb)}"
                if limit_mb > 0
                else f"{format_size(used_mb)} / Unlimited"
            )
            txt = (
                f"📊 *Your Statistics*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 Joined: {join_date}\n"
                f"📥 Downloads: {info['total_downloads']:,}\n"
                f"💾 Data Used: {limit_str}\n"
                f"🆔 User ID: `{user.id}`\n"
                f"👤 Username: @{user.username or 'None'}\n"
                f"✅ Verified: {'Yes' if info.get('verified') else 'No'}"
            )
        else:
            txt = "📊 No stats yet. Send a link to start downloading!"
        kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # How to use
    if query.data == "how_to_use":
        txt = (
            "❓ *How to Use*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "*Option 1 — Quick link:*\n"
            "Paste any video URL → Bot fetches all available qualities → You pick one!\n\n"
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
            "⚠️ *Limit:* Max 50 MB for Telegram.\n"
            "🗑️ *Auto-delete:* Videos delete after 5 min to save space."
        )
        kb = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")]]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # About
    if query.data == "about":
        total_users, total_downloads, active_today, uptime = user_manager.get_stats()
        txt = (
            f"ℹ️ *About Bot*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Name: Universal Media Downloader\n"
            f"👨‍💻 Developer: MANDAL ({CONTACT_USERNAME})\n"
            f"📊 Version: 4.0 Ultimate\n\n"
            f"📈 *Live Stats:*\n"
            f"  👥 Total Users: {total_users:,}\n"
            f"  📥 Downloads: {total_downloads:,}\n"
            f"  🟢 Active Today: {active_today}\n"
            f"  ⏱️ Uptime: {uptime}\n\n"
            f"⚡ Powered by: yt-dlp & python-telegram-bot"
        )
        kb = [
            [InlineKeyboardButton(f"📞 Contact {CONTACT_USERNAME}", url=f"https://t.me/{CONTACT_USERNAME.lstrip('@')}")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main")],
        ]
        await query.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # MP3 mode
    if query.data == "mp3_mode":
        context.user_data["mp3_mode"] = True
        await query.message.reply_text(
            "🎵 *MP3 Mode Active!*\n\n"
            "Send me any video link and I'll extract the audio as MP3.\n"
            "Supported: YouTube, Instagram, TikTok, Twitter, Facebook",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]]),
        )
        return

    if query.data == "dl_video":
        context.user_data["mp3_mode"] = False
        await query.message.reply_text(
            "📥 *Download Video*\n\n"
            "Send me a video link — I'll fetch all available qualities for you!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]]),
        )
        return

    if query.data == "back_to_main":
        try:
            await query.message.delete()
        except Exception:
            pass
        await start(update, context)
        return

    if query.data.startswith("platform_"):
        platform = query.data.replace("platform_", "").upper()
        context.user_data["platform"] = platform
        context.user_data["mp3_mode"] = False
        await query.message.reply_text(
            f"📥 *{platform} Downloader*\n\nSend me the video link:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]]),
        )
        return


# ── handle_message ────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id

    if user_id in waiting_for_input:
        await handle_admin_input(update, context)
        return

    if user_manager.is_banned(user_id) and not is_admin(user_id):
        await update.message.reply_text(
            "🚫 *You are banned from using this bot.*", parse_mode=ParseMode.MARKDOWN
        )
        return

    if maintenance_mode and not is_admin(user_id):
        await update.message.reply_text(
            "🔧 *Bot is under maintenance.*\nPlease try again later.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if force_channels and not await check_subscription(user_id, context):
        await update.message.reply_text(
            "⚠️ *Access Denied!*\n\nJoin the required channels to use this bot.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await get_subscription_keyboard(),
        )
        return

    text = update.message.text or ""
    url_hints = [".com", ".org", ".net", "http", "www",
                 "youtu", "instagram", "tiktok", "twitter", "facebook"]
    if not any(h in text.lower() for h in url_hints):
        await update.message.reply_text(
            "❌ *Please send a valid video URL!*", parse_mode=ParseMode.MARKDOWN
        )
        return

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

    # MP3 mode: skip quality picker
    if context.user_data.get("mp3_mode"):
        await do_download(update, context, url, 0)
        return

    # Playlist: auto-download
    if is_playlist_url(url):
        await do_playlist_download(update, context, url)
        return

    # Smart link: fetch formats + show info
    fetching_msg = await update.message.reply_text(
        "🔍 *Fetching video info...*", parse_mode=ParseMode.MARKDOWN
    )

    info = await download_manager.fetch_formats(url)
    try:
        await fetching_msg.delete()
    except Exception:
        pass

    if not info.get("ok"):
        await update.message.reply_text(
            "❌ *Could not fetch video info.*\nCheck the URL and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    title    = clean_filename(info["title"])
    duration = info.get("duration", 0)
    formats  = info.get("formats", [])
    wait_str = estimate_wait(int(duration)) if duration else "unknown"
    dur_str  = f"{int(duration)//60}:{int(duration)%60:02d}" if duration else "?"

    quality_lines = ""
    for fmt in formats:
        if fmt["height"] > 0:
            fs = fmt.get("filesize", 0)  # bytes
            size_str = f"  •  ~{format_size(fs / 1024 / 1024)}" if fs else ""
            quality_lines += f"  • {fmt['label']}{size_str}\n"

    info_text = (
        f"🎬 *{title[:60]}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Duration: {dur_str}  •  Est. wait: {wait_str}\n"
        f"🌐 {detect_platform(url)}\n\n"
        f"📺 *Available Qualities:*\n{quality_lines}\n"
        f"👇 *Choose your quality:*"
    )

    context.user_data["pending_url"] = url

    await update.message.reply_text(
        info_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_quality_keyboard(formats, url),
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
    if not is_admin(user_id):
        target = update.message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text(
                "⛔ *Unauthorized Access!*\nAdmin only.", parse_mode=ParseMode.MARKDOWN
            )
        return

    global maintenance_mode, zip_enabled

    total_users, total_downloads, active_today, uptime = user_manager.get_stats()
    verified_users = sum(1 for u in bot_data["users"].values() if u.get("verified"))
    banned_count   = len(bot_data.get("banned_users", []))
    extra_admins   = len(bot_data.get("admin_ids", []))

    keyboard = [
        [
            InlineKeyboardButton("📢 Broadcast",       callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Stats",           callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("🔧 Maintenance",     callback_data="admin_maintenance"),
            InlineKeyboardButton("➕ Add Channel",      callback_data="admin_add_channel"),
        ],
        [
            InlineKeyboardButton("➖ Remove Channel",   callback_data="admin_remove_channel"),
            InlineKeyboardButton("📋 Channels",        callback_data="admin_channels_list"),
        ],
        [
            InlineKeyboardButton("👥 Users List",      callback_data="admin_users_list"),
            InlineKeyboardButton("🖼️ Set Banner",       callback_data="admin_set_banner"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Verif.",    callback_data="admin_reset_verifications"),
            InlineKeyboardButton("📊 Export Users",    callback_data="admin_export_users"),
        ],
        [
            InlineKeyboardButton(f"🚫 Banned ({banned_count})", callback_data="admin_banned_list"),
            InlineKeyboardButton("📋 Dl History",      callback_data="admin_download_history"),
        ],
        [
            InlineKeyboardButton("🔒 Set User Limit",  callback_data="admin_set_limit"),
            InlineKeyboardButton("📈 Limit Stats",     callback_data="admin_limit_stats"),
        ],
        [
            InlineKeyboardButton(f"👑 Manage Admins ({extra_admins})", callback_data="admin_manage_admins"),
        ],
        [
            InlineKeyboardButton(
                f"📦 User Zip: {'🟢 ON' if zip_enabled else '🔴 OFF'}",
                callback_data="admin_toggle_zip",
            ),
        ],
        [
            InlineKeyboardButton("🔙 Main Menu",       callback_data="back_to_main"),
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
        f"📦 User Zip: {'🟢 ON' if zip_enabled else '🔴 OFF'}\n"
        f"🚫 Banned: {banned_count}  •  👑 Extra Admins: {extra_admins}\n"
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
    global maintenance_mode, force_channels, BANNER_FILE_ID, BANNER_URL, bot_data, zip_enabled

    back_btn = [[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]]

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

    elif query.data == "admin_maintenance":
        maintenance_mode = not maintenance_mode
        bot_data["maintenance_mode"] = maintenance_mode
        save_data(bot_data)
        try:
            await query.answer(f"Maintenance {'enabled' if maintenance_mode else 'disabled'}!")
        except Exception:
            pass
        await admin_panel(update, context)

    elif query.data == "admin_add_channel":
        waiting_for_input[update.effective_user.id] = "add_channel"
        await query.message.edit_text(
            "📢 *Add Force Channel*\n\n"
            "Send the channel link or username:\n"
            "• Public: `@username` or `https://t.me/username`\n"
            "• Private: `https://t.me/+invitehash`\n\n"
            "⚠️ I must be admin in the channel!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn),
        )

    elif query.data == "admin_remove_channel":
        if not force_channels:
            try:
                await query.answer("No channels to remove!")
            except Exception:
                pass
            return
        keyboard = [
            [InlineKeyboardButton(
                f"❌ @{ch['identifier']}" if ch.get("type") == "public"
                else f"❌ Private ({ch['invite_hash'][:8]}...)",
                callback_data=f"remove_channel_{i}",
            )]
            for i, ch in enumerate(force_channels)
        ]
        keyboard += back_btn
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
            try:
                await query.answer(f"Removed {display}!")
            except Exception:
                pass
            await admin_panel(update, context)

    elif query.data == "admin_channels_list":
        if not force_channels:
            try:
                await query.answer("No channels configured!")
            except Exception:
                pass
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
            uname  = info.get("username", "N/A")
            dl     = info.get("total_downloads", 0)
            last   = datetime.fromisoformat(info["last_active"]).strftime("%m-%d %H:%M")
            flag   = " 🚫" if uid in banned_list else ""
            adm    = " 👑" if int(uid) in bot_data.get("admin_ids", []) else ""
            txt   += f"• @{uname} — {dl} dl — {last}{flag}{adm}\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n_(Truncated)_"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    elif query.data == "admin_set_banner":
        waiting_for_input[update.effective_user.id] = "set_banner"
        await query.message.edit_text(
            "🖼️ *Set Banner Image*\n\nSend a photo directly or an image URL.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn),
        )

    elif query.data == "admin_reset_verifications":
        for uid in bot_data["users"]:
            bot_data["users"][uid]["verified"] = False
        save_data(bot_data)
        try:
            await query.answer("All verifications reset!")
        except Exception:
            pass
        await admin_panel(update, context)

    elif query.data == "admin_export_users":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "User ID", "Username", "Full Name",
            "Join Date", "Last Active", "Downloads", "Verified", "Banned",
            "Limit MB", "Used MB",
        ])
        banned_list = bot_data.get("banned_users", [])
        for uid, info in bot_data["users"].items():
            lmb, umb = db_get_limit(int(uid))
            writer.writerow([
                uid,
                info.get("username", ""),
                info.get("full_name", ""),
                info.get("join_date", ""),
                info.get("last_active", ""),
                info.get("total_downloads", 0),
                info.get("verified", False),
                uid in banned_list,
                lmb,
                round(umb, 2),
            ])
        await query.message.reply_document(
            document=io.BytesIO(output.getvalue().encode()),
            filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            caption="📊 Users Export",
        )
        try:
            await query.answer("Export sent!")
        except Exception:
            pass

    elif query.data == "admin_broadcast":
        waiting_for_input[update.effective_user.id] = "broadcast"
        await query.message.edit_text(
            "📢 *Broadcast Message*\n\n"
            "Send any message to broadcast to all users.\n"
            f"👥 Recipients: {len(bot_data['users']):,}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn),
        )

    elif query.data == "admin_banned_list":
        banned_list = bot_data.get("banned_users", [])
        if not banned_list:
            try:
                await query.answer("No banned users!")
            except Exception:
                pass
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

    elif query.data == "admin_download_history":
        history = bot_data.get("download_history", [])
        total   = len(history)
        if not history:
            try:
                await query.answer("No download history yet!")
            except Exception:
                pass
            return
        recent    = list(reversed(history[-20:]))
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
            ts    = datetime.fromisoformat(entry["timestamp"]).strftime("%m-%d %H:%M")
            uid   = entry.get("user_id", "?")
            uname = bot_data["users"].get(uid, {}).get("username", uid)
            plat  = entry.get("platform", "?")
            txt  += f"• @{uname} — {plat} — {ts}\n"
        if len(txt) > 4000:
            txt = txt[:4000] + "...\n_(Truncated)_"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    elif query.data == "admin_set_limit":
        waiting_for_input[update.effective_user.id] = "set_limit"
        await query.message.edit_text(
            "🔒 *Set Download Limit for User*\n\n"
            "Send in this format:\n"
            "`<user_id> <limit_mb>`\n\n"
            "*Examples:*\n"
            "`123456789 500` — 500 MB limit\n"
            "`123456789 0` — Unlimited\n"
            "`123456789 51200` — 50 GB limit\n\n"
            "Range: 1 MB to 100 GB (102400 MB)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn),
        )

    elif query.data == "admin_limit_stats":
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("SELECT user_id, limit_mb, used_mb FROM user_limits ORDER BY used_mb DESC LIMIT 15")
        rows = c.fetchall()
        conn.close()
        if not rows:
            try:
                await query.answer("No limit data yet!")
            except Exception:
                pass
            return
        txt = "📈 *User Limit Stats (Top 15 by Usage)*\n\n"
        for row in rows:
            uid2  = str(row[0])
            uname = bot_data["users"].get(uid2, {}).get("username", uid2)
            lmb   = row[1]
            umb   = row[2]
            lstr  = format_size(lmb) if lmb > 0 else "Unlimited"
            txt  += f"• @{uname}: {format_size(umb)} / {lstr}\n"
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back_btn)
        )

    elif query.data == "admin_manage_admins":
        if update.effective_user.id != MAIN_ADMIN_ID:
            try:
                await query.answer("Only main admin can manage admins!", show_alert=True)
            except Exception:
                pass
            return
        admins = bot_data.get("admin_ids", [])
        txt    = (
            f"👑 *Admin Management*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔑 Main Admin: `{MAIN_ADMIN_ID}` (permanent)\n\n"
            f"*Extra Admins ({len(admins)}):*\n"
        )
        kb = []
        for aid in admins:
            uname = bot_data["users"].get(str(aid), {}).get("username", str(aid))
            txt  += f"• `{aid}` — @{uname}\n"
            kb.append([InlineKeyboardButton(
                f"❌ Remove @{uname}", callback_data=f"manage_admin_remove_{aid}"
            )])
        if not admins:
            txt += "None yet.\n"
        txt += f"\n💡 Use `/addadmin <user_id>` to add\nor `/removeadmin <user_id>` to remove."
        kb.append([InlineKeyboardButton("➕ Add Admin", callback_data="manage_admin_add")])
        kb += back_btn
        await query.message.edit_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data == "manage_admin_add":
        if update.effective_user.id != MAIN_ADMIN_ID:
            try:
                await query.answer("Only main admin!", show_alert=True)
            except Exception:
                pass
            return
        waiting_for_input[update.effective_user.id] = "add_admin"
        await query.message.edit_text(
            "👑 *Add Admin*\n\nSend the user_id to make admin:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn),
        )

    elif query.data.startswith("manage_admin_remove_"):
        # FIX #2: Only one admin_callback call — removed duplicate invocation
        if update.effective_user.id != MAIN_ADMIN_ID:
            try:
                await query.answer("Only main admin!", show_alert=True)
            except Exception:
                pass
            return
        target_id = int(query.data.replace("manage_admin_remove_", ""))
        if remove_admin(target_id):
            uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
            try:
                await query.answer(f"Removed @{uname} from admins!")
            except Exception:
                pass
        else:
            try:
                await query.answer("Could not remove (main admin is permanent)!")
            except Exception:
                pass
        # FIX #2: Re-render manage admins page directly — single call only
        query.data = "admin_manage_admins"
        await admin_callback(update, context)

    elif query.data == "admin_toggle_zip":
        zip_enabled = not zip_enabled
        bot_data["zip_enabled"] = zip_enabled
        save_data(bot_data)
        status_txt = "🟢 ON" if zip_enabled else "🔴 OFF"
        try:
            await query.answer(f"📦 User Zip is now {status_txt}!", show_alert=True)
        except Exception:
            pass
        await admin_panel(update, context)

    elif query.data == "admin_panel":
        await admin_panel(update, context)


# ── handle_admin_input ────────────────────────────────────────────────────────────
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    action  = waiting_for_input.get(user_id)
    if not action:
        return

    global BANNER_FILE_ID, BANNER_URL, bot_data, force_channels

    if action == "add_channel":
        text     = update.message.text or update.message.caption or ""
        ch_type, identifier = await extract_channel_info(text)

        if not ch_type:
            await update.message.reply_text(
                "❌ *Invalid format!*\nSend `@channel` or `https://t.me/+invite`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

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
                        f"❌ *I'm not admin in @{identifier}!*", parse_mode=ParseMode.MARKDOWN
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

    elif action == "set_limit":
        text  = (update.message.text or "").strip()
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text(
                "❌ *Format:* `<user_id> <limit_mb>`\nExample: `123456789 500`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            target_id = int(parts[0])
            limit_mb  = float(parts[1])
        except ValueError:
            await update.message.reply_text(
                "❌ *Invalid values.* Both must be numbers.", parse_mode=ParseMode.MARKDOWN
            )
            return
        if limit_mb < 0 or limit_mb > 102400:
            await update.message.reply_text(
                "❌ *Limit must be 0–102400 MB* (0 = unlimited)", parse_mode=ParseMode.MARKDOWN
            )
            return
        db_set_limit(target_id, limit_mb)
        uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
        lstr  = format_size(limit_mb) if limit_mb > 0 else "Unlimited"
        await update.message.reply_text(
            f"✅ *Limit set!*\nUser: @{uname} (`{target_id}`)\nNew limit: *{lstr}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        del waiting_for_input[user_id]

    elif action == "add_admin":
        if user_id != MAIN_ADMIN_ID:
            del waiting_for_input[user_id]
            return
        text = (update.message.text or "").strip()
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ *Send a valid user_id (number)*", parse_mode=ParseMode.MARKDOWN
            )
            return
        if add_admin(target_id):
            uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
            await update.message.reply_text(
                f"✅ *@{uname} (`{target_id}`) is now an admin!*",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                "⚠️ *Already an admin or is main admin.*", parse_mode=ParseMode.MARKDOWN
            )
        del waiting_for_input[user_id]


# ── Command handlers ──────────────────────────────────────────────────────────────
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    total_users, total_dl, active_today, uptime = user_manager.get_stats()
    verified = sum(1 for u in bot_data["users"].values() if u.get("verified"))
    banned   = len(bot_data.get("banned_users", []))
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {total_users:,}   ✅ Verified: {verified:,}\n"
        f"🚫 Banned: {banned}\n"
        f"📥 Downloads: {total_dl:,}\n"
        f"🟢 Active Today: {active_today}\n"
        f"⏱️ Uptime: {uptime}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    waiting_for_input[update.effective_user.id] = "broadcast"
    await update.message.reply_text(
        f"📢 *Broadcast Message*\nSend the message to broadcast.\n👥 Recipients: {len(bot_data['users']):,}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return
    if is_admin(target_id):
        await update.message.reply_text("❌ Cannot ban an admin!", parse_mode=ParseMode.MARKDOWN)
        return
    if user_manager.ban_user(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", "Unknown")
        await update.message.reply_text(
            f"🚫 *Banned*\nID: `{target_id}`\nUsername: @{uname}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` is already banned.", parse_mode=ParseMode.MARKDOWN)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return
    if user_manager.unban_user(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", "Unknown")
        await update.message.reply_text(
            f"✅ *Unbanned*\nID: `{target_id}`\nUsername: @{uname}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` is not banned.", parse_mode=ParseMode.MARKDOWN)


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MAIN_ADMIN_ID:
        await update.message.reply_text("⛔ *Only main admin can add admins!*", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/addadmin <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return
    if add_admin(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
        await update.message.reply_text(
            f"✅ *@{uname} (`{target_id}`) is now an admin!*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚠️ *Already an admin or is main admin.*", parse_mode=ParseMode.MARKDOWN)


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MAIN_ADMIN_ID:
        await update.message.reply_text("⛔ *Only main admin can remove admins!*", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removeadmin <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return
    if remove_admin(target_id):
        uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
        await update.message.reply_text(
            f"✅ *@{uname} (`{target_id}`) removed from admins.*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚠️ *Not an admin or is main admin (permanent).*", parse_mode=ParseMode.MARKDOWN)


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setlimit <user_id> <mb>`\nExample: `/setlimit 123456 500`\n`0` = unlimited",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        limit_mb  = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid values.", parse_mode=ParseMode.MARKDOWN)
        return
    db_set_limit(target_id, limit_mb)
    lstr  = format_size(limit_mb) if limit_mb > 0 else "Unlimited"
    uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
    await update.message.reply_text(
        f"✅ *Limit updated!*\n@{uname} (`{target_id}`) → *{lstr}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def resetusage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/resetusage <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return
    db_reset_usage(target_id)
    uname = bot_data["users"].get(str(target_id), {}).get("username", str(target_id))
    await update.message.reply_text(
        f"✅ *Usage reset for @{uname} (`{target_id}`)*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    init_db()
    startup_check()

    async def _post_init(app):
        global _global_dl_semaphore
        _global_dl_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        logger.info(f"Download semaphore initialised (limit={MAX_CONCURRENT_DOWNLOADS})")

    async def _post_shutdown(app):
        count = 0
        for d in glob.glob("/tmp/tmp*"):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                count += 1
        logger.info(f"Shutdown: cleaned {count} temp dirs")
        print(f"\n✅ Bot offline — {count} temp dirs cleaned.\n")

    # Large timeouts for big file uploads (up to 2GB documents)
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=600,
        write_timeout=600,
        connect_timeout=30,
        pool_timeout=600,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start",       start))
    application.add_handler(CommandHandler("admin",       admin_panel))
    application.add_handler(CommandHandler("stats",       stats_command))
    application.add_handler(CommandHandler("broadcast",   broadcast_command))
    application.add_handler(CommandHandler("ban",         ban_command))
    application.add_handler(CommandHandler("unban",       unban_command))
    application.add_handler(CommandHandler("addadmin",    addadmin_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("setlimit",    setlimit_command))
    application.add_handler(CommandHandler("resetusage",  resetusage_command))
    application.add_handler(CommandHandler("cancel",      cancel_command))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE,
        handle_message,
    ))

    application.add_error_handler(error_handler)

    logger.info("🚀 Bot v4.0 is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
