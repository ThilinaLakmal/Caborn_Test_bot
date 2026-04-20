"""
Callback query handler for inline button interactions
"""
from handlers.registration_handler import (
    start_registration,
    handle_platform_selection,
    cancel_registration
)
from handlers.admin_handler import (
    show_pending_approvals,
    approve_user_registration,
    reject_user_registration,
    back_to_admin_dashboard,
    show_manage_users,
    show_all_users,
    prompt_delete_user,
    view_pending_user_details
)
from handlers.user_settings_handler import (
    show_user_settings,
    prompt_change_name,
    confirm_delete_account,
    process_delete_account
)
from handlers.trading_handler import (
    initialize_user_session,
    start_futures_trading,
    stop_futures_trading,
    get_user_balance,
    get_trading_status,
    get_detailed_trading_status,
    is_trading_active,
    get_trading_mode,
    MAX_CONCURRENT_TRADES
)
from handlers.mt5_handler import (
    initialize_mt5_session,
    start_mt5_trading,
    stop_mt5_trading,
    get_mt5_balance,
    get_mt5_trading_status,
    get_detailed_mt5_status,
    is_mt5_trading_active,
    get_mt5_trading_mode,
    MAX_CONCURRENT_TRADES as MT5_MAX_TRADES,
)
from handlers.mexc_handler import (
    initialize_mexc_session,
    start_mexc_trading,
    stop_mexc_trading,
    get_mexc_user_balance,
    get_mexc_trading_status,
    get_detailed_mexc_status,
    is_mexc_trading_active,
    get_mexc_trading_mode,
    MAX_CONCURRENT_TRADES as MEXC_MAX_TRADES,
)
from telebot import types
import telebot.apihelper
from user_control.add_users import get_user_by_telegram_id, get_user_platform, load_MEXC_credentials
from config import FUTURES_SYMBOLS_LIST as symbols_list, MEXC_FUTURES_SYMBOLS_LIST as mexc_symbols_list, MIN_BALANCE, MEXC_MIN_BALANCE


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


def _safe_edit_message_text(bot, chat_id, message_id, text, parse_mode='HTML', reply_markup=None):
    """
    Safely edit a message without crashing if content is identical or message expired.
    If message can't be edited, sends a new message instead.
    
    Args:
        bot: Telebot instance
        chat_id: Chat ID
        message_id: Message ID to edit
        text: New message text
        parse_mode: Parse mode (default: HTML)
        reply_markup: Inline keyboard markup
    
    Returns:
        None (silently handles errors)
    """
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except telebot.apihelper.ApiTelegramException as e:
        error_str = str(e).lower()
        
        # Message not modified (content is identical) - ignore
        if "message is not modified" in error_str:
            pass  # Already has same content, no action needed
        
        # Message expired or not found - send new message instead
        elif "message to edit not found" in error_str or "message can't be edited" in error_str:
            try:
                bot.send_message(
                    chat_id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
            except Exception as send_err:
                print(f"[ERROR] Could not send fallback message: {send_err}")
        else:
            # Re-raise other errors for debugging
            raise
    except Exception as e:
        print(f"[ERROR] Error editing message: {e}")


_processing_callbacks = set()

def handle_callback_query(bot, call):
    """
    Handle all callback queries from inline buttons
    with anti-spam and debouncing protection.
    """
    user_id = call.from_user.id
    
    if user_id in _processing_callbacks:
        bot.answer_callback_query(call.id, "⏳ Processing your request, please wait...", show_alert=False)
        return
        
    _processing_callbacks.add(user_id)
    try:
        return _handle_callback_query_impl(bot, call)
    finally:
        _processing_callbacks.discard(user_id)


def _handle_callback_query_impl(bot, call):
    """
    Actual implementation of callback query handling
    """
    callback_data = call.data
    user_id = call.from_user.id
    
    # Registration callbacks
    if callback_data == "user_register":
        start_registration(bot, call)
        return
    
    # Platform selection during registration
    if callback_data == "reg_platform_binance":
        handle_platform_selection(bot, call, "binance")
        return

    if callback_data == "reg_platform_mt5":
        handle_platform_selection(bot, call, "mt5")
        return

    if callback_data == "reg_platform_mexc":
        handle_platform_selection(bot, call, "mexc")
        return

    if callback_data == "reg_platform_all":
        handle_platform_selection(bot, call, "all")
        return

    
    if callback_data == "cancel_registration":
        cancel_registration(bot, call)
        return
    
    # Admin - Pending approvals
    if callback_data == "admin_pending":
        show_pending_approvals(bot, call)
        return
    
    # Admin - Manage users
    if callback_data == "admin_users":
        show_manage_users(bot, call)
        return
    
    # Admin - View all users
    if callback_data == "admin_view_all_users":
        show_all_users(bot, call)
        return
    
    # Admin - Delete user prompt
    if callback_data == "admin_delete_user":
        prompt_delete_user(bot, call)
        return
    
    # Admin - Back to dashboard
    if callback_data == "back_to_dashboard":
        back_to_admin_dashboard(bot, call)
        return
    
    # Approve user
    if callback_data.startswith("approve_"):
        telegram_id = int(callback_data.replace("approve_", ""))
        approve_user_registration(bot, call, telegram_id)
        return
    
    # Reject user
    if callback_data.startswith("reject_"):
        telegram_id = int(callback_data.replace("reject_", ""))
        reject_user_registration(bot, call, telegram_id)
        return
    
    # View pending user details
    if callback_data.startswith("view_pending_"):
        telegram_id = int(callback_data.replace("view_pending_", ""))
        view_pending_user_details(bot, call, telegram_id)
        return
    
    # ==========================================
    # TRADING CALLBACKS - PLATFORM AWARE
    # ==========================================
    
    # Start Trading - Show trading mode selection based on user's platform
    if callback_data == "start_trading":
        try:
            # Check if trading is already active
            username_key = f"user_{user_id}"
            
            if is_trading_active(username_key) or is_mt5_trading_active(username_key) or is_mexc_trading_active(username_key):
                _safe_answer_callback_query(call.id, "⚠️ Trading is already running!", show_alert=True)
                return
            
            # Get user data from database
            user_data, _ = get_user_by_telegram_id(user_id)
            
            if not user_data:
                _safe_answer_callback_query(call.id, "❌ User not found. Please register first.", show_alert=True)
                return
            
            if user_data.get('status') != 'active':
                _safe_answer_callback_query(call.id, "⏳ Your account is pending admin approval.", show_alert=True)
                return
            
            user_platform = user_data.get('platform', 'binance')
            user_name = user_data.get('name', call.from_user.first_name or 'Trader')
            
            # Show trading mode selection with platform-specific options
            keyboard = types.InlineKeyboardMarkup()
            
            if user_platform == "binance":
                # Binance only users
                keyboard.row(
                    types.InlineKeyboardButton("📊 Spot Trading", callback_data="trade_mode_spot"),
                    types.InlineKeyboardButton("🚀 Futures Trading", callback_data="trade_mode_future")
                )
                platform_emoji = "📈"
                platform_name = "Binance Crypto"
                available_modes = "<b>Available Modes:</b> Spot & Futures Trading"
            elif user_platform == "mt5":
                # MT5 only users
                keyboard.row(
                    types.InlineKeyboardButton("💹 MT5 Forex/Gold", callback_data="trade_mode_mt5")
                )
                platform_emoji = "💹"
                platform_name = "MT5 Forex/Gold"
                available_modes = "<b>Available Modes:</b> MT5 Forex/Gold Trading"
            elif user_platform == "mexc":
                # MEXC only users
                keyboard.row(
                    types.InlineKeyboardButton("🔷 MEXC Futures", callback_data="trade_mode_mexc")
                )
                platform_emoji = "🔷"
                platform_name = "MEXC Futures"
                available_modes = "<b>Available Modes:</b> MEXC Futures Trading"
            else:  # all platforms
                # Binance + MEXC + MT5
                keyboard.row(
                    types.InlineKeyboardButton("📊 Spot Trading", callback_data="trade_mode_spot"),
                    types.InlineKeyboardButton("🚀 Binance Futures", callback_data="trade_mode_future")
                )
                keyboard.row(
                    types.InlineKeyboardButton("🔷 MEXC Futures", callback_data="trade_mode_mexc"),
                    types.InlineKeyboardButton("💹 MT5 Forex/Gold", callback_data="trade_mode_mt5")
                )
                platform_emoji = "🌐"
                platform_name = "Binance + MEXC + MT5"
                available_modes = "<b>Available Modes:</b> Spot, Binance Futures, MEXC Futures & MT5"
            
            keyboard.row(
                types.InlineKeyboardButton("📊 View Balance", callback_data="view_balance"),
                types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
            )
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"<b>✅ READY TO TRADE ✅</b>\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                     f"👤 <b>Welcome:</b> {user_name}\n"
                     f"{platform_emoji} <b>Registered Platform:</b> {platform_name}\n"
                     f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
                     f"━━━━━━━━━━━━━━━━━━━━━━\n"
                     f"{available_modes}\n\n"
                     f"📊 <b>Select Trading Mode:</b>",
                parse_mode='HTML',
                reply_markup=keyboard
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # Spot Trading (Coming Soon)
    if callback_data == "trade_mode_spot":
        # Check user platform
        user_platform = get_user_platform(user_id)

        if user_platform not in ["binance", "all"]:
            _safe_answer_callback_query(
                bot,
                call.id,
                "❌ You are not registered for Binance trading.",
                show_alert=True
            )
            return

        bot.answer_callback_query(call.id, "🔜 Spot Trading coming soon!", show_alert=True)
        return
    
    # Start Future Trading - Platform validation
    if callback_data == "trade_mode_future":
        try:
            # Check user platform first
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'binance') if user_data else None

            if user_platform not in ["binance", "all"]:
                _safe_answer_callback_query(
                    bot,
                    call.id,
                    "❌ You are not registered for Binance trading.",
                    show_alert=True
                )
                return

            username_key = f"user_{user_id}"
            api_key = user_data.get('api_key')
            api_secret = user_data.get('api_secret')
            
            if not api_key or not api_secret:
                _safe_answer_callback_query(call.id, "❌ API keys not found. Please complete registration.", show_alert=True)
                return
            
            # Initialize session
            initialize_user_session(username_key, user_id, api_key, api_secret, symbols_list)
            
            # Check minimum balance before starting - get detailed error info
            from handlers.trading_handler import get_user_balance_with_error_info
            result = get_user_balance_with_error_info(username_key)
            balance = result.get('balance')
            error_info = result.get('error')
            
            if error_info:
                # API error occurred - show user-friendly message
                error_msg = error_info.get('user_message', '')
                solution = error_info.get('solution', '')
                
                # Send detailed error message
                full_message = error_msg + "\n\n" + solution
                bot.send_message(
                    call.message.chat.id,
                    full_message,
                    parse_mode='HTML'
                )
                return
            
            if balance is None:
                bot.answer_callback_query(call.id, "❌ Unable to fetch balance. Try again later.", show_alert=True)
                return
            
            if balance < MIN_BALANCE:
                _safe_answer_callback_query(
                    bot,
                    call.id, 
                    f"❌ Insufficient balance!\n\nMinimum required: ${MIN_BALANCE:.2f}\nYour balance: ${balance:.2f}", 
                    show_alert=True
                )
                return
            
            # Start the trading loop on background thread
            from utils.bg_loop import loop
            import asyncio
            import concurrent.futures
            import threading
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="<b>⏳ Starting Binance Futures Trading...</b>\n\nStarting background scans. Please wait...",
                parse_mode='HTML'
            )
            
            def bg_start_future():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        start_futures_trading(bot, user_id, username_key), loop
                    )
                    success = future.result()  # Wait indefinitely, no timeout limit
                    
                    if success:
                        keyboard = types.InlineKeyboardMarkup()
                        keyboard.row(
                            types.InlineKeyboardButton("🛑 Stop Trading", callback_data="stop_trading"),
                            types.InlineKeyboardButton("📊 View Status", callback_data="view_status")
                        )
                        keyboard.row(
                            types.InlineKeyboardButton("💰 Balance", callback_data="view_balance"),
                            types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                        )
                        
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>🚀 FUTURE TRADING STARTED 🚀</b>\n"
                                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                 "✅ <b>Trading Mode:</b> Future Trading\n"
                                 "✅ <b>Status:</b> <u>Running</u>\n"
                                 "✅ <b>Monitoring:</b> 100+ coins\n\n"
                                 "<i>The bot is now actively monitoring the market and will execute trades automatically.</i>",
                            parse_mode='HTML',
                            reply_markup=keyboard
                        )
                    else:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>❌ Failed to start trading</b>",
                            parse_mode='HTML'
                        )
                except Exception as e:
                    try:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text=f"<b>❌ Start Failed</b>\n\n<code>{str(e)[:100]}</code>",
                            parse_mode='HTML'
                        )
                    except:
                        pass
                        
            threading.Thread(target=bg_start_future, daemon=True).start()
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # Stop Trading (handles Binance, MT5, MEXC, and all combinations)
    if callback_data == "stop_trading":
        try:
            username_key = f"user_{user_id}"
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'binance') if user_data else 'binance'

            from utils.bg_loop import loop
            import asyncio
            import concurrent.futures

            import threading
            
            bot.answer_callback_query(call.id, "🛑 Stopping Trading... Please wait.")
            
            def bg_stop_trading():
                try:
                    stopped_platforms = []
                    
                    def _stop_with_timeout(coro, platform_name):
                        fut = asyncio.run_coroutine_threadsafe(coro, loop)
                        try:
                            if fut.result(): # Wait without timeout
                                stopped_platforms.append(platform_name)
                        except Exception as e:
                            print(f"[STOP] ⚠️ {platform_name} stop error: {e}")

                    if user_platform == "binance":
                        if is_trading_active(username_key):
                            _stop_with_timeout(stop_futures_trading(username_key), "Binance")
                    elif user_platform == "mt5":
                        if is_mt5_trading_active(username_key):
                            _stop_with_timeout(stop_mt5_trading(username_key), "MT5")
                    elif user_platform == "mexc":
                        if is_mexc_trading_active(username_key):
                            _stop_with_timeout(stop_mexc_trading(username_key), "MEXC")
                    else:  # all
                        if is_trading_active(username_key):
                            _stop_with_timeout(stop_futures_trading(username_key), "Binance")
                        if is_mt5_trading_active(username_key):
                            _stop_with_timeout(stop_mt5_trading(username_key), "MT5")
                        if is_mexc_trading_active(username_key):
                            _stop_with_timeout(stop_mexc_trading(username_key), "MEXC")

                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.row(
                        types.InlineKeyboardButton("🚀 Restart Trading", callback_data="start_trading"),
                        types.InlineKeyboardButton("📊 View Balance", callback_data="view_balance")
                    )
                    keyboard.row(
                        types.InlineKeyboardButton("🔙 Main Menu", callback_data="user_dashboard")
                    )

                    if stopped_platforms:
                        platform_list = " & ".join(stopped_platforms)
                        msg = (
                            f"<b>🛑 {platform_list} TRADING STOPPED 🛑</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            "✅ All open positions closed\n"
                            "✅ All trading activities stopped\n"
                            "✅ No new trades will be opened\n\n"
                            "<i>You can restart anytime.</i>"
                        )
                    else:
                        msg = (
                            "<b>🛑 TRADING STOPPED 🛑</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            "✅ No active trading to stop.\n\n"
                            "<i>You can start trading anytime.</i>"
                        )

                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=msg,
                        parse_mode='HTML',
                        reply_markup=keyboard
                    )
                except Exception as e:
                    print(f"[STOP] Threaded stop error: {e}")
                    
            threading.Thread(target=bg_stop_trading, daemon=True).start()
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return

    # Start MEXC Futures Trading
    if callback_data == "trade_mode_mexc":
        try:
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'mexc') if user_data else None

            if user_platform not in ["mexc", "all"]:
                _safe_answer_callback_query(
                    bot,
                    call.id,
                    "❌ You are not registered for MEXC trading.",
                    show_alert=True
                )
                return

            username_key = f"user_{user_id}"
            mexc_api_key = user_data.get('mexc_api_key')
            mexc_api_secret = user_data.get('mexc_api_secret')

            if not mexc_api_key or not mexc_api_secret:
                bot.answer_callback_query(call.id, "❌ MEXC API keys not found. Please re-register.", show_alert=True)
                return

            initialize_mexc_session(username_key, user_id, mexc_api_key, mexc_api_secret, mexc_symbols_list)

            # Check balance with detailed error handling
            from handlers.mexc_handler import get_mexc_user_balance_with_error_info
            result = get_mexc_user_balance_with_error_info(username_key)
            balance = result.get('balance')
            error_info = result.get('error')
            
            if error_info:
                # API error occurred - show user-friendly message
                error_msg = error_info.get('user_message', 'Could not fetch MEXC balance')
                solution = error_info.get('solution', '')
                
                # Send detailed error message
                full_message = error_msg + "\n\n" + solution if solution else error_msg
                bot.send_message(
                    call.message.chat.id,
                    full_message,
                    parse_mode='HTML'
                )
                return
            
            if balance is None:
                bot.answer_callback_query(call.id, "❌ Unable to fetch MEXC balance. Try again.", show_alert=True)
                return

            if balance < MEXC_MIN_BALANCE:
                _safe_answer_callback_query(
                    bot,
                    call.id,
                    f"❌ Insufficient MEXC balance!\n\nMinimum: ${MEXC_MIN_BALANCE:.2f}\nYour balance: ${balance:.2f}",
                    show_alert=True
                )
                return

            from utils.bg_loop import loop
            import asyncio
            import concurrent.futures
            import threading
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="<b>⏳ Starting MEXC Futures Trading...</b>\n\nStarting background scans. Please wait...",
                parse_mode='HTML'
            )
            
            def bg_start_mexc():
                try:
                    fut = asyncio.run_coroutine_threadsafe(start_mexc_trading(bot, user_id, username_key), loop)
                    success = fut.result()
                    
                    if success:
                        keyboard = types.InlineKeyboardMarkup()
                        keyboard.row(
                            types.InlineKeyboardButton("🛑 Stop Trading", callback_data="stop_trading"),
                            types.InlineKeyboardButton("📊 View Status", callback_data="view_status")
                        )
                        keyboard.row(
                            types.InlineKeyboardButton("💰 Balance", callback_data="view_balance"),
                            types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                        )
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>🚀 MEXC FUTURES TRADING STARTED 🚀</b>\n"
                                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                 "✅ <b>Trading Mode:</b> MEXC Futures\n"
                                 "✅ <b>Status:</b> <u>Running</u>\n"
                                 f"✅ <b>Balance:</b> <code>{balance:.2f} USDT</code>\n\n"
                                 "<i>The bot is now actively monitoring MEXC markets and will execute trades automatically.</i>",
                            parse_mode='HTML',
                            reply_markup=keyboard
                        )
                    else:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>❌ Failed to start MEXC trading</b>",
                            parse_mode='HTML'
                        )
                except Exception as e:
                    try:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text=f"<b>❌ Start Failed</b>\n\n<code>{str(e)[:100]}</code>",
                            parse_mode='HTML'
                        )
                    except:
                        pass
                        
            threading.Thread(target=bg_start_mexc, daemon=True).start()
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # View Balance (platform-aware)
    if callback_data == "view_balance":
        try:
            username_key = f"user_{user_id}"
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'binance') if user_data else 'binance'
            
            # Get balance based on platform
            if user_platform == "binance":
                balance = get_user_balance(username_key)
                currency = "USDT"
                platform_emoji = "📈"
                platform_name = "Binance"
                
                if balance is not None:
                    balance_text = (
                        f"<b>💼 {platform_name} BALANCE 💼</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
                        f"💵 <b>Available:</b> <code>{balance:.2f} {currency}</code>\n"
                        f"📊 <b>Total:</b> <code>{balance:.2f} {currency}</code>"
                    )
                else:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch balance. Start trading first.", show_alert=True)
                    return
            elif user_platform == "mt5":
                balance = get_mt5_balance(user_id)
                currency = "USD"
                platform_emoji = "💹"
                platform_name = "MT5"
                
                if balance is not None:
                    balance_text = (
                        f"<b>💼 {platform_name} BALANCE 💼</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
                        f"💵 <b>Available:</b> <code>{balance:.2f} {currency}</code>\n"
                        f"📊 <b>Total:</b> <code>{balance:.2f} {currency}</code>"
                    )
                else:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch balance. Start trading first.", show_alert=True)
                    return
            elif user_platform == "mexc":
                balance = get_mexc_user_balance(username_key)
                currency = "USDT"
                platform_emoji = "🔷"
                platform_name = "MEXC Futures"
                
                if balance is not None:
                    balance_text = (
                        f"<b>💼 {platform_name} BALANCE 💼</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
                        f"💵 <b>Available:</b> <code>{balance:.2f} {currency}</code>\n"
                        f"📊 <b>Total:</b> <code>{balance:.2f} {currency}</code>"
                    )
                else:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch MEXC balance. Start trading first.", show_alert=True)
                    return
            elif user_platform == "all":
                binance_balance = get_user_balance(username_key)
                mexc_balance = get_mexc_user_balance(username_key)
                mt5_balance = get_mt5_balance(user_id)
                
                balance_text = "<b>💼 ALL PLATFORMS BALANCES 💼</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                
                if binance_balance is not None:
                    balance_text += f"📈 <b>Binance Futures:</b>\n"
                    balance_text += f"   💵 <code>{binance_balance:.2f} USDT</code>\n\n"
                else:
                    balance_text += f"📈 <b>Binance Futures:</b> Not initialized\n\n"
                
                if mexc_balance is not None:
                    balance_text += f"🔷 <b>MEXC Futures:</b>\n"
                    balance_text += f"   💵 <code>{mexc_balance:.2f} USDT</code>\n\n"
                else:
                    balance_text += f"🔷 <b>MEXC Futures:</b> Not initialized\n\n"
                
                if mt5_balance is not None:
                    balance_text += f"💹 <b>MT5 Forex:</b>\n"
                    balance_text += f"   💵 <code>{mt5_balance:.2f} USD</code>\n\n"
                else:
                    balance_text += f"💹 <b>MT5 Forex:</b> Not initialized\n\n"
                
                if binance_balance is None and mexc_balance is None and mt5_balance is None:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch balances. Start trading first.", show_alert=True)
                    return
            else:  # fallback to binance
                balance = get_user_balance(username_key)
                currency = "USDT"
                platform_emoji = "📈"
                platform_name = "Binance"
                
                if balance is not None:
                    balance_text = (
                        f"<b>💼 {platform_name} BALANCE 💼</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
                        f"💵 <b>Available:</b> <code>{balance:.2f} {currency}</code>\n"
                        f"📊 <b>Total:</b> <code>{balance:.2f} {currency}</code>"
                    )
                else:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch balance. Start trading first.", show_alert=True)
                    return
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.row(
                types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
            )
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=balance_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )
            
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # View Trading Status (platform-aware) - Detailed P&L Report
    if callback_data == "view_status":
        try:
            username_key = f"user_{user_id}"
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'binance') if user_data else 'binance'
            
            # Get detailed status based on platform
            if user_platform == "binance":
                status = get_detailed_trading_status(username_key)
                max_trades = MAX_CONCURRENT_TRADES
                currency = "USDT"
                platform_emoji = "📈"
                platform_name = "Binance Futures"
            elif user_platform == "mt5":
                status = get_detailed_mt5_status(username_key)
                max_trades = MT5_MAX_TRADES
                currency = "USD"
                platform_emoji = "💹"
                platform_name = "MT5 Forex"
            elif user_platform == "mexc":
                status = get_detailed_mexc_status(username_key)
                max_trades = MEXC_MAX_TRADES
                currency = "USDT"
                platform_emoji = "🔷"
                platform_name = "MEXC Futures"
            elif user_platform == "all":
                # Show the most active platform's status; prefer the one currently running
                if is_mexc_trading_active(username_key):
                    status = get_detailed_mexc_status(username_key)
                    max_trades = MEXC_MAX_TRADES
                    currency = "USDT"
                    platform_emoji = "🔷"
                    platform_name = "MEXC Futures (All Platforms)"
                elif is_trading_active(username_key):
                    status = get_detailed_trading_status(username_key)
                    max_trades = MAX_CONCURRENT_TRADES
                    currency = "USDT"
                    platform_emoji = "📈"
                    platform_name = "Binance Futures (All Platforms)"
                else:
                    status = get_detailed_mt5_status(username_key)
                    max_trades = MT5_MAX_TRADES
                    currency = "USD"
                    platform_emoji = "💹"
                    platform_name = "MT5 Forex (All Platforms)"
            else:
                # Fallback to binance
                status = get_detailed_trading_status(username_key)
                max_trades = MAX_CONCURRENT_TRADES
                currency = "USDT"
                platform_emoji = "📈"
                platform_name = "Binance Futures"
            
            if status:
                keyboard = types.InlineKeyboardMarkup()
                keyboard.row(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="view_status"),
                    types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                )
                
                # Basic info
                bot_status = status.get('status') or 'Unknown'
                trading_mode = status.get('trading_mode') or 'Not Set'
                active_trades = status.get('active_trades', 0)
                balance = status.get('balance', 0)
                unrealized_pnl = status.get('unrealized_pnl', 0)
                pnl_percentage = status.get('pnl_percentage', 0)
                positions = status.get('positions', [])
                
                # Format values
                balance_text = f"{balance:.2f}" if balance is not None else "N/A"
                pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                pnl_sign = "+" if unrealized_pnl >= 0 else ""
                
                # Build message
                message = (
                    f"<b>📊 {platform_name} DETAILED REPORT 📊</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
                    f"🔄 <b>Status:</b> <u>{bot_status}</u>\n"
                    f"📈 <b>Mode:</b> {trading_mode}\n\n"
                    
                    "<b>💰 ACCOUNT SUMMARY</b>\n"
                    "─────────────────────\n"
                    f"💵 <b>Balance:</b> <code>{balance_text} {currency}</code>\n"
                )
                
                # Add platform-specific details
                if user_platform in ["binance", "all"]:
                    margin_balance = status.get('margin_balance', 0)
                    available_balance = status.get('available_balance', 0)
                    
                    # Get cross wallet balance and total initial margin if available
                    cross_wallet_balance = status.get('cross_wallet_balance', margin_balance)
                    total_initial_margin = status.get('total_initial_margin', 0)
                    total_maint_margin = status.get('total_maint_margin', 0)
                    
                    message += (
                        f"📊 <b>Margin Balance:</b> <code>{margin_balance:.2f} {currency}</code>\n"
                        f"💎 <b>Available:</b> <code>{available_balance:.2f} {currency}</code>\n"
                        f"🔒 <b>Initial Margin:</b> <code>{total_initial_margin:.2f} {currency}</code>\n"
                        f"⚠️ <b>Maint. Margin:</b> <code>{total_maint_margin:.2f} {currency}</code>\n"
                    )
                    
                    # Calculate margin level/health
                    if total_maint_margin > 0:
                        margin_ratio = (margin_balance / total_maint_margin) * 100
                        margin_health = "🟢 Healthy" if margin_ratio > 150 else ("🟡 Moderate" if margin_ratio > 110 else "🔴 Warning")
                        message += f"📈 <b>Margin Level:</b> {margin_health} <code>({margin_ratio:.1f}%)</code>\n"
                    
                elif user_platform == "mt5":
                    equity = status.get('equity', 0)
                    margin = status.get('margin', 0)
                    free_margin = status.get('free_margin', 0)
                    leverage = status.get('leverage', 0)
                    message += (
                        f"📊 <b>Equity:</b> <code>{equity:.2f} {currency}</code>\n"
                        f"🔒 <b>Margin Used:</b> <code>{margin:.2f} {currency}</code>\n"
                        f"💎 <b>Free Margin:</b> <code>{free_margin:.2f} {currency}</code>\n"
                        f"⚡ <b>Leverage:</b> 1:{leverage}\n"
                    )
                elif user_platform in ["mexc", "all"]:
                    available_balance = status.get('available_balance', balance)
                    total_initial_margin = status.get('total_initial_margin', 0)
                    message += (
                        f"💎 <b>Available:</b> <code>{available_balance:.2f} {currency}</code>\n"
                        f"🔒 <b>Margin Used:</b> <code>{total_initial_margin:.2f} {currency}</code>\n"
                    )
                
                # Add P&L summary
                message += (
                    f"\n{pnl_emoji} <b>Unrealized P&L:</b> <code>{pnl_sign}{unrealized_pnl:.2f} {currency}</code>\n"
                    f"📈 <b>P&L %:</b> <code>{pnl_sign}{pnl_percentage:.2f}%</code>\n"
                )
                
                # Add positions details
                message += (
                    f"\n<b>📊 OPEN POSITIONS ({active_trades}/{max_trades})</b>\n"
                    "─────────────────────\n"
                )
                
                if positions:
                    for pos in positions[:5]:  # Show max 5 positions
                        symbol = pos.get('symbol', 'Unknown')
                        side = pos.get('side', '?')
                        pos_pnl = pos.get('profit', pos.get('unrealized_pnl', 0))
                        pos_pnl_pct = pos.get('pnl_percentage', 0)
                        entry = pos.get('entry_price', 0)
                        current = pos.get('current_price', pos.get('mark_price', 0))
                        
                        pos_emoji = "🟢" if pos_pnl >= 0 else "🔴"
                        side_emoji = "📈" if side in ["BUY", "LONG"] else "📉"
                        pnl_sign_pos = "+" if pos_pnl >= 0 else ""
                        
                        # Platform-specific details
                        if user_platform in ["binance", "all"]:
                            leverage = pos.get('leverage', 'N/A')
                            position_size = pos.get('size', 0)
                            liquidation_price = pos.get('liquidation_price', 0)
                            
                            message += (
                                f"\n{side_emoji} <b>{symbol}</b> ({side})\n"
                                f"   Entry: <code>{entry:.5f}</code> | Now: <code>{current:.5f}</code>\n"
                                f"   Size: <code>{position_size:.4f}</code> | Leverage: <code>{leverage}x</code>\n"
                            )
                            
                            # Show liquidation price if available
                            if liquidation_price > 0:
                                message += f"   💀 Liquidation: <code>{liquidation_price:.5f}</code>\n"
                            
                            message += f"   {pos_emoji} P&L: <code>{pnl_sign_pos}{pos_pnl:.2f} ({pnl_sign_pos}{pos_pnl_pct:.2f}%)</code>\n"
                        elif user_platform in ["mexc", "all"]:
                            lev = pos.get('leverage', 'N/A')
                            position_size = pos.get('size', 0)
                            message += (
                                f"\n{side_emoji} <b>{symbol}</b> ({side})\n"
                                f"   Entry: <code>{entry:.5f}</code> | Now: <code>{current:.5f}</code>\n"
                                f"   Size: <code>{position_size:.4f}</code> | Leverage: <code>{lev}x</code>\n"
                                f"   {pos_emoji} P&L: <code>{pnl_sign_pos}{pos_pnl:.2f} ({pnl_sign_pos}{pos_pnl_pct:.2f}%)</code>\n"
                            )
                        else:
                            # MT5 format
                            volume = pos.get('volume', 0)
                            sl = pos.get('sl', 0)
                            tp = pos.get('tp', 0)
                            
                            message += (
                                f"\n{side_emoji} <b>{symbol}</b> ({side}) {volume} lots\n"
                                f"   Entry: <code>{entry:.5f}</code>\n"
                                f"   Current: <code>{current:.5f}</code>\n"
                                f"   SL: <code>{sl:.5f}</code> | TP: <code>{tp:.5f}</code>\n"
                                f"   {pos_emoji} P&L: <code>{pnl_sign_pos}${pos_pnl:.2f} ({pnl_sign_pos}{pos_pnl_pct:.2f}%)</code>\n"
                            )
                    
                    if len(positions) > 5:
                        message += f"\n<i>... and {len(positions) - 5} more positions</i>\n"
                else:
                    message += "<i>No open positions</i>\n"
                
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=message,
                    parse_mode='HTML',
                    reply_markup=keyboard
                )
            else:
                bot.answer_callback_query(call.id, "❌ No active session. Start trading first.", show_alert=True)
            
            bot.answer_callback_query(call.id)
        except Exception as e:
            print(f"[VIEW_STATUS] Error: {e}")
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # User Dashboard - Show current status
    if callback_data == "user_dashboard":
        try:
            from handlers.welcome_messages import get_active_user_welcome
            
            user_data_db, _ = get_user_by_telegram_id(user_id)
            if user_data_db and user_data_db.get('status') == 'active':
                user_name = user_data_db.get('name') or call.from_user.first_name or "User"
                username = call.from_user.username or "N/A"
                platform = user_data_db.get('platform', 'binance')
                
                username_key = f"user_{user_id}"
                
                # Check trading status based on platform
                if platform == "binance":
                    trading_active = is_trading_active(username_key)
                    trading_mode = get_trading_mode(username_key)
                elif platform == "mt5":
                    trading_active = is_mt5_trading_active(username_key)
                    trading_mode = get_mt5_trading_mode(username_key)
                elif platform == "mexc":
                    trading_active = is_mexc_trading_active(username_key)
                    trading_mode = get_mexc_trading_mode(username_key)
                elif platform == "all":
                    b_active = is_trading_active(username_key)
                    m_active = is_mt5_trading_active(username_key)
                    x_active = is_mexc_trading_active(username_key)
                    trading_active = b_active or m_active or x_active
                    
                    active_modes = []
                    if b_active:
                        active_modes.append("Binance")
                    if x_active:
                        active_modes.append("MEXC")
                    if m_active:
                        active_modes.append("MT5")
                    trading_mode = " & ".join(active_modes) if active_modes else None
                else:  # all platforms
                    # Check if either platform is active
                    binance_active = is_trading_active(username_key)
                    mt5_active = is_mt5_trading_active(username_key)
                    trading_active = binance_active or mt5_active
                    
                    # Get trading mode for active platform(s)
                    if binance_active and mt5_active:
                        trading_mode = "Binance & MT5"
                    elif binance_active:
                        trading_mode = get_trading_mode(username_key)
                    elif mt5_active:
                        trading_mode = get_mt5_trading_mode(username_key)
                    else:
                        trading_mode = None
                
                welcome_text, markup = get_active_user_welcome(
                    user_name, username, user_id, 
                    trading_active, trading_mode, platform
                )
                
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=welcome_text,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                bot.answer_callback_query(call.id)
            else:
                bot.answer_callback_query(call.id, "❌ User not found or inactive", show_alert=True)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # Placeholder responses for other callbacks
    callback_responses = {
        # Admin callbacks
        "admin_stats": "📊 Statistics coming soon...",
        "admin_trading": "📈 Trading overview coming soon...",
        "admin_settings": "⚙️ Settings coming soon...",
        "admin_logs": "📝 Logs coming soon...",
        "admin_refresh": "🔄 Dashboard refreshed!",
        
        # User callbacks - removed user_settings as it's now implemented
        "user_trading": "📈 Trading - Use 'Start Trading' button",
        "user_help": "❓ Help coming soon...",
        "user_info": "ℹ️ Bot information coming soon...",
    }
    
    # ==========================================
    # USER SETTINGS CALLBACKS
    # ==========================================
    
    # User settings menu
    if callback_data == "user_settings":
        show_user_settings(bot, call)
        return
    
    # Change name
    if callback_data == "settings_change_name":
        prompt_change_name(bot, call)
        return
    
    # Delete account - confirm
    if callback_data == "settings_delete_account":
        confirm_delete_account(bot, call)
        return
    
    # Delete account - process
    if callback_data == "settings_confirm_delete":
        process_delete_account(bot, call)
        return
    
    # ==========================================
    # MT5 FOREX TRADING CALLBACKS
    # ==========================================
    
    # Start MT5 Forex Trading
    if callback_data == "trade_mode_mt5":
        try:
            # Check user platform first
            user_data, _ = get_user_by_telegram_id(user_id)
            user_platform = user_data.get('platform', 'binance') if user_data else None
            
            if user_platform not in ["mt5", "all"]:
                bot.answer_callback_query(
                    call.id,
                    f"❌ You are not registered for MT5 trading!\n\nYou registered with {user_platform} platform.",
                    show_alert=True
                )
                return
            
            username_key = f"user_{user_id}"
            
            # Check for metaapi_account_id
            metaapi_account_id = user_data.get('metaapi_account_id')
            if not metaapi_account_id:
                bot.answer_callback_query(
                    call.id,
                    "❌ MetaAPI account not provisioned yet. Contact admin.",
                    show_alert=True
                )
                return
            
            mt5_login = user_data.get('mt5_login')
            mt5_password = user_data.get('mt5_password')
            
            if not mt5_login or not mt5_password:
                bot.answer_callback_query(call.id, "❌ MT5 credentials not found. Please complete registration.", show_alert=True)
                return
            
            initialize_mt5_session(username_key, user_id)
            
            # Check minimum balance via MetaAPI (async)
            from utils.bg_loop import loop
            import asyncio
            import concurrent.futures
            from mt5.mt5_core import create_user_context, get_account_balance as mt5_get_balance, disconnect_mt5 as mt5_disconnect
            from mt5.metaapi_manager import get_last_connection_error

            # Notify user that connection is being established (can take 15-30s)
            bot.answer_callback_query(call.id, "⏳ Connecting to MT5... please wait up to 30 seconds.", show_alert=False)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="<b>⏳ Connecting to MT5...</b>\n\n"
                     "Establishing MetaAPI cloud connection.\n"
                     "This may take up to 30 seconds.",
                parse_mode='HTML',
            )

            import threading
            
            def bg_start_mt5():
                # Quick connect to check balance / verify credentials
                try:
                    ctx_future = asyncio.run_coroutine_threadsafe(
                        create_user_context(user_id, metaapi_account_id, bot=bot), loop
                    )
                    ctx = ctx_future.result(timeout=120)  # Wait up to 120s for connection
                except Exception as e:
                    # Log to admin, show simple message to user
                    error_type = type(e).__name__
                    error_msg = str(e) if str(e) else "Timeout or unknown connection error"
                    try:
                        from config import BOT_CREATOR_ID
                        admin_msg = f"⏱️ <b>Connection Exception</b> ({error_type})\n\n" \
                                    f"User {user_id} MT5 connection failed.\n" \
                                    f"Account ID: <code>{metaapi_account_id}</code>\n" \
                                    f"Error: <code>{error_msg[:200]}</code>"
                        bot.send_message(BOT_CREATOR_ID, admin_msg, parse_mode='HTML')
                    except:
                        pass
                    if error_type == "TimeoutError":
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>⏳ Connection Timeout</b>\n\n"
                                 "It is taking longer than expected to connect to your trading account. "
                                 "Your account may still be provisioning in the background.\n\n"
                                 "Please wait 1-2 minutes and try pressing 'Start Trading' again. ",
                            parse_mode='HTML',
                        )
                    else:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>❌ Connection Failed</b>\n\n"
                                 "Unable to connect to your trading account.\n"
                                 f"Error: {error_type}: {error_msg[:100]}",
                            parse_mode='HTML',
                        )
                    return

                if ctx is None:
                    from mt5.metaapi_manager import (
                        get_last_connection_error,
                        get_last_connection_raw_error,
                        is_admin_action_required,
                    )
                    user_msg   = get_last_connection_error(user_id) or "Unknown error"
                    admin_raw  = get_last_connection_raw_error(user_id) or user_msg
                    need_admin = is_admin_action_required(user_id)

                    try:
                        from config import BOT_CREATOR_ID
                        if need_admin:
                            admin_msg = (
                                f"🚨 <b>ACTION REQUIRED: MetaAPI Top-Up Needed</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                f"👤 <b>Affected User:</b> <code>{user_id}</code>\n"
                                f"🆔 <b>MetaAPI Account ID:</b>\n"
                                f"<code>{metaapi_account_id}</code>\n\n"
                                f"❌ <b>Root Cause:</b>\n"
                                f"MetaAPI refused to deploy the trading account because "
                                f"the MetaAPI subscription/credit balance is insufficient.\n\n"
                                f"📋 <b>Raw API Error:</b>\n"
                                f"<code>{admin_raw[:400]}</code>\n\n"
                                f"✅ <b>Action Required:</b>\n"
                                f"Log in and top up your MetaAPI account:\n"
                                f"https://app.metaapi.cloud/billing\n\n"
                                f"<i>The user has been told to contact you.</i>"
                            )
                        else:
                            admin_msg = (
                                f"❌ <b>MT5 Connection Failed</b>\n\n"
                                f"User: <code>{user_id}</code>\n"
                                f"Account ID: <code>{metaapi_account_id}</code>\n\n"
                                f"<b>Error Details:</b>\n"
                                f"<code>{admin_raw[:300]}</code>"
                            )
                        bot.send_message(BOT_CREATOR_ID, admin_msg, parse_mode='HTML')
                    except:
                        pass

                    # Simple, clean message to user — never show raw API errors
                    if need_admin:
                        user_display_msg = (
                            "<b>⚠️ MT5 Temporarily Unavailable</b>\n\n"
                            "We are unable to connect to your trading account right now "
                            "due to a service configuration issue.\n\n"
                            "📩 Please <b>contact the admin</b> to resolve this.\n"
                            "<i>The admin has already been notified.</i>"
                        )
                    else:
                        user_display_msg = f"<b>❌ Connection Failed</b>\n\n{user_msg}"
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=user_display_msg,
                        parse_mode='HTML',
                    )
                    return

                try:
                    bal_future = asyncio.run_coroutine_threadsafe(
                        mt5_get_balance(ctx), loop
                    )
                    balance = bal_future.result()
                except Exception:
                    asyncio.run_coroutine_threadsafe(mt5_disconnect(user_id), loop)
                    try:
                        from config import BOT_CREATOR_ID
                        admin_msg = f"⏱️ <b>Balance Check Timeout</b>\n\nUser {user_id} balance query timed out."
                        bot.send_message(BOT_CREATOR_ID, admin_msg, parse_mode='HTML')
                    except:
                        pass
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="<b>❌ Connection Failed</b>\n\n"
                             "Unable to retrieve account information.\n"
                             "Please try again.",
                        parse_mode='HTML',
                    )
                    return

                if balance is None:
                    asyncio.run_coroutine_threadsafe(mt5_disconnect(user_id), loop)
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="<b>❌ Connection Failed</b>\n\n"
                             "Unable to retrieve account information.\n"
                             "Please try again or contact support.",
                        parse_mode='HTML',
                    )
                    return

                if balance < MIN_BALANCE:
                    asyncio.run_coroutine_threadsafe(mt5_disconnect(user_id), loop)
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=f"<b>❌ Insufficient MT5 Balance</b>\n\n"
                             f"💰 Your balance: <b>${balance:.2f}</b>\n"
                             f"⚠️ Minimum required: <b>${MIN_BALANCE:.2f}</b>\n\n"
                             "Please deposit funds into your MT5 account and try again.",
                        parse_mode='HTML',
                    )
                    return

                # Pass the already-connected context to avoid a redundant reconnection
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        start_mt5_trading(bot, user_id, username_key, existing_ctx=ctx), loop
                    )
                    success = future.result()
                except Exception as e:
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="<b>❌ Start Failed</b>\n\n"
                             f"MT5 trading could not be started: {str(e)[:50]}\n",
                        parse_mode='HTML',
                    )
                    return

                if success:
                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.row(
                        types.InlineKeyboardButton("🛑 Stop MT5", callback_data="stop_mt5"),
                        types.InlineKeyboardButton("📊 MT5 Status", callback_data="view_mt5_status")
                    )
                    keyboard.row(
                        types.InlineKeyboardButton("💰 MT5 Balance", callback_data="view_mt5_balance"),
                        types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                    )
                    
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="<b>💱 MT5 FOREX TRADING STARTED 💱</b>\n"
                             "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                             f"👤 <b>Login:</b> {mt5_login}\n"
                             "✅ <b>Platform:</b> MetaAPI Cloud\n"
                             "✅ <b>Status:</b> <u>Running</u>\n"
                             "✅ <b>Pairs:</b> XAUUSD (Gold)\n\n"
                             "<i>The bot is scanning forex pairs using StochRSI signals.</i>",
                        parse_mode='HTML',
                        reply_markup=keyboard
                    )
                else:
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="<b>❌ Failed to Start MT5 Trading</b>\n\n"
                             "The trading loop could not be started.\n"
                             "Please try again or contact admin.",
                        parse_mode='HTML',
                    )
            
            # Start background thread
            threading.Thread(target=bg_start_mt5, daemon=True).start()
            
            try:
                bot.answer_callback_query(call.id)
            except Exception as answer_err:
                print(f"[MT5-START] ⚠️ Could not answer callback query (likely expired): {answer_err}")
        except Exception as e:
            import traceback
            print(f"[MT5-START] ❌ Unexpected error: {traceback.format_exc()}")
            try:
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"<b>❌ Unexpected MT5 Error</b>\n\n<code>{str(e)[:200]}</code>",
                    parse_mode='HTML',
                )
            except Exception:
                bot.answer_callback_query(call.id, f"❌ MT5 Error: {str(e)[:100]}", show_alert=True)
        return
    
    # Stop MT5 Trading
    if callback_data == "stop_mt5":
        try:
            username_key = f"user_{user_id}"
            
            from utils.bg_loop import loop
            import asyncio
            import concurrent.futures
            
            import threading
            
            bot.answer_callback_query(call.id, "🛑 Stopping MT5 Trading... Please wait.")
            
            def bg_stop_mt5():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        stop_mt5_trading(username_key), loop
                    )
                    success = future.result() # Wait indefinitely for cloud to release context
                    
                    if success:
                        keyboard = types.InlineKeyboardMarkup()
                        keyboard.row(
                            types.InlineKeyboardButton("💱 Restart MT5", callback_data="trade_mode_mt5"),
                            types.InlineKeyboardButton("🚀 Futures", callback_data="trade_mode_future")
                        )
                        keyboard.row(
                            types.InlineKeyboardButton("🔙 Main Menu", callback_data="user_dashboard")
                        )
                        
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>🛑 MT5 TRADING STOPPED 🛑</b>\n"
                                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                 "✅ MT5 forex trading has been stopped\n"
                                 "✅ No new trades will be opened\n\n"
                                 "<i>You can restart anytime.</i>",
                            parse_mode='HTML',
                            reply_markup=keyboard
                        )
                except Exception as e:
                    pass
                    
            threading.Thread(target=bg_stop_mt5, daemon=True).start()
            
            bot.answer_callback_query(call.id, "🛑 MT5 Trading stopped")
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # View MT5 Balance
    if callback_data == "view_mt5_balance":
        try:
            username_key = f"user_{user_id}"
            
            from utils.bg_loop import loop
            import asyncio
            import threading
            from handlers.mt5_handler import get_mt5_balance_async
            
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="<b>⏳ Fetching MT5 Balance...</b>\n\nConnecting to broker to retrieve latest balance...",
                parse_mode='HTML'
            )
            
            def bg_view_mt5_bal():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        get_mt5_balance_async(user_id), loop
                    )
                    balance = future.result()  # Wait indefinitely to ensure balance loads
                    
                    if balance is not None:
                        keyboard = types.InlineKeyboardMarkup()
                        keyboard.row(
                            types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                        )
                        
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>💼 MT5 ACCOUNT BALANCE 💼</b>\n"
                                 "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                 f"💵 <b>Balance:</b> <code>${balance:.2f}</code>",
                            parse_mode='HTML',
                            reply_markup=keyboard
                        )
                    else:
                        bot.edit_message_text(
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            text="<b>❌ Cannot fetch MT5 balance.</b>\n\nIs trading active or account configured properly?",
                            parse_mode='HTML'
                        )
                except Exception as e:
                    pass
            
            threading.Thread(target=bg_view_mt5_bal, daemon=True).start()
            bot.answer_callback_query(call.id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    # View MT5 Status - Detailed P&L Report
    if callback_data == "view_mt5_status":
        try:
            username_key = f"user_{user_id}"
            status = get_detailed_mt5_status(username_key)
            
            if status:
                keyboard = types.InlineKeyboardMarkup()
                keyboard.row(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="view_mt5_status"),
                    types.InlineKeyboardButton("🔙 Back", callback_data="user_dashboard")
                )
                
                # Basic info
                bot_status = status.get('status') or 'Unknown'
                trading_mode = status.get('trading_mode') or 'Not Set'
                active_trades = status.get('active_trades', 0)
                balance = status.get('balance', 0)
                equity = status.get('equity', 0)
                margin = status.get('margin', 0)
                free_margin = status.get('free_margin', 0)
                unrealized_pnl = status.get('unrealized_pnl', 0)
                pnl_percentage = status.get('pnl_percentage', 0)
                leverage = status.get('leverage', 0)
                currency = status.get('currency', 'USD')
                positions = status.get('positions', [])
                
                # Format values
                pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                pnl_sign = "+" if unrealized_pnl >= 0 else ""
                
                # Build message
                message = (
                    "<b>📊 MT5 FOREX DETAILED REPORT 📊</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"💹 <b>Platform:</b> MetaTrader 5\n"
                    f"🔄 <b>Status:</b> <u>{bot_status}</u>\n"
                    f"📈 <b>Mode:</b> {trading_mode}\n\n"
                    
                    "<b>💰 ACCOUNT SUMMARY</b>\n"
                    "─────────────────────\n"
                    f"💵 <b>Balance:</b> <code>${balance:.2f}</code>\n"
                    f"📊 <b>Equity:</b> <code>${equity:.2f}</code>\n"
                    f"🔒 <b>Margin Used:</b> <code>${margin:.2f}</code>\n"
                    f"💎 <b>Free Margin:</b> <code>${free_margin:.2f}</code>\n"
                    f"⚡ <b>Leverage:</b> 1:{leverage}\n\n"
                    
                    f"{pnl_emoji} <b>Unrealized P&L:</b> <code>{pnl_sign}${unrealized_pnl:.2f}</code>\n"
                    f"📈 <b>P&L %:</b> <code>{pnl_sign}{pnl_percentage:.2f}%</code>\n\n"
                    
                    f"<b>📊 OPEN POSITIONS ({active_trades}/{MT5_MAX_TRADES})</b>\n"
                    "─────────────────────\n"
                )
                
                if positions:
                    for pos in positions[:5]:
                        symbol = pos.get('symbol', 'Unknown')
                        side = pos.get('side', '?')
                        volume = pos.get('volume', 0)
                        pos_pnl = pos.get('profit', 0)
                        pos_pnl_pct = pos.get('pnl_percentage', 0)
                        entry = pos.get('entry_price', 0)
                        current = pos.get('current_price', 0)
                        sl = pos.get('sl', 0)
                        tp = pos.get('tp', 0)
                        
                        pos_emoji = "🟢" if pos_pnl >= 0 else "🔴"
                        side_emoji = "📈" if side == "BUY" else "📉"
                        pnl_sign_pos = "+" if pos_pnl >= 0 else ""
                        
                        message += (
                            f"\n{side_emoji} <b>{symbol}</b> ({side}) {volume} lots\n"
                            f"   Entry: <code>{entry:.5f}</code>\n"
                            f"   Current: <code>{current:.5f}</code>\n"
                            f"   SL: <code>{sl:.5f}</code> | TP: <code>{tp:.5f}</code>\n"
                            f"   {pos_emoji} P&L: <code>{pnl_sign_pos}${pos_pnl:.2f} ({pnl_sign_pos}{pos_pnl_pct:.2f}%)</code>\n"
                        )
                    
                    if len(positions) > 5:
                        message += f"\n<i>... and {len(positions) - 5} more positions</i>\n"
                else:
                    message += "<i>No open positions</i>\n"
                
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=message,
                    parse_mode='HTML',
                    reply_markup=keyboard
                )
            else:
                bot.answer_callback_query(call.id, "❌ No active MT5 session. Start trading first.", show_alert=True)
            
            bot.answer_callback_query(call.id)
        except Exception as e:
            print(f"[VIEW_MT5_STATUS] Error: {e}")
            bot.answer_callback_query(call.id, f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
    
    response = callback_responses.get(callback_data, "⚠️ Unknown action")
    bot.answer_callback_query(call.id, response, show_alert=False)
