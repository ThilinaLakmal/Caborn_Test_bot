# ======================================================================
# DNS RESOLUTION MONKEY PATCH
# Overrides aiodns/pycares which breaks MetaApi websocket connections
# when IPv6 DNS is present. Must run before any aiohttp instances are made.
# ======================================================================
import aiohttp.resolver
import aiohttp.connector
aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver

from config import TELEGRAM_BOT_TOKEN, APP_MODE
import telebot
import time
import sys
import os
import tempfile
import hashlib
import atexit

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


def _release_single_instance_lock():
    """Release lock file when process exits."""
    global _lock_file
    lock_file = globals().get("_lock_file")
    if not lock_file:
        return

    try:
        if os.name == "nt" and msvcrt:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass

    try:
        lock_file.close()
    except OSError:
        pass

def enforce_single_instance():
    """Ensure only one local process per bot token runs at a time."""
    token_fingerprint = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).hexdigest()[:12]
    lock_filename = f"telegram_bot_{token_fingerprint}.lock"
    lock_path = os.path.join(tempfile.gettempdir(), lock_filename)

    try:
        global _lock_file
        _lock_file = open(lock_path, "a+")
        _lock_file.seek(0)
        if _lock_file.read(1) == "":
            _lock_file.seek(0)
            _lock_file.write("1")
            _lock_file.flush()
        _lock_file.seek(0)

        if os.name == "nt" and msvcrt:
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise RuntimeError("Unsupported platform for file locking.")

        atexit.register(_release_single_instance_lock)
    except (OSError, RuntimeError):
        print("\n" + "="*60)
        print("❌ CRITICAL ERROR: Bot is already running!")
        print("="*60)
        print("Another local instance of this bot token is already running.")
        print("This causes a Telegram API '409 Conflict' error.")
        print("Please stop the other instance (e.g., background terminal, Task Manager, or VPS) before starting this one.")
        print("="*60 + "\n")
        sys.exit(1)

from handlers.start_handler import handle_start_command
from handlers.callback_handler import handle_callback_query
from utils.status_monitor import start_status_monitor
from utils.bg_loop import start_background_loop

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# --- TELEGRAM API ERROR PROTECTION MONKEY PATCH ---
original_edit_message_text = bot.edit_message_text
original_answer_callback_query = bot.answer_callback_query

def safe_edit_message_text(*args, **kwargs):
    try:
        return original_edit_message_text(*args, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" in str(e).lower():
            pass # Ignore benign error
        else:
            raise

def safe_answer_callback_query(*args, **kwargs):
    try:
        return original_answer_callback_query(*args, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            pass # Ignore benign error
        else:
            raise

bot.edit_message_text = safe_edit_message_text
bot.answer_callback_query = safe_answer_callback_query
# --------------------------------------------------

@bot.message_handler(commands=['start'])
def start(message):
    """Handle /start command"""
    handle_start_command(bot, message)


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    """Handle all callback queries from inline buttons"""
    handle_callback_query(bot, call)


if __name__ == "__main__":
    # Prevent multiple instances from running concurrently
    enforce_single_instance()

    # Display current mode
    mode_emoji = "🟢" if APP_MODE == "DEV" else "🔴"
    mode_text = "TESTNET (Fake Money)" if APP_MODE == "DEV" else "LIVE TRADING (Real Money)"
    print(f"\n{'='*60}")
    print(f"{mode_emoji} BOT MODE: {APP_MODE} - {mode_text} {mode_emoji}")
    print(f"{'='*60}\n")
    
    # Start background event loop
    start_background_loop()
    
    # Start status monitoring system
    start_status_monitor(bot)
    
    # Start bot polling with connection retry logic
    print("🤖 Bot started and polling...")
    
    while True:
        try:
            bot.polling(non_stop=True, timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            print("🔄 Reconnecting in 5 seconds...")
            time.sleep(5)
            continue
