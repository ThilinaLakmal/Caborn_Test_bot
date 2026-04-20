"""
User settings handler - manage name changes and account deletion
"""
from telebot import types
import telebot.apihelper
from user_control.add_users import (
    get_user_by_telegram_id,
    delete_user,
    is_admin
)
from firebase_admin import firestore

# Get Firestore client
db = firestore.client()


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


def show_user_settings(bot, call):
    """Display user settings menu"""
    user_id = call.from_user.id
    
    # Get user data
    user_data, _ = get_user_by_telegram_id(user_id)
    
    if not user_data:
        _safe_answer_callback_query(bot, call.id, "❌ User not found", show_alert=True)
        return
    
    _safe_answer_callback_query(bot, call.id)
    
    user_name = user_data.get('name', 'N/A')
    platform = user_data.get('platform', 'binance')
    status = user_data.get('status', 'unknown')
    
    # Platform display
    if platform == "binance":
        platform_emoji = "📈"
        platform_name = "Binance"
    elif platform == "mt5":
        platform_emoji = "💹"
        platform_name = "MT5"
    else:
        platform_emoji = "🌐"
        platform_name = "Both Platforms"
    
    # Status display
    status_emoji = "✅" if status == "active" else ("⏳" if status == "pending" else "❌")
    
    message = (
        "⚙️ <b>USER SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>Name:</b> {user_name}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n"
        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
        f"{status_emoji} <b>Status:</b> {status.title()}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>What would you like to do?</b>"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("✏️ Change Name", callback_data="settings_change_name"),
        types.InlineKeyboardButton("🗑️ Delete Account", callback_data="settings_delete_account"),
        types.InlineKeyboardButton("🔙 Back to Dashboard", callback_data="user_dashboard")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=message,
        parse_mode='HTML',
        reply_markup=markup
    )


def prompt_change_name(bot, call):
    """Prompt user to enter new name"""
    user_id = call.from_user.id
    
    _safe_answer_callback_query(bot, call.id)
    
    message = (
        "✏️ <b>CHANGE NAME</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please send your <b>new name</b>:\n\n"
        "<i>Enter your full name (minimum 2 characters)</i>"
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="user_settings"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=message,
        parse_mode='HTML',
        reply_markup=markup
    )
    
    # Register next step handler to receive the new name
    bot.register_next_step_handler(call.message, process_change_name, bot)


def process_change_name(message, bot):
    """Process name change request"""
    user_id = message.from_user.id
    new_name = message.text.strip()
    
    # Validate name
    if len(new_name) < 2:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="user_settings"))
        
        bot.send_message(
            message.chat.id,
            "❌ <b>Invalid Name</b>\n\n"
            "Name must be at least 2 characters long.\n"
            "Please try again.",
            parse_mode='HTML',
            reply_markup=markup
        )
        return
    
    # Check if name is too long
    if len(new_name) > 50:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="user_settings"))
        
        bot.send_message(
            message.chat.id,
            "❌ <b>Name Too Long</b>\n\n"
            "Name must be less than 50 characters.\n"
            "Please try again.",
            parse_mode='HTML',
            reply_markup=markup
        )
        return
    
    # Update name in database
    try:
        doc_ref = db.collection("users").document(str(user_id))
        doc_ref.update({"name": new_name})
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="user_settings"))
        
        bot.send_message(
            message.chat.id,
            f"✅ <b>Name Updated Successfully!</b>\n\n"
            f"Your new name: <b>{new_name}</b>",
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="user_settings"))
        
        bot.send_message(
            message.chat.id,
            f"❌ <b>Error Updating Name</b>\n\n{str(e)}",
            parse_mode='HTML',
            reply_markup=markup
        )


def confirm_delete_account(bot, call):
    """Ask user to confirm account deletion"""
    user_id = call.from_user.id
    
    _safe_answer_callback_query(bot, call.id)
    
    message = (
        "🗑️ <b>DELETE ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>WARNING: This action cannot be undone!</b>\n\n"
        "Deleting your account will:\n"
        "• Remove all your data from our database\n"
        "• Cancel any active trading sessions\n"
        "• Revoke your access to the bot\n\n"
        "Are you sure you want to delete your account?"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("❌ Yes, Delete", callback_data="settings_confirm_delete"),
        types.InlineKeyboardButton("🔙 Cancel", callback_data="user_settings")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=message,
        parse_mode='HTML',
        reply_markup=markup
    )


def process_delete_account(bot, call):
    """Process account deletion"""
    user_id = call.from_user.id

    # Acknowledge the callback immediately before running slow cleanup work.
    # Telegram callback queries expire quickly and account deletion may take
    # long enough (stop tasks, remove MetaAPI account, delete DB record) to
    # exceed that window.
    _safe_answer_callback_query(bot, call.id, "⏳ Deleting account...", show_alert=False)
    
    # Get user data for confirmation message
    user_data, _ = get_user_by_telegram_id(user_id)
    
    if not user_data:
        _safe_answer_callback_query(bot, call.id, "❌ User not found", show_alert=True)
        return
    
    user_name = user_data.get('name', 'User')
    
    # Stop any active trading sessions first
    try:
        from handlers.trading_handler import is_trading_active, user_data as trading_user_data
        from handlers.mt5_handler import is_mt5_trading_active, mt5_user_data
        from utils.bg_loop import loop
        import asyncio
        
        username_key = f"user_{user_id}"
        
        # Stop Binance trading if active
        if is_trading_active(username_key):
            from handlers.trading_handler import stop_futures_trading
            future = asyncio.run_coroutine_threadsafe(
                stop_futures_trading(username_key), loop
            )
            future.result(timeout=5)
        
        # Stop MT5 trading if active
        if is_mt5_trading_active(username_key):
            from handlers.mt5_handler import stop_mt5_trading
            future = asyncio.run_coroutine_threadsafe(
                stop_mt5_trading(username_key), loop
            )
            future.result(timeout=5)
    except Exception as e:
        print(f"[DELETE-ACCOUNT] Error stopping trading: {e}")

    # Remove MetaAPI cloud account if user has one
    metaapi_account_id = user_data.get('metaapi_account_id')
    platform = user_data.get('platform', 'binance')
    if metaapi_account_id and platform in ('mt5', 'all'):
        try:
            from mt5.metaapi_manager import remove_account
            from utils.bg_loop import loop
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                remove_account(metaapi_account_id), loop
            )
            future.result(timeout=30)
            print(f"[DELETE-ACCOUNT] MetaAPI account {metaapi_account_id} removed for user {user_id}")
        except Exception as e:
            print(f"[DELETE-ACCOUNT] Error removing MetaAPI account: {e}")
    
    # Delete user from database
    success = delete_user(user_id)
    
    if success:
        success_message = (
            "✅ <b>Account Deleted Successfully</b>\n\n"
            f"Goodbye, {user_name}! 👋\n\n"
            "Your account and all associated data have been removed.\n\n"
            "If you change your mind, you can register again by pressing /start"
        )
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=success_message,
                parse_mode='HTML'
            )
        except Exception as e:
            # If message can't be edited (already modified, expired, or identical),
            # send a new message instead
            if "message is not modified" in str(e) or "message to edit not found" in str(e):
                bot.send_message(
                    call.message.chat.id,
                    success_message,
                    parse_mode='HTML'
                )
            else:
                raise
    else:
        error_message = (
            "❌ <b>Error Deleting Account</b>\n\n"
            "Something went wrong. Please try again later or contact support."
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="user_settings"))
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=error_message,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            # If message can't be edited, send a new message instead
            if "message is not modified" in str(e) or "message to edit not found" in str(e):
                bot.send_message(
                    call.message.chat.id,
                    error_message,
                    parse_mode='HTML',
                    reply_markup=markup
                )
            else:
                raise
