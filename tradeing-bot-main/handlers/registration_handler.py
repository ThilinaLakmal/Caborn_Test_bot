"""
User registration conversation handler
Handles the multi-step registration process for Binance, MT5, MEXC, and all platforms.
"""
from telebot import types
import telebot.apihelper
from config import MAIN_ADMIN, BOT_CREATOR_ID
from utils.logging_service import logger
from user_control.add_users import (
    add_user,
    add_user_mt5,
    add_user_mexc,
    add_user_all_platforms,
    is_api_key_in_use,
    is_api_secret_in_use,
    is_mt5_login_in_use,
    is_mexc_api_key_in_use,
    is_mexc_api_secret_in_use,
)


def _safe_answer_callback_query(bot, call_id, text=None, show_alert=False):
    """
    Safely answer callback query without crashing if query times out.
    Silently ignores 'query too old' errors (don't notify user).
    """
    try:
        bot.answer_callback_query(call_id, text=text, show_alert=show_alert)
    except telebot.apihelper.ApiTelegramException as e:
        if "query is too old" in str(e) or "query ID is invalid" in str(e):
            # Query expired - silently ignore (no user notification)
            pass
        else:
            raise  # Re-raise other API errors


def _get_text(message):
    """Safely extract stripped text from a message. Returns None for non-text messages."""
    if message.text is None:
        return None
    return message.text.strip()


# Temporary storage for registration data
registration_data = {}


def notify_admin_new_registration(bot, telegram_id, name, platform):
    """
    Notify admin when a new user completes registration.
    Includes Accept/Reject buttons for immediate action.
    """
    try:
        # Platform display
        if platform == "binance":
            platform_emoji = "📈"
            platform_name = "Binance Crypto Trading"
            platform_desc = "Spot & Futures Trading"
        elif platform == "mt5":
            platform_emoji = "💹"
            platform_name = "MT5 Forex/Gold Trading"
            platform_desc = "Forex & Gold Trading"
        elif platform == "mexc":
            platform_emoji = "🔷"
            platform_name = "MEXC Futures Trading"
            platform_desc = "Crypto Futures Trading"
        else:  # all
            platform_emoji = "🌐"
            platform_name = "All Platforms"
            platform_desc = "Binance + MT5 + MEXC Trading"
        
        # Create inline keyboard with Accept/Reject buttons
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Accept", callback_data=f"approve_{telegram_id}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{telegram_id}")
        )
        markup.add(
            types.InlineKeyboardButton("👁️ View Details", callback_data=f"view_pending_{telegram_id}"),
            types.InlineKeyboardButton("📋 All Pending", callback_data="admin_pending")
        )
        
        notification_text = (
            "🔔 <b>NEW REGISTRATION REQUEST</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>Name:</b> {name}\n"
            f"🆔 <b>Telegram ID:</b> <code>{telegram_id}</code>\n"
            f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
            f"📊 <b>Trading Type:</b> {platform_desc}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏳ <b>Status:</b> Pending Approval\n\n"
            "👇 <b>Take action below:</b>"
        )
        
        bot.send_message(
            BOT_CREATOR_ID,
            notification_text,
            parse_mode='HTML',
            reply_markup=markup
        )
        
    except Exception as e:
        print(f"[ERROR] Failed to notify admin: {e}")


def start_registration(bot, call):
    """Start the registration process - ask for platform choice first"""
    user_id = call.from_user.id
    registration_data[user_id] = {}
    
    # Create platform selection buttons
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📈 Binance Crypto Trading", callback_data="reg_platform_binance")
    )
    markup.add(
        types.InlineKeyboardButton("🔷 MEXC Futures Trading", callback_data="reg_platform_mexc")
    )
    markup.add(
        types.InlineKeyboardButton("💹 MT5 Forex/Gold Trading", callback_data="reg_platform_mt5")
    )
    markup.add(
        types.InlineKeyboardButton("🌐 All Platforms (Binance + MEXC + MT5)", callback_data="reg_platform_all")
    )
    markup.add(
        types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_registration")
    )

    _safe_answer_callback_query(bot, call.id)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📝 <b>Registration Started</b>\n\n"
                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 "🔹 <b>Select your trading platform:</b>\n\n"
                 "📈 <b>Binance</b> - Trade crypto (Spot/Futures)\n"
                 "🔷 <b>MEXC</b> - Trade crypto futures\n"
                 "💹 <b>MT5</b> - Trade Forex & Gold\n"
                 "🌐 <b>All Platforms</b> - Trade on all three\n\n"
                 "<i>Choose the platform(s) you want to register for:</i>",
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception:
        pass


def handle_platform_selection(bot, call, platform):
    """Handle platform selection and proceed to name collection"""
    user_id = call.from_user.id
    
    if user_id not in registration_data:
        registration_data[user_id] = {}
    
    registration_data[user_id]['platform'] = platform
    
    if platform == "binance":
        platform_name = "Binance Crypto"
    elif platform == "mt5":
        platform_name = "MT5 Forex/Gold"
    elif platform == "mexc":
        platform_name = "MEXC Futures"
    else:  # all
        platform_name = "All Platforms (Binance + MEXC + MT5)"
    
    _safe_answer_callback_query(bot, call.id)
    
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"📝 <b>Registration - {platform_name}</b>\n\n"
                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 "Please enter your <b>full name</b>:",
            parse_mode='HTML'
        )
    except Exception as e:
        # If edit fails, send new message
        bot.send_message(
            call.message.chat.id,
            f"📝 <b>Registration - {platform_name}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please enter your <b>full name</b>:",
            parse_mode='HTML'
        )
    
    bot.register_next_step_handler(call.message, lambda msg: get_full_name(bot, msg))


def get_full_name(bot, message):
    """Collect user's full name and route to platform-specific credential collection"""
    user_id = message.from_user.id
    full_name = _get_text(message)
    if full_name is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_full_name(bot, msg))
        return
    
    if len(full_name) < 2:
        bot.send_message(
            message.chat.id,
            "❌ Name too short. Please enter your full name:"
        )
        bot.register_next_step_handler(message, lambda msg: get_full_name(bot, msg))
        return
    
    registration_data[user_id]['name'] = full_name
    platform = registration_data[user_id].get('platform', 'binance')
    
    if platform == 'binance':
        # Binance registration flow
        bot.send_message(
            message.chat.id,
            f"✅ Name: {full_name}\n\n"
            "🔑 Now, send your <b>Binance API Key</b>:\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to get your API Key:</b>\n\n"
            "1️⃣ Go to Binance → Account → API Management\n"
            "2️⃣ Create new API key\n"
            "3️⃣ Enable Futures trading\n"
            "4️⃣ Copy API Key and paste here\n\n"
            "⚠️ <b>Important:</b> Make sure Futures trading is enabled!",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_api_key(bot, msg))
    elif platform == 'mexc':
        # MEXC registration flow
        bot.send_message(
            message.chat.id,
            f"✅ Name: {full_name}\n\n"
            "🔑 Now, send your <b>MEXC API Key</b>:\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to get your MEXC API Key:</b>\n\n"
            "1️⃣ Go to MEXC → Profile → API Management\n"
            "2️⃣ Create a new API key\n"
            "3️⃣ Enable <b>Futures/Contract Trading</b> permission\n"
            "4️⃣ Copy the <b>API Key</b> value and paste here\n\n"
            "⚠️ <b>Important Notes:</b>\n"
            "• This is the <code>api_key</code> parameter\n"
            "• Must enable Futures/Contract trading permission\n"
            "• Keep it safe and never share!",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key(bot, msg))
    elif platform == 'mt5':
        # MT5 registration flow
        bot.send_message(
            message.chat.id,
            f"✅ Name: {full_name}\n\n"
            "🔢 Now, send your <b>MT5 Login ID</b>:\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to get your Login ID:</b>\n\n"
            "1️⃣ Click on your balance (top left, near sidebar)\n"
            "2️⃣ Click on <b>Manage</b>\n"
            "3️⃣ Click on <b>Account Information</b>\n"
            "4️⃣ Copy the <b>MT ID</b>",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_login(bot, msg))
    else:  # all platforms
        # Start with Binance credentials first
        bot.send_message(
            message.chat.id,
            f"✅ Name: {full_name}\n\n"
            "📝 <b>Step 1/3: Binance Registration</b>\n\n"
            "🔑 Now, send your <b>Binance API Key</b>:\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>How to get your API Key:</b>\n\n"
            "1️⃣ Go to Binance → Account → API Management\n"
            "2️⃣ Create new API key\n"
            "3️⃣ Enable Futures trading\n"
            "4️⃣ Copy API Key and paste here\n\n"
            "⚠️ <b>Important:</b> Make sure Futures trading is enabled!",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_api_key_all(bot, msg))


def get_api_key(bot, message):
    """Collect user's API key"""
    user_id = message.from_user.id
    api_key = _get_text(message)
    if api_key is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_api_key(bot, msg))
        return
    
    if len(api_key) < 20:
        bot.send_message(
            message.chat.id,
            "❌ Invalid API Key. Please send your Binance API Key:"
        )
        bot.register_next_step_handler(message, lambda msg: get_api_key(bot, msg))
        return
    
    # Check if API key is already in use
    key_in_use, existing_user_id = is_api_key_in_use(api_key)
    if key_in_use and existing_user_id != user_id:
        bot.send_message(
            message.chat.id,
            f"❌ This API Key is already registered.\n\nPlease use a different API Key:"
        )
        bot.register_next_step_handler(message, lambda msg: get_api_key(bot, msg))
        return
    
    registration_data[user_id]['api_key'] = api_key
    
    bot.send_message(
        message.chat.id,
        "✅ API Key saved\n\n"
        "🔐 Now, send your <b>Binance API Secret</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Where to find your Secret:</b>\n\n"
        "Your API Secret is shown on the same page where you got your API Key\n"
        "(Binance → API Management)\n\n"
        "⚠️ <b>Keep it safe!</b> Never share your secret with anyone.",
        parse_mode='HTML'
    )
    
    bot.register_next_step_handler(message, lambda msg: get_api_secret(bot, msg))


def get_api_secret(bot, message):
    """Collect user's API secret and complete Binance registration"""
    user_id = message.from_user.id
    api_secret = _get_text(message)
    if api_secret is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_api_secret(bot, msg))
        return
    
    if len(api_secret) < 20:
        bot.send_message(
            message.chat.id,
            "❌ Invalid API Secret. Please send your Binance API Secret:"
        )
        bot.register_next_step_handler(message, lambda msg: get_api_secret(bot, msg))
        return
    
    # Check if API secret is already in use
    secret_in_use, existing_user_id = is_api_secret_in_use(api_secret)
    if secret_in_use and existing_user_id != user_id:
        bot.send_message(
            message.chat.id,
            f"❌ This API Secret is already registered.\n\nPlease use a different API Secret:"
        )
        bot.register_next_step_handler(message, lambda msg: get_api_secret(bot, msg))
        return
    
    registration_data[user_id]['api_secret'] = api_secret
    
    # Save to database
    try:
        name = registration_data[user_id]['name']
        api_key = registration_data[user_id]['api_key']
        
        add_user(
            telegram_id=user_id,
            api_key=api_key,
            api_secret=api_secret,
            status="pending",
            language="en",
            name=name,
            platform="binance"
        )
        
        # Log successful registration
        logger.log_registration(
            user_id=user_id,
            platform="binance",
            status="SUCCESS",
            api_keys_present=True
        )
        
        # Notify admin about new registration
        notify_admin_new_registration(bot, user_id, name, "binance")
        
        # Clear registration data
        del registration_data[user_id]
        
        bot.send_message(
            message.chat.id,
            "✅ <b>Binance Registration Complete!</b>\n\n"
            "📈 <b>Platform:</b> Binance Crypto Trading\n"
            f"🆔 <b>Your Telegram ID:</b> <code>{user_id}</code>\n"
            "⏳ <b>Status:</b> Pending admin approval\n\n"
            "Admin has been notified. You'll be notified once approved.\n\n"
            f"📞 <b>Contact admin:</b> {MAIN_ADMIN}",
            parse_mode='HTML'
        )
        
    except Exception as e:
        # Log registration failure
        logger.log_registration(
            user_id=user_id,
            platform="binance",
            status="FAILED",
            error_msg=str(e),
            api_keys_present=True
        )
        
        bot.send_message(
            message.chat.id,
            f"❌ Registration failed. Please try again later.\n\nError: {str(e)}"
        )
        if user_id in registration_data:
            del registration_data[user_id]


# =====================================================
# MT5 REGISTRATION FUNCTIONS
# =====================================================

def get_mt5_login(bot, message):
    """Collect user's MT5 Login ID"""
    user_id = message.from_user.id
    mt5_login = _get_text(message)
    if mt5_login is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_login(bot, msg))
        return
    
    # Validate MT5 login (should be a number)
    if not mt5_login.isdigit() or len(mt5_login) < 5:
        bot.send_message(
            message.chat.id,
            "❌ Invalid MT5 Login ID. Please enter a valid account number:\n\n"
            "<i>(Example: 101047292)</i>",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_login(bot, msg))
        return
    
    # Check if MT5 login is already in use
    login_in_use, existing_user_id = is_mt5_login_in_use(mt5_login)
    if login_in_use and existing_user_id != user_id:
        bot.send_message(
            message.chat.id,
            f"❌ This MT5 Login is already registered.\n\nPlease use a different MT5 account:"
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_login(bot, msg))
        return
    
    registration_data[user_id]['mt5_login'] = int(mt5_login)
    
    bot.send_message(
        message.chat.id,
        f"✅ MT5 Login: {mt5_login}\n\n"
        "🌐 Now, send your <b>MT5 Server</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>How to get your Server:</b>\n\n"
        "In the same place where you got your Login ID,\n"
        "copy the <b>Server</b> name\n\n"
        "<i>(Example: XMGlobal-MT5 5)</i>",
        parse_mode='HTML'
    )
    
    bot.register_next_step_handler(message, lambda msg: get_mt5_server(bot, msg))


def get_mt5_server(bot, message):
    """Collect user's MT5 Server"""
    user_id = message.from_user.id
    mt5_server = _get_text(message)
    if mt5_server is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_server(bot, msg))
        return
    
    if len(mt5_server) < 3:
        bot.send_message(
            message.chat.id,
            "❌ Invalid MT5 Server. Please enter your server name:\n\n"
            "<i>(Example: XMGlobal-MT5 5)</i>",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_server(bot, msg))
        return
    
    registration_data[user_id]['mt5_server'] = mt5_server
    
    bot.send_message(
        message.chat.id,
        f"✅ MT5 Server: {mt5_server}\n\n"
        "🔐 Finally, send your <b>MT5 Password</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 Enter your XM account's trading password\n\n"
        "<i>(This is the password you use to login to MT5)</i>",
        parse_mode='HTML'
    )
    
    bot.register_next_step_handler(message, lambda msg: get_mt5_password(bot, msg))


def get_mt5_password(bot, message):
    """Collect user's MT5 password, provision MetaAPI account, and complete MT5 registration"""
    user_id = message.from_user.id
    mt5_password = _get_text(message)
    if mt5_password is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_password(bot, msg))
        return
    
    if len(mt5_password) < 4:
        bot.send_message(
            message.chat.id,
            "❌ Invalid MT5 Password. Please enter your MT5 password:"
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_password(bot, msg))
        return
    
    registration_data[user_id]['mt5_password'] = mt5_password
    
    # Save to database
    try:
        name = registration_data[user_id]['name']
        mt5_login = registration_data[user_id]['mt5_login']
        mt5_server = registration_data[user_id]['mt5_server']
        
        # Admin provisions account during approval
        metaapi_account_id = None
        
        # Only reach here if no billing error
        add_user_mt5(
            telegram_id=user_id,
            mt5_login=mt5_login,
            mt5_password=mt5_password,
            mt5_server=mt5_server,
            status="pending",
            language="en",
            name=name,
            metaapi_account_id=metaapi_account_id
        )
        
        # Notify admin about new registration
        notify_admin_new_registration(bot, user_id, name, "mt5")
        
        # Clear registration data
        del registration_data[user_id]
        
        provision_note = "\n\n⚠️ <i>Cloud account setup pending — admin will complete setup.</i>"
        
        bot.send_message(
            message.chat.id,
            "✅ <b>MT5 Registration Complete!</b>\n\n"
            "💹 <b>Platform:</b> MT5 Forex/Gold Trading\n"
            f"🆔 <b>Your Telegram ID:</b> <code>{user_id}</code>\n"
            "⏳ <b>Status:</b> Pending admin approval\n\n"
            "Admin has been notified. You'll be notified once approved.\n\n"
            f"📞 <b>Contact admin:</b> {MAIN_ADMIN}" + provision_note,
            parse_mode='HTML'
        )
        
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ Registration failed. Please try again later.\n\nError: {str(e)}"
        )
        if user_id in registration_data:
            del registration_data[user_id]




def cancel_registration(bot, call):
    """Cancel the registration process"""
    user_id = call.from_user.id
    
    if user_id in registration_data:
        del registration_data[user_id]
    
    _safe_answer_callback_query(bot, call.id, "Registration cancelled")
    
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ <b>Registration Cancelled</b>\n\n"
                 "You can start registration again anytime by pressing /start",
            parse_mode='HTML'
        )
    except Exception as e:
        # Message already shows cancel or edit failed
        pass


# =====================================================
# MEXC-ONLY REGISTRATION FUNCTIONS
# =====================================================

def get_mexc_api_key(bot, message):
    """Collect user's MEXC API key."""
    user_id = message.from_user.id
    api_key = _get_text(message)
    if api_key is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key(bot, msg))
        return

    if len(api_key) < 10:
        bot.send_message(message.chat.id, "❌ Invalid MEXC API Key. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key(bot, msg))
        return

    in_use, existing_user_id = is_mexc_api_key_in_use(api_key)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This MEXC API Key is already registered. Use a different key:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key(bot, msg))
        return

    registration_data[user_id]['mexc_api_key'] = api_key

    bot.send_message(
        message.chat.id,
        "✅ MEXC API Key saved\n\n"
        "🔐 Now, send your <b>MEXC API Secret</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Where to find your Secret:</b>\n\n"
        "Your API Secret is shown right after you create the API key on MEXC.\n"
        "This is the <code>api_secret</code> parameter.\n\n"
        "⚠️ <b>Keep it safe!</b> Never share your secret.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret(bot, msg))


def get_mexc_api_secret(bot, message):
    """Collect user's MEXC API secret and complete MEXC registration."""
    user_id = message.from_user.id
    api_secret = _get_text(message)
    if api_secret is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret(bot, msg))
        return

    if len(api_secret) < 10:
        bot.send_message(message.chat.id, "❌ Invalid MEXC API Secret. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret(bot, msg))
        return

    in_use, existing_user_id = is_mexc_api_secret_in_use(api_secret)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This MEXC API Secret is already registered. Use a different one:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret(bot, msg))
        return

    registration_data[user_id]['mexc_api_secret'] = api_secret

    try:
        name = registration_data[user_id]['name']
        mexc_api_key = registration_data[user_id]['mexc_api_key']

        add_user_mexc(
            telegram_id=user_id,
            mexc_api_key=mexc_api_key,
            mexc_api_secret=api_secret,
            status="pending",
            language="en",
            name=name,
        )

        notify_admin_new_registration(bot, user_id, name, "mexc")
        del registration_data[user_id]

        bot.send_message(
            message.chat.id,
            "✅ <b>MEXC Registration Complete!</b>\n\n"
            "🔷 <b>Platform:</b> MEXC Futures Trading\n"
            f"🆔 <b>Your Telegram ID:</b> <code>{user_id}</code>\n"
            "⏳ <b>Status:</b> Pending admin approval\n\n"
            "Admin has been notified. You'll be notified once approved.\n\n"
            f"📞 <b>Contact admin:</b> {MAIN_ADMIN}",
            parse_mode='HTML'
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Registration failed. Please try again.\n\nError: {str(e)}")
        if user_id in registration_data:
            del registration_data[user_id]


# =====================================================
# ALL PLATFORMS REGISTRATION FUNCTIONS
# (Binance → MEXC → MT5)
# =====================================================

def get_api_key_all(bot, message):
    """Step 1/3 — Collect Binance API key for all-platforms registration."""
    user_id = message.from_user.id
    api_key = _get_text(message)
    if api_key is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_api_key_all(bot, msg))
        return

    if len(api_key) < 20:
        bot.send_message(message.chat.id, "❌ Invalid Binance API Key. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_api_key_all(bot, msg))
        return

    in_use, existing_user_id = is_api_key_in_use(api_key)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This Binance API Key is already registered. Use a different key:")
        bot.register_next_step_handler(message, lambda msg: get_api_key_all(bot, msg))
        return

    registration_data[user_id]['api_key'] = api_key
    bot.send_message(
        message.chat.id,
        "✅ Binance API Key saved\n\n"
        "🔐 Now, send your <b>Binance API Secret</b>:",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_api_secret_all(bot, msg))


def get_api_secret_all(bot, message):
    """Step 1/3 cont — Collect Binance API secret then move to MEXC."""
    user_id = message.from_user.id
    api_secret = _get_text(message)
    if api_secret is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_api_secret_all(bot, msg))
        return

    if len(api_secret) < 20:
        bot.send_message(message.chat.id, "❌ Invalid Binance API Secret. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_api_secret_all(bot, msg))
        return

    in_use, existing_user_id = is_api_secret_in_use(api_secret)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This Binance API Secret is already registered. Use a different one:")
        bot.register_next_step_handler(message, lambda msg: get_api_secret_all(bot, msg))
        return

    registration_data[user_id]['api_secret'] = api_secret

    bot.send_message(
        message.chat.id,
        "✅ Binance credentials saved!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 <b>Step 2/3: MEXC Registration</b>\n\n"
        "🔑 Now, send your <b>MEXC API Key</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>How to get your MEXC API Key:</b>\n\n"
        "1️⃣ Go to MEXC → Profile → API Management\n"
        "2️⃣ Create a new API key\n"
        "3️⃣ Enable <b>Futures/Contract Trading</b> permission\n"
        "4️⃣ Copy the <b>API Key</b> value and paste here\n\n"
        "⚠️ <b>Note:</b> This is the <code>api_key</code> parameter",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mexc_api_key_all(bot, msg))


def get_mexc_api_key_all(bot, message):
    """Step 2/3 — Collect MEXC API key for all-platforms registration."""
    user_id = message.from_user.id
    api_key = _get_text(message)
    if api_key is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key_all(bot, msg))
        return

    if len(api_key) < 10:
        bot.send_message(message.chat.id, "❌ Invalid MEXC API Key. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key_all(bot, msg))
        return

    in_use, existing_user_id = is_mexc_api_key_in_use(api_key)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This MEXC API Key is already registered. Use a different key:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_key_all(bot, msg))
        return

    registration_data[user_id]['mexc_api_key'] = api_key
    bot.send_message(
        message.chat.id,
        "✅ MEXC API Key saved\n\n"
        "🔐 Now, send your <b>MEXC API Secret</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Your API Secret is shown after creating the API key on MEXC.\n"
        "This is the <code>api_secret</code> parameter.\n\n"
        "⚠️ <b>Keep it safe!</b> Never share your secret.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret_all(bot, msg))


def get_mexc_api_secret_all(bot, message):
    """Step 2/3 cont — Collect MEXC API secret then move to MT5."""
    user_id = message.from_user.id
    api_secret = _get_text(message)
    if api_secret is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret_all(bot, msg))
        return

    if len(api_secret) < 10:
        bot.send_message(message.chat.id, "❌ Invalid MEXC API Secret. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret_all(bot, msg))
        return

    in_use, existing_user_id = is_mexc_api_secret_in_use(api_secret)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This MEXC API Secret is already registered. Use a different one:")
        bot.register_next_step_handler(message, lambda msg: get_mexc_api_secret_all(bot, msg))
        return

    registration_data[user_id]['mexc_api_secret'] = api_secret

    bot.send_message(
        message.chat.id,
        "✅ MEXC credentials saved!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 <b>Step 3/3: MT5 Registration</b>\n\n"
        "🔢 Now, send your <b>MT5 Login ID</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>How to get your Login ID:</b>\n\n"
        "1️⃣ Click on your balance (top left, near sidebar)\n"
        "2️⃣ Click on <b>Manage</b>\n"
        "3️⃣ Click on <b>Account Information</b>\n"
        "4️⃣ Copy the <b>MT ID</b>",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mt5_login_all(bot, msg))


def get_mt5_login_all(bot, message):
    """Step 3/3 — Collect MT5 login ID for all-platforms registration."""
    user_id = message.from_user.id
    mt5_login = _get_text(message)
    if mt5_login is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_login_all(bot, msg))
        return

    if not mt5_login.isdigit() or len(mt5_login) < 5:
        bot.send_message(
            message.chat.id,
            "❌ Invalid MT5 Login ID. Please enter a valid account number:\n\n"
            "<i>(Example: 101047292)</i>",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_login_all(bot, msg))
        return

    in_use, existing_user_id = is_mt5_login_in_use(mt5_login)
    if in_use and existing_user_id != user_id:
        bot.send_message(message.chat.id, "❌ This MT5 Login is already registered. Use a different account:")
        bot.register_next_step_handler(message, lambda msg: get_mt5_login_all(bot, msg))
        return

    registration_data[user_id]['mt5_login'] = int(mt5_login)
    bot.send_message(
        message.chat.id,
        f"✅ MT5 Login: {mt5_login}\n\n"
        "🌐 Now, send your <b>MT5 Server</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>(Example: XMGlobal-MT5 5)</i>",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mt5_server_all(bot, msg))


def get_mt5_server_all(bot, message):
    """Step 3/3 cont — Collect MT5 server for all-platforms registration."""
    user_id = message.from_user.id
    mt5_server = _get_text(message)
    if mt5_server is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_server_all(bot, msg))
        return

    if len(mt5_server) < 3:
        bot.send_message(
            message.chat.id,
            "❌ Invalid MT5 Server. Please enter your server name:\n\n<i>(Example: XMGlobal-MT5 5)</i>",
            parse_mode='HTML'
        )
        bot.register_next_step_handler(message, lambda msg: get_mt5_server_all(bot, msg))
        return

    registration_data[user_id]['mt5_server'] = mt5_server
    bot.send_message(
        message.chat.id,
        f"✅ MT5 Server: {mt5_server}\n\n"
        "🔐 Finally, send your <b>MT5 Password</b>:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>(The password you use to login to MT5)</i>",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(message, lambda msg: get_mt5_password_all(bot, msg))


def get_mt5_password_all(bot, message):
    """Step 3/3 final — Collect MT5 password and complete all-platforms registration."""
    user_id = message.from_user.id
    mt5_password = _get_text(message)
    if mt5_password is None:
        bot.send_message(message.chat.id, "❌ Please send a text message.")
        bot.register_next_step_handler(message, lambda msg: get_mt5_password_all(bot, msg))
        return

    if len(mt5_password) < 4:
        bot.send_message(message.chat.id, "❌ Invalid MT5 Password. Please try again:")
        bot.register_next_step_handler(message, lambda msg: get_mt5_password_all(bot, msg))
        return

    registration_data[user_id]['mt5_password'] = mt5_password

    try:
        name = registration_data[user_id]['name']
        api_key = registration_data[user_id]['api_key']
        api_secret = registration_data[user_id]['api_secret']
        mexc_api_key = registration_data[user_id]['mexc_api_key']
        mexc_api_secret = registration_data[user_id]['mexc_api_secret']
        mt5_login = registration_data[user_id]['mt5_login']
        mt5_server = registration_data[user_id]['mt5_server']

        # Admin provisions account during approval
        metaapi_account_id = None

        add_user_all_platforms(
            telegram_id=user_id,
            api_key=api_key,
            api_secret=api_secret,
            mt5_login=mt5_login,
            mt5_password=mt5_password,
            mt5_server=mt5_server,
            mexc_api_key=mexc_api_key,
            mexc_api_secret=mexc_api_secret,
            status="pending",
            language="en",
            name=name,
            metaapi_account_id=metaapi_account_id,
        )

        notify_admin_new_registration(bot, user_id, name, "all")
        del registration_data[user_id]

        provision_note = "\n\n⚠️ <i>MT5 cloud setup pending — admin will complete setup.</i>"

        bot.send_message(
            message.chat.id,
            "✅ <b>Registration Complete — All Platforms!</b>\n\n"
            "🌐 <b>Platforms:</b> Binance + MEXC + MT5\n"
            "📈 <b>Binance:</b> Crypto Trading (Spot/Futures)\n"
            "🔷 <b>MEXC:</b> Crypto Futures Trading\n"
            "💹 <b>MT5:</b> Forex/Gold Trading\n"
            f"🆔 <b>Your Telegram ID:</b> <code>{user_id}</code>\n"
            "⏳ <b>Status:</b> Pending admin approval\n\n"
            "Admin has been notified. You can trade on all platforms once approved!\n\n"
            f"📞 <b>Contact admin:</b> {MAIN_ADMIN}" + provision_note,
            parse_mode='HTML'
        )

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Registration failed. Please try again.\n\nError: {str(e)}")
        if user_id in registration_data:
            del registration_data[user_id]

