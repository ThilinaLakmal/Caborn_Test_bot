"""
Welcome message templates for different user types and statuses
"""
from config import BOT_NAME, MAIN_ADMIN
from telebot import types


def get_admin_welcome(user_name, username, user_id):
    """Generate admin dashboard welcome message and markup"""
    welcome_text = f"""
🎯 <b>ADMIN DASHBOARD</b>

👋 Welcome back, <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━━━
📊 <b>System Status:</b> ✅ Online
⚙️ <b>Access Level:</b> Administrator

Use the buttons below to manage the bot:
    """
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Pending Approvals", callback_data="admin_pending"),
        types.InlineKeyboardButton("👥 Manage Users", callback_data="admin_users")
    )
    markup.add(
        types.InlineKeyboardButton("📊 View Stats", callback_data="admin_stats"),
        types.InlineKeyboardButton("📈 Trading Overview", callback_data="admin_trading")
    )
    markup.add(
        types.InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_settings"),
        types.InlineKeyboardButton("📝 View Logs", callback_data="admin_logs")
    )
    markup.add(
        types.InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="admin_refresh")
    )
    
    return welcome_text, markup


def get_active_user_welcome(user_name, username, user_id, trading_active=False, trading_mode=None, platform="binance"):
    """Generate welcome message for active users with platform info"""
    # Platform display info
    if platform == "binance":
        platform_emoji = "📈"
        platform_name = "Binance Crypto"
    elif platform == "mt5":
        platform_emoji = "💹"
        platform_name = "MT5 Forex/Gold"
    elif platform == "mexc":
        platform_emoji = "🔷"
        platform_name = "MEXC Futures"
    else:  # all
        platform_emoji = "🌐"
        platform_name = "Binance + MEXC + MT5"
    
    if trading_active:
        welcome_text = f"""
✨ <b>WELCOME BACK!</b>

👋 Hello <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>
{platform_emoji} <b>Platform:</b> {platform_name}

━━━━━━━━━━━━━━━━━━━━━━
✅ <b>Status:</b> Active
🔐 <b>Access:</b> Authorized
🟢 <b>Trading:</b> {trading_mode} Running

Use buttons below to manage your active trading session.
        """
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📊 View Trading", callback_data="view_status"),
            types.InlineKeyboardButton("💰 Balance", callback_data="view_balance")
        )
        markup.add(
            types.InlineKeyboardButton("🛑 Stop Trading", callback_data="stop_trading"),
            types.InlineKeyboardButton("❓ Help", callback_data="user_help")
        )
    else:
        welcome_text = f"""
✨ <b>WELCOME BACK!</b>

👋 Hello <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>
{platform_emoji} <b>Platform:</b> {platform_name}

━━━━━━━━━━━━━━━━━━━━━━
✅ <b>Status:</b> Active
🔐 <b>Access:</b> Authorized

Your account is active and ready!
        """
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🚀 Start Trading", callback_data="start_trading"),
            types.InlineKeyboardButton("⚙️ Settings", callback_data="user_settings")
        )
        markup.add(
            types.InlineKeyboardButton("❓ Help", callback_data="user_help")
        )
    
    return welcome_text, markup


def get_pending_user_welcome(user_name, username, user_id):
    """Generate welcome message for pending users"""
    welcome_text = f"""
⏳ <b>REGISTRATION PENDING</b>

👋 Hello <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━━━
⏳ <b>Status:</b> Pending Approval
🔒 <b>Access:</b> Waiting for Admin

Your registration is under review. 
Please wait for admin approval.

📧 <b>Contact:</b> {MAIN_ADMIN}
    """
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❓ Need Help?", callback_data="user_help"))
    
    return welcome_text, markup


def get_rejected_user_welcome(user_name, username, user_id):
    """Generate welcome message for rejected users"""
    welcome_text = f"""
❌ <b>ACCESS DENIED</b>

👋 Hello <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━━━
❌ <b>Status:</b> Registration Rejected

Your registration was not approved.
Please contact the admin for more information.

📧 <b>Contact:</b> {MAIN_ADMIN}
    """
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📧 Contact Admin", url=f"https://t.me/{MAIN_ADMIN.replace('@', '')}"))
    
    return welcome_text, markup


def get_new_user_welcome(user_name, username, user_id):
    """Generate welcome message for new users"""
    welcome_text = f"""
🌟 <b>WELCOME TO {BOT_NAME}!</b>

👋 Hello <b>{user_name}</b>!
━━━━━━━━━━━━━━━━━━━━━━

Thank you for starting the bot!

🤖 <b>Bot:</b> {BOT_NAME}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━━━
ℹ️ <b>Status:</b> New User
🔓 <b>Access:</b> Not Registered

To use this bot, you need to register with your API credentials.

📧 <b>For Support:</b> {MAIN_ADMIN}
    """
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📝 Register Now", callback_data="user_register"),
        types.InlineKeyboardButton("ℹ️ Learn More", callback_data="user_info")
    )
    
    return welcome_text, markup
