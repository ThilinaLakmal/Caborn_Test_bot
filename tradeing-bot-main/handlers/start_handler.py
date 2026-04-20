"""
Start command handler - manages user auto-login and welcome messages
"""
from user_control.add_users import is_admin, get_user_by_telegram_id, is_user_exists
from handlers.welcome_messages import (
    get_admin_welcome,
    get_active_user_welcome,
    get_pending_user_welcome,
    get_rejected_user_welcome,
    get_new_user_welcome
)


def handle_start_command(bot, message):
    """
    Handle /start command with auto-login functionality
    Displays appropriate welcome message based on user role and status
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    username = message.from_user.username or "N/A"
    
    # Check if user is admin
    if is_admin(user_id):
        welcome_text, markup = get_admin_welcome(user_name, username, user_id)
        bot.send_message(message.chat.id, welcome_text, parse_mode='HTML', reply_markup=markup)
        return
    
    # Check if user exists in database
    user_exists = is_user_exists(user_id)
    
    if user_exists:
        user_data, _ = get_user_by_telegram_id(user_id)
        status = user_data.get("status", "pending")
        platform = user_data.get("platform", "binance")
        
        # Use name from database if available
        db_name = user_data.get("name")
        if db_name:
            user_name = db_name
        
        if status == "active":
            # Check if user has active trading session
            from handlers.trading_handler import is_trading_active, get_trading_mode
            from handlers.mt5_handler import is_mt5_trading_active, get_mt5_trading_mode
            from handlers.mexc_handler import is_mexc_trading_active, get_mexc_trading_mode
            
            username_key = f"user_{user_id}"
            
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
        elif status == "pending":
            welcome_text, markup = get_pending_user_welcome(user_name, username, user_id)
        else:  # rejected
            welcome_text, markup = get_rejected_user_welcome(user_name, username, user_id)
    else:
        # New user - not registered yet
        welcome_text, markup = get_new_user_welcome(user_name, username, user_id)
    
    bot.send_message(message.chat.id, welcome_text, parse_mode='HTML', reply_markup=markup)
