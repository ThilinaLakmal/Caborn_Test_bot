"""
Cleanup utilities to prevent memory leaks.
Removes user sessions from global dictionaries when trading stops.
"""
import asyncio

def cleanup_binance_session(username_key):
    """
    Clean up Binance/futures trading session data for a user.
    Removes all references from global dictionaries.
    """
    from handlers.trading_handler import user_data, user_tasks
    
    # Cancel any active tasks
    if username_key in user_tasks:
        for task_name, task in user_tasks[username_key].items():
            if task and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        user_tasks[username_key].clear()
        del user_tasks[username_key]
    
    # Remove session data
    if username_key in user_data:
        del user_data[username_key]
    
    print(f"[CLEANUP] ✅ Cleaned up Binance session for {username_key}")


def cleanup_mexc_session(username_key):
    """
    Clean up MEXC trading session data for a user.
    Removes all references from global dictionaries.
    """
    from handlers.mexc_handler import mexc_user_data, mexc_user_tasks
    
    # Cancel any active tasks
    if username_key in mexc_user_tasks:
        for task_name, task in mexc_user_tasks[username_key].items():
            if task and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        mexc_user_tasks[username_key].clear()
        del mexc_user_tasks[username_key]
    
    # Remove session data
    if username_key in mexc_user_data:
        del mexc_user_data[username_key]
    
    print(f"[CLEANUP] ✅ Cleaned up MEXC session for {username_key}")


def cleanup_mt5_session(username_key):
    """
    Clean up MT5 trading session data for a user.
    Removes all references from global dictionaries.
    """
    from handlers.mt5_handler import mt5_user_data, mt5_user_tasks
    
    # Cancel any active tasks
    if username_key in mt5_user_tasks:
        for task_name, task in mt5_user_tasks[username_key].items():
            if task and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        mt5_user_tasks[username_key].clear()
        del mt5_user_tasks[username_key]
    
    # Remove session data
    if username_key in mt5_user_data:
        del mt5_user_data[username_key]
    
    print(f"[CLEANUP] ✅ Cleaned up MT5 session for {username_key}")


async def cleanup_metaapi_connections(telegram_id):
    """
    Clean up MetaAPI connections and related state for a user.
    """
    from mt5.metaapi_manager import (
        disconnect_user,
        _user_balance_state,
        _user_depletion_alert_sent,
        _user_recovery_alert_sent,
        _user_depletion_alert_timestamp,
        _user_connection_locks
    )
    
    try:
        # Disconnect MetaAPI connection
        await disconnect_user(telegram_id)
        
        # Clean up all state dictionaries
        _user_balance_state.pop(telegram_id, None)
        _user_depletion_alert_sent.pop(telegram_id, None)
        _user_recovery_alert_sent.pop(telegram_id, None)
        _user_depletion_alert_timestamp.pop(telegram_id, None)
        _user_connection_locks.pop(telegram_id, None)
        
        print(f"[CLEANUP] ✅ Cleaned up MetaAPI connection for telegram_id={telegram_id}")
    except Exception as e:
        print(f"[CLEANUP] ⚠️ Error cleaning MetaAPI: {e}")


def cleanup_crash_protection_data(telegram_id):
    """
    Clean up crash protection daily state for a user.
    Called when user stops trading to free up memory.
    """
    from trading.crash_protection import crash_protector
    from mt5.mt5_crash_protection import mt5_crash_protector
    
    try:
        # Clean up futures crash protection
        crash_protector._user_daily_start_balance.pop(telegram_id, None)
        crash_protector._user_daily_trade_count.pop(telegram_id, None)
        crash_protector._user_daily_start_time.pop(telegram_id, None)
        
        # Clean up MT5 crash protection
        mt5_crash_protector._user_daily_start_balance.pop(telegram_id, None)
        mt5_crash_protector._user_daily_trade_count.pop(telegram_id, None)
        mt5_crash_protector._user_daily_start_time.pop(telegram_id, None)
        
        print(f"[CLEANUP] ✅ Cleaned up crash protection data for telegram_id={telegram_id}")
    except Exception as e:
        print(f"[CLEANUP] ⚠️ Error cleaning crash protection: {e}")


def cleanup_registration_data(telegram_id):
    """
    Clean up registration temporary data for a user.
    """
    from handlers.registration_handler import registration_data
    
    try:
        if telegram_id in registration_data:
            del registration_data[telegram_id]
            print(f"[CLEANUP] ✅ Cleaned up registration data for telegram_id={telegram_id}")
    except Exception as e:
        print(f"[CLEANUP] ⚠️ Error cleaning registration data: {e}")


def cleanup_all_user_sessions(username_key, telegram_id):
    """
    Complete cleanup of all user data across all platforms and services.
    Call this when user logs out or stops trading.
    """
    print(f"[CLEANUP] 🧹 Starting full cleanup for {username_key}...")
    
    try:
        # Clean up platform sessions
        cleanup_binance_session(username_key)
        cleanup_mexc_session(username_key)
        cleanup_mt5_session(username_key)
        
        # Note: MetaAPI cleanup must be awaited, so it's called separately
        # cleanup_metaapi_connections(telegram_id)  # Must be called with await
        
        # Clean up state dictionaries
        cleanup_crash_protection_data(telegram_id)
        cleanup_registration_data(telegram_id)
        
        print(f"[CLEANUP] ✅ Full cleanup completed for {username_key}")
    except Exception as e:
        print(f"[CLEANUP] ❌ Error during full cleanup: {e}")
