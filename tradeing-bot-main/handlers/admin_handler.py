"""
Admin panel handlers for user management
"""
from telebot import types
from telebot.apihelper import ApiTelegramException
from user_control.add_users import (
    get_pending_users, 
    approve_user, 
    delete_user,
    is_admin,
    get_user_by_telegram_id
)
from config import BOT_CREATOR_ID, MAIN_ADMIN
import firebase_admin
from firebase_admin import firestore
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError

# Get Firestore client
db = firestore.client()

# Prevent duplicate concurrent provisioning for the same user (e.g., double-click Accept)
_approvals_in_progress = set()
_approvals_lock = threading.Lock()

# Track in-flight MetaAPI provisioning futures across approval retries so we do
# not create duplicate paid deployments when an earlier attempt finishes late.
_provisioning_futures = {}
_provisioning_futures_lock = threading.Lock()


def _finalize_provisioning_future(telegram_id, future):
    """Persist a finished provisioning result, then clear the shared future."""
    try:
        metaapi_account_id = future.result()
        if metaapi_account_id:
            from user_control.add_users import update_user_metaapi_account_id
            update_user_metaapi_account_id(telegram_id, metaapi_account_id)
            print(
                f"[ADMIN-APPROVE] ✅ Saved completed background provisioning for "
                f"user {telegram_id}: {metaapi_account_id}"
            )
        else:
            print(f"[ADMIN-APPROVE] ⚠️ Background provisioning finished without an account ID for {telegram_id}")
    except Exception as exc:
        print(f"[ADMIN-APPROVE] ⚠️ Background provisioning failed for {telegram_id}: {exc}")
    finally:
        with _provisioning_futures_lock:
            if _provisioning_futures.get(telegram_id) is future:
                _provisioning_futures.pop(telegram_id, None)


def _get_or_start_provisioning_future(telegram_id, mt5_login, mt5_password, mt5_server, bot):
    """Reuse an active provisioning task for the user or start a new one."""
    with _provisioning_futures_lock:
        existing_future = _provisioning_futures.get(telegram_id)
        if existing_future and not existing_future.done():
            return existing_future, False
        if existing_future and existing_future.done():
            _provisioning_futures.pop(telegram_id, None)

        import asyncio
        from utils.bg_loop import loop
        from mt5.metaapi_manager import provision_account

        future = asyncio.run_coroutine_threadsafe(
            provision_account(
                telegram_id=telegram_id,
                mt5_login=str(mt5_login),
                mt5_password=mt5_password,
                mt5_server=mt5_server,
                name=f"mt5_{telegram_id}",
                bot=bot
            ),
            loop
        )
        future.add_done_callback(lambda done_future: _finalize_provisioning_future(telegram_id, done_future))
        _provisioning_futures[telegram_id] = future
        return future, True


def safe_edit(bot, call, text, parse_mode='HTML', reply_markup=None):
    """Edit a message, silently ignoring 'message is not modified' errors."""
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except ApiTelegramException as e:
        if 'message is not modified' not in str(e):
            raise


def show_pending_approvals(bot, call):
    """Display all pending user registrations"""
    user_id = call.from_user.id
    
    # Verify admin
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    # Get pending users
    pending_users = get_pending_users()
    
    if not pending_users:
        safe_edit(bot, call, "✅ <b>No Pending Approvals</b>\n\nAll registrations have been processed.")
        return
    
    # Display pending users with platform info
    message = f"⏳ <b>Pending Approvals ({len(pending_users)})</b>\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    message += "<i>Click on a user to view details and take action</i>\n\n"
    
    for idx, user in enumerate(pending_users, 1):
        telegram_id = user['telegram_id']
        name = user.get('name', 'N/A')
        platform = user.get('platform', 'binance')
        registered_at = user.get('registered_at', 'Unknown')
        
        # Platform display
        if platform == "binance":
            platform_emoji = "📈"
            platform_name = "Binance"
        elif platform == "mt5":
            platform_emoji = "💹"
            platform_name = "MT5"
        elif platform == "mexc":
            platform_emoji = "🔷"
            platform_name = "MEXC"
        else:  # all
            platform_emoji = "🌐"
            platform_name = "All Platforms"
        
        message += f"<b>{idx}. {name}</b>\n"
        message += f"🆔 <code>{telegram_id}</code> | {platform_emoji} {platform_name}\n\n"
    
    # Create inline keyboard with user selection buttons
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for user in pending_users:
        telegram_id = user['telegram_id']
        name = user.get('name', str(telegram_id))[:20]
        platform = user.get('platform', 'binance')
        
        # Platform emoji
        if platform == "binance":
            platform_emoji = "📈"
        elif platform == "mt5":
            platform_emoji = "💹"
        elif platform == "mexc":
            platform_emoji = "🔷"
        elif platform == "all":
            platform_emoji = "🌐"
        else:
            platform_emoji = "🌐"
        
        markup.add(
            types.InlineKeyboardButton(
                f"{platform_emoji} {name} - ID: {telegram_id}", 
                callback_data=f"view_pending_{telegram_id}"
            )
        )
    
    markup.add(
        types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending"),
        types.InlineKeyboardButton("🏠 Back to Dashboard", callback_data="back_to_dashboard")
    )
    
    safe_edit(bot, call, message, reply_markup=markup)


def view_pending_user_details(bot, call, telegram_id):
    """Display detailed information of a pending user with Accept/Reject buttons"""
    admin_id = call.from_user.id
    
    # Verify admin
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    # Get user data from database
    user_data, _ = get_user_by_telegram_id(telegram_id)
    
    if not user_data:
        bot.answer_callback_query(call.id, "❌ User not found", show_alert=True)
        return
    
    name = user_data.get('name', 'N/A')
    platform = user_data.get('platform', 'binance')
    registered_at = user_data.get('registered_at', 'Unknown')
    language = user_data.get('language', 'en')
    status = user_data.get('status', 'pending')
    
    # Platform display
    if platform == "binance":
        platform_emoji = "📈"
        platform_name = "Binance Crypto Trading"
        platform_desc = "Spot & Futures Trading"
        credentials = f"🔑 <b>API Key:</b> {user_data.get('api_key', 'N/A')[:15]}...\n"
        credentials += f"🔐 <b>API Secret:</b> {user_data.get('api_secret', 'N/A')[:15]}...\n"
    elif platform == "mt5":
        platform_emoji = "💹"
        platform_name = "MT5 Forex/Gold Trading"
        platform_desc = "Forex & Gold Trading"
        credentials = f"🔢 <b>MT5 Login:</b> {user_data.get('mt5_login', 'N/A')}\n"
        credentials += f"🌐 <b>MT5 Server:</b> {user_data.get('mt5_server', 'N/A')}\n"
        metaapi_id = user_data.get('metaapi_account_id')
        if metaapi_id:
            credentials += f"☁️ <b>MetaAPI ID:</b> {metaapi_id[:10]}...\n"
    elif platform == "mexc":
        platform_emoji = "🔷"
        platform_name = "MEXC Futures Trading"
        platform_desc = "Crypto Perpetual Futures"
        credentials = f"🔑 <b>MEXC Access Key:</b> {user_data.get('mexc_api_key', 'N/A')[:15]}...\n"
        credentials += f"🔐 <b>MEXC Secret Key:</b> {user_data.get('mexc_api_secret', 'N/A')[:15]}...\n"
    elif platform == "all":
        platform_emoji = "🌐"
        platform_name = "All Platforms"
        platform_desc = "Binance + MEXC + MT5 Trading"
        credentials = f"<b>📈 Binance Credentials:</b>\n"
        credentials += f"🔑 API Key: {user_data.get('api_key', 'N/A')[:15]}...\n"
        credentials += f"🔐 API Secret: {user_data.get('api_secret', 'N/A')[:15]}...\n\n"
        credentials += f"<b>🔷 MEXC Credentials:</b>\n"
        credentials += f"🔑 MEXC Access Key: {user_data.get('mexc_api_key', 'N/A')[:15]}...\n"
        credentials += f"🔐 MEXC Secret Key: {user_data.get('mexc_api_secret', 'N/A')[:15]}...\n\n"
        credentials += f"<b>💹 MT5 Credentials:</b>\n"
        credentials += f"🔢 MT5 Login: {user_data.get('mt5_login', 'N/A')}\n"
        credentials += f"🌐 MT5 Server: {user_data.get('mt5_server', 'N/A')}\n"
        metaapi_id = user_data.get('metaapi_account_id')
        if metaapi_id:
            credentials += f"☁️ MetaAPI ID: {metaapi_id[:10]}...\n"
    
    # Format registration date
    if registered_at != 'Unknown':
        try:
            reg_date = registered_at[:19].replace('T', ' ')
        except:
            reg_date = registered_at[:10] if len(registered_at) >= 10 else registered_at
    else:
        reg_date = 'Unknown'
    
    message = (
        f"👤 <b>USER DETAILS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Name:</b> {name}\n"
        f"<b>Telegram ID:</b> <code>{telegram_id}</code>\n"
        f"{platform_emoji} <b>Platform:</b> {platform_name}\n"
        f"📊 <b>Trading Type:</b> {platform_desc}\n"
        f"🌐 <b>Language:</b> {language.upper()}\n"
        f"📅 <b>Registered:</b> {reg_date}\n"
        f"⏳ <b>Status:</b> {status.capitalize()}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>CREDENTIALS:</b>\n\n"
        f"{credentials}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Take action:</b> ✅ Accept or ❌ Reject this registration"
    )
    
    # Create inline keyboard with Accept/Reject buttons
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Accept", callback_data=f"approve_{telegram_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{telegram_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📋 Back to Pending List", callback_data="admin_pending")
    )
    
    safe_edit(bot, call, message, reply_markup=markup)


def approve_user_registration(bot, call, telegram_id):
    """Approve a pending user"""
    admin_id = call.from_user.id
    
    # Verify admin
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    # Prevent duplicate concurrent approval/provision attempts for same user
    with _approvals_lock:
        if telegram_id in _approvals_in_progress:
            bot.answer_callback_query(call.id, "⏳ Approval already in progress for this user.", show_alert=True)
            return
        _approvals_in_progress.add(telegram_id)

    try:
        # Fetch user data first
        user_data, _ = get_user_by_telegram_id(telegram_id)
        if not user_data:
            bot.answer_callback_query(call.id, "❌ User not found", show_alert=True)
            return

        # If already approved, do not provision or approve again
        if user_data.get('status') == 'active':
            bot.answer_callback_query(call.id, "ℹ️ User is already approved.", show_alert=True)
            return
            
        platform = user_data.get('platform', 'binance')
        mt5_login = user_data.get('mt5_login')
        mt5_password = user_data.get('mt5_password')
        mt5_server = user_data.get('mt5_server')
        existing_metaapi_id = user_data.get('metaapi_account_id')
        
        # Provision MetaAPI account only if needed
        if platform in ('mt5', 'all') and mt5_login and mt5_password and mt5_server:
            if existing_metaapi_id:
                print(f"[ADMIN-APPROVE] ℹ️ Reusing existing MetaAPI account: {existing_metaapi_id}")
            else:
                try:
                    bot.answer_callback_query(call.id, "⏳ Provisioning MetaAPI account...", show_alert=False)
                    future, started_new_future = _get_or_start_provisioning_future(
                        telegram_id=telegram_id,
                        mt5_login=mt5_login,
                        mt5_password=mt5_password,
                        mt5_server=mt5_server,
                        bot=bot
                    )
                    if not started_new_future:
                        print(f"[ADMIN-APPROVE] ℹ️ Reusing in-flight provisioning task for user {telegram_id}")

                    # Real MT5 accounts can take 2-5 minutes to deploy.
                    # 360 s (6 min) = 300 s wait_deployed + 60 s margin for
                    # account creation, deploy kick-off, and the post-deploy sleep.
                    metaapi_account_id = future.result(timeout=360)
                    if not metaapi_account_id:
                        raise Exception(
                            "MetaAPI provisioning returned no account ID. "
                            "Check MT5 login, password, server, and MetaAPI logs."
                        )
                    print(f"[ADMIN-APPROVE] ✅ MetaAPI account provisioned: {metaapi_account_id}")
                    
                    # Update user in Firebase
                    from user_control.add_users import update_user_metaapi_account_id
                    update_user_metaapi_account_id(telegram_id, metaapi_account_id)
                    
                except FutureTimeoutError as e:
                    error_str = str(e) or repr(e) or "MetaAPI provisioning timed out"
                    print(f"[ADMIN-APPROVE] ⚠️ MetaAPI provisioning timed out: {repr(e)}")
                    bot.send_message(
                        admin_id,
                        f"❌ <b>Provisioning Timed Out for ID: {telegram_id}</b>\n\n"
                        "MetaAPI did not finish provisioning within 360 seconds.\n"
                        "This happens more often with real MT5 accounts than demo accounts.\n\n"
                        "The user is still pending. Provisioning is still running in the background, "
                        "and if it finishes successfully the MetaAPI account ID will be saved automatically.\n\n"
                        "Please retry approval later or check MetaAPI dashboard/logs.",
                        parse_mode='HTML'
                    )

                    try:
                        bot.send_message(
                            telegram_id,
                            "⚠️ <b>Your registration is still pending</b>\n\n"
                            "Your MT5 cloud setup is taking longer than expected. "
                            "Please wait while admin checks it.",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass

                    try:
                        bot.answer_callback_query(call.id, "❌ Provisioning timed out. Left as pending.", show_alert=True)
                    except Exception:
                        pass
                    return
                except Exception as e:
                    error_str = str(e) or repr(e) or "Unknown MetaAPI provisioning error"
                    print(f"[ADMIN-APPROVE] ⚠️ MetaAPI provisioning failed: {e}")
                    if 'top up' in error_str.lower() or 'billing' in error_str.lower() or 'credit' in error_str.lower():
                        bot.send_message(
                            admin_id,
                            f"❌ <b>Provisioning Failed for ID: {telegram_id}</b>\n\n"
                            "MetaAPI account has insufficient credits. Please top up the billing.",
                            parse_mode='HTML'
                        )
                    else:
                        bot.send_message(
                            admin_id,
                            f"❌ <b>Provisioning Failed for ID: {telegram_id}</b>\n\nError: {error_str}",
                            parse_mode='HTML'
                        )

                    # Notify user explicitly so they are not left unaware
                    try:
                        bot.send_message(
                            telegram_id,
                            "⚠️ <b>Your registration is still pending</b>\n\n"
                            "We could not finish your MT5 cloud setup yet. "
                            "Please wait while admin resolves this issue, then try again later.",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass

                    try:
                        bot.answer_callback_query(call.id, "❌ Provisioning failed. Left as pending.", show_alert=True)
                    except Exception:
                        pass
                    return
                
        # Approve user
        success, message, username = approve_user(telegram_id, BOT_CREATOR_ID)
        
        if success:
            try:
                bot.answer_callback_query(call.id, f"✅ User approved!", show_alert=False)
            except Exception as e:
                print(f"Error answering callback: {e}")
                
            # Notify the user
            try:
                bot.send_message(
                    telegram_id,
                    "🎉 <b>Registration Approved!</b>\n\n"
                    "Your account has been activated.\n"
                    "You can now use the bot. Send /start to begin!",
                    parse_mode='HTML'
                )
            except:
                pass  # User might have blocked the bot
                
            # Notify the admin
            try:
                bot.send_message(
                    admin_id,
                    f"✅ User {telegram_id} has been successfully approved.",
                    parse_mode='HTML'
                )
            except:
                pass
            
            # Refresh the pending list
            try:
                show_pending_approvals(bot, call)
            except Exception as e:
                print(f"Error refreshing pending user list: {e}")
        else:
            try:
                bot.answer_callback_query(call.id, f"❌ Error: {message}", show_alert=True)
            except:
                pass
    finally:
        with _approvals_lock:
            _approvals_in_progress.discard(telegram_id)


def reject_user_registration(bot, call, telegram_id):
    """Reject and delete a pending user from the database"""
    admin_id = call.from_user.id
    
    # Verify admin
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    # Get user data before deletion
    user_data, _ = get_user_by_telegram_id(telegram_id)
    
    if not user_data:
        bot.answer_callback_query(call.id, "❌ User not found", show_alert=True)
        return
    
    user_name = user_data.get('name', 'N/A')
    platform = user_data.get('platform', 'binance')
    username_key = f"user_{telegram_id}"
    
    # ─────────────────────────────────────────────────
    # STEP 1: Stop any active trading sessions
    # ─────────────────────────────────────────────────
    try:
        from utils.bg_loop import loop
        import asyncio
        
        # Stop Binance futures trading
        if platform in ('binance', 'all'):
            try:
                from handlers.trading_handler import is_trading_active, stop_futures_trading
                if is_trading_active(username_key):
                    future = asyncio.run_coroutine_threadsafe(
                        stop_futures_trading(username_key), loop
                    )
                    future.result(timeout=5)
                    print(f"[ADMIN-REJECT] Stopped Binance futures for {telegram_id}")
            except Exception as e:
                print(f"[ADMIN-REJECT] Error stopping Binance trading: {e}")
        
        # Stop MEXC trading
        if platform in ('mexc', 'all'):
            try:
                from handlers.mexc_handler import is_mexc_trading_active, stop_mexc_trading
                if is_mexc_trading_active(username_key):
                    future = asyncio.run_coroutine_threadsafe(
                        stop_mexc_trading(username_key), loop
                    )
                    future.result(timeout=5)
                    print(f"[ADMIN-REJECT] Stopped MEXC trading for {telegram_id}")
            except Exception as e:
                print(f"[ADMIN-REJECT] Error stopping MEXC trading: {e}")
        
        # Stop MT5 trading
        if platform in ('mt5', 'all'):
            try:
                from handlers.mt5_handler import is_mt5_trading_active, stop_mt5_trading
                if is_mt5_trading_active(username_key):
                    future = asyncio.run_coroutine_threadsafe(
                        stop_mt5_trading(username_key), loop
                    )
                    future.result(timeout=5)
                    print(f"[ADMIN-REJECT] Stopped MT5 trading for {telegram_id}")
            except Exception as e:
                print(f"[ADMIN-REJECT] Error stopping MT5 trading: {e}")
    except Exception as e:
        print(f"[ADMIN-REJECT] Error stopping trading sessions: {e}")
    
    # ─────────────────────────────────────────────────
    # STEP 2: Remove MetaAPI cloud account if exists
    # ─────────────────────────────────────────────────
    metaapi_account_id = user_data.get('metaapi_account_id')
    if metaapi_account_id and platform in ('mt5', 'all'):
        try:
            from mt5.metaapi_manager import remove_account
            from utils.bg_loop import loop
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                remove_account(metaapi_account_id), loop
            )
            future.result(timeout=30)
            print(f"[ADMIN-REJECT] MetaAPI account {metaapi_account_id} removed for user {telegram_id}")
        except Exception as e:
            print(f"[ADMIN-REJECT] Error removing MetaAPI account: {e}")
    
    # ─────────────────────────────────────────────────
    # STEP 3: Clean up global state and caches
    # ─────────────────────────────────────────────────
    try:
        from utils.cleanup_utils import cleanup_all_user_sessions
        cleanup_all_user_sessions(username_key, telegram_id)
        print(f"[ADMIN-REJECT] Cleaned up all sessions for {telegram_id}")
    except Exception as e:
        print(f"[ADMIN-REJECT] Error cleaning up sessions: {e}")
    
    # Notify user before deletion
    try:
        bot.send_message(
            telegram_id,
            "❌ <b>Registration Rejected</b>\n\n"
            "Your registration was not approved by the administrator.\n"
            "Your account has been removed from the system.\n\n"
            f"If you have questions, please contact: {MAIN_ADMIN}",
            parse_mode='HTML'
        )
    except:
        pass  # User might have blocked the bot
    
    # Delete user from database
    success = delete_user(telegram_id)
    
    if success:
        try:
            bot.answer_callback_query(
                call.id, 
                f"✅ User {user_name} rejected and removed from database", 
                show_alert=True
            )
        except:
            pass
            
        # Notify the admin
        try:
            bot.send_message(
                admin_id,
                f"❌ User {telegram_id} ({user_name}) has been rejected.",
                parse_mode='HTML'
            )
        except:
            pass

        # Refresh the pending list or show success message
        try:
            # Try to show pending list
            show_pending_approvals(bot, call)
        except Exception as e:
            # If edit fails, just show a message
            print(f"Error refreshing pending list: {e}")
    else:
        try:
            bot.answer_callback_query(call.id, f"❌ Error deleting user", show_alert=True)
        except:
            pass


def show_manage_users(bot, call):
    """Display all users with management options"""
    user_id = call.from_user.id
    
    # Verify admin
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    # Get all users from database
    users_ref = db.collection("users")
    all_users = list(users_ref.stream())
    
    if not all_users:
        safe_edit(bot, call, "📭 <b>No Users Found</b>\n\nThe database is empty.")
        return
    
    # Count users by status
    active_count = sum(1 for u in all_users if u.to_dict().get('status') == 'active')
    pending_count = sum(1 for u in all_users if u.to_dict().get('status') == 'pending')
    rejected_count = sum(1 for u in all_users if u.to_dict().get('status') == 'rejected')
    
    # Display user statistics
    message = f"👥 <b>USER MANAGEMENT</b>\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    message += f"📊 <b>Total Users:</b> {len(all_users)}\n"
    message += f"✅ <b>Active:</b> {active_count}\n"
    message += f"⏳ <b>Pending:</b> {pending_count}\n"
    message += f"❌ <b>Rejected:</b> {rejected_count}\n\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Show recent users (last 10)
    message += "<b>📋 Recent Users:</b>\n\n"
    for idx, user_doc in enumerate(all_users[-10:], 1):
        user = user_doc.to_dict()
        telegram_id = user.get('telegram_id', 'N/A')
        name = user.get('name', 'N/A')
        platform = user.get('platform', 'binance')
        status = user.get('status', 'unknown')
        
        # Status emoji
        status_emoji = "✅" if status == "active" else ("⏳" if status == "pending" else "❌")
        
        # Platform emoji
        if platform == "binance":
            platform_emoji = "📈"
            platform_display = "Binance"
        elif platform == "mt5":
            platform_emoji = "💹"
            platform_display = "MT5"
        else:  # all
            platform_emoji = "🌐"
            platform_display = "All Platforms"
        
        message += f"{idx}. {status_emoji} <b>{name}</b>\n"
        message += f"   🆔 <code>{telegram_id}</code>\n"
        message += f"   {platform_emoji} {platform_display}\n\n"
    
    # Create inline keyboard
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🗑️ Delete User by ID", callback_data="admin_delete_user"),
        types.InlineKeyboardButton("📋 View All Users", callback_data="admin_view_all_users"),
        types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_users"),
        types.InlineKeyboardButton("🏠 Back to Dashboard", callback_data="back_to_dashboard")
    )
    
    safe_edit(bot, call, message, reply_markup=markup)


def show_all_users(bot, call):
    """Display all users in detail"""
    user_id = call.from_user.id
    
    # Verify admin
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    # Get all users from database
    users_ref = db.collection("users")
    all_users = list(users_ref.stream())
    
    if not all_users:
        safe_edit(bot, call, "📭 <b>No Users Found</b>")
        return
    
    message = f"👥 <b>ALL USERS ({len(all_users)})</b>\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for idx, user_doc in enumerate(all_users, 1):
        user = user_doc.to_dict()
        telegram_id = user.get('telegram_id', 'N/A')
        name = user.get('name', 'N/A')
        platform = user.get('platform', 'binance')
        status = user.get('status', 'unknown')
        registered_at = user.get('registered_at', 'Unknown')[:10]
        
        status_emoji = "✅" if status == "active" else ("⏳" if status == "pending" else "❌")
        
        # Platform emoji and display
        if platform == "binance":
            platform_emoji = "📈"
            platform_display = "Binance"
        elif platform == "mt5":
            platform_emoji = "💹"
            platform_display = "MT5"
        else:  # all
            platform_emoji = "🌐"
            platform_display = "All Platforms"
        
        message += f"<b>{idx}. {name}</b>\n"
        message += f"🆔 <code>{telegram_id}</code>\n"
        message += f"{platform_emoji} {platform_display} | {status_emoji} {status}\n"
        message += f"📅 {registered_at}\n\n"
        
        # Split message if too long
        if len(message) > 3500:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_users"))
            safe_edit(bot, call, message + f"\n<i>... and {len(all_users) - idx} more users</i>", reply_markup=markup)
            return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_users"))
    safe_edit(bot, call, message, reply_markup=markup)


def prompt_delete_user(bot, call):
    """Prompt admin to enter user's Telegram ID for deletion"""
    user_id = call.from_user.id
    
    # Verify admin
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    message = (
        "🗑️ <b>DELETE USER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>Warning:</b> This action cannot be undone!\n\n"
        "Please send the <b>Telegram ID</b> of the user you want to delete.\n\n"
        "<i>Example: 123456789</i>\n\n"
        "The user will be permanently removed from Firebase."
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_users"))
    
    safe_edit(bot, call, message, reply_markup=markup)
    
    # Register next step handler to receive the telegram ID
    bot.register_next_step_handler(call.message, process_delete_user, bot)


def process_delete_user(message, bot):
    """Process the deletion of a user"""
    admin_id = message.from_user.id
    
    # Verify admin
    if not is_admin(admin_id):
        bot.send_message(message.chat.id, "❌ Unauthorized")
        return
    
    try:
        # Extract telegram ID from message
        user_telegram_id = int(message.text.strip())
        
        # Check if user exists
        user_data, _ = get_user_by_telegram_id(user_telegram_id)
        
        if not user_data:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
            
            bot.send_message(
                message.chat.id,
                f"❌ <b>User Not Found</b>\n\n"
                f"No user exists with Telegram ID: <code>{user_telegram_id}</code>",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        # Prevent admin from deleting themselves
        if user_telegram_id == admin_id:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
            
            bot.send_message(
                message.chat.id,
                "⚠️ <b>Cannot Delete Yourself</b>\n\n"
                "You cannot delete your own admin account!",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        # Get user info for confirmation
        user_name = user_data.get('name', 'N/A')
        user_platform = user_data.get('platform', 'binance')
        user_status = user_data.get('status', 'unknown')
        
        # Platform display
        if user_platform == "binance":
            platform_display = "Binance"
        elif user_platform == "mt5":
            platform_display = "MT5"
        else:
            platform_display = "All Platforms"
        
        # ─────────────────────────────────────────────────
        # STEP 1: Stop any active trading sessions
        # ─────────────────────────────────────────────────
        username_key = f"user_{user_telegram_id}"
        try:
            from utils.bg_loop import loop
            import asyncio
            
            # Stop Binance futures trading
            if user_platform in ('binance', 'all'):
                try:
                    from handlers.trading_handler import is_trading_active, stop_futures_trading
                    if is_trading_active(username_key):
                        future = asyncio.run_coroutine_threadsafe(
                            stop_futures_trading(username_key), loop
                        )
                        future.result(timeout=5)
                        print(f"[ADMIN-DELETE] Stopped Binance futures for {user_telegram_id}")
                except Exception as e:
                    print(f"[ADMIN-DELETE] Error stopping Binance trading: {e}")
            
            # Stop MEXC trading
            if user_platform in ('mexc', 'all'):
                try:
                    from handlers.mexc_handler import is_mexc_trading_active, stop_mexc_trading
                    if is_mexc_trading_active(username_key):
                        future = asyncio.run_coroutine_threadsafe(
                            stop_mexc_trading(username_key), loop
                        )
                        future.result(timeout=5)
                        print(f"[ADMIN-DELETE] Stopped MEXC trading for {user_telegram_id}")
                except Exception as e:
                    print(f"[ADMIN-DELETE] Error stopping MEXC trading: {e}")
            
            # Stop MT5 trading
            if user_platform in ('mt5', 'all'):
                try:
                    from handlers.mt5_handler import is_mt5_trading_active, stop_mt5_trading
                    if is_mt5_trading_active(username_key):
                        future = asyncio.run_coroutine_threadsafe(
                            stop_mt5_trading(username_key), loop
                        )
                        future.result(timeout=5)
                        print(f"[ADMIN-DELETE] Stopped MT5 trading for {user_telegram_id}")
                except Exception as e:
                    print(f"[ADMIN-DELETE] Error stopping MT5 trading: {e}")
        except Exception as e:
            print(f"[ADMIN-DELETE] Error stopping trading sessions: {e}")
        
        # ─────────────────────────────────────────────────
        # STEP 2: Remove MetaAPI cloud account if exists
        # ─────────────────────────────────────────────────
        metaapi_account_id = user_data.get('metaapi_account_id')
        if metaapi_account_id and user_platform in ('mt5', 'all'):
            try:
                from mt5.metaapi_manager import remove_account
                from utils.bg_loop import loop
                import asyncio
                future = asyncio.run_coroutine_threadsafe(
                    remove_account(metaapi_account_id), loop
                )
                future.result(timeout=30)
                print(f"[ADMIN-DELETE] MetaAPI account {metaapi_account_id} removed for user {user_telegram_id}")
            except Exception as e:
                print(f"[ADMIN-DELETE] Error removing MetaAPI account: {e}")
        
        # ─────────────────────────────────────────────────
        # STEP 3: Clean up global state and caches
        # ─────────────────────────────────────────────────
        try:
            from utils.cleanup_utils import cleanup_all_user_sessions
            cleanup_all_user_sessions(username_key, user_telegram_id)
            print(f"[ADMIN-DELETE] Cleaned up all sessions for {user_telegram_id}")
        except Exception as e:
            print(f"[ADMIN-DELETE] Error cleaning up sessions: {e}")

        success = delete_user(user_telegram_id)
        
        if success:
            # Notify the deleted user
            try:
                bot.send_message(
                    user_telegram_id,
                    "⚠️ <b>Account Deleted</b>\n\n"
                    "Your account has been removed by an administrator.\n"
                    "All your data has been deleted.\n\n"
                    "If you believe this is an error, please contact support.",
                    parse_mode='HTML'
                )
            except:
                pass  # User might have blocked the bot
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
            
            bot.send_message(
                message.chat.id,
                f"✅ <b>User Deleted Successfully</b>\n\n"
                f"👤 <b>Name:</b> {user_name}\n"
                f"🆔 <b>Telegram ID:</b> <code>{user_telegram_id}</code>\n"
                f"📈 <b>Platform:</b> {platform_display}\n"
                f"📊 <b>Status:</b> {user_status}\n\n"
                f"The user has been permanently removed from the database.",
                parse_mode='HTML',
                reply_markup=markup
            )
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
            
            bot.send_message(
                message.chat.id,
                f"❌ <b>Deletion Failed</b>\n\n"
                f"Could not delete user with ID: <code>{user_telegram_id}</code>\n"
                f"Please try again or check the logs.",
                parse_mode='HTML',
                reply_markup=markup
            )
    
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
        
        bot.send_message(
            message.chat.id,
            "❌ <b>Invalid Input</b>\n\n"
            "Please enter a valid numeric Telegram ID.\n\n"
            "<i>Example: 123456789</i>",
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
        
        bot.send_message(
            message.chat.id,
            f"❌ <b>Error</b>\n\n{str(e)}",
            parse_mode='HTML',
            reply_markup=markup
        )


def back_to_admin_dashboard(bot, call):
    """Return to admin dashboard"""
    from handlers.start_handler import handle_start_command
    from handlers.welcome_messages import get_admin_welcome
    
    user_id = call.from_user.id
    user_name = call.from_user.first_name or "Admin"
    username = call.from_user.username or "N/A"
    
    # Verify admin
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        return
    
    bot.answer_callback_query(call.id)
    
    welcome_text, markup = get_admin_welcome(user_name, username, user_id)
    
    safe_edit(bot, call, welcome_text, reply_markup=markup)
