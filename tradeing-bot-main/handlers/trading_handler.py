"""
Trading handler for managing futures trading operations
Contains the exact trading logic from the old bot
"""
import asyncio
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from requests.exceptions import ConnectTimeout

from utils.logging_service import logger
from trading.future_trading import (
    get_recent_prices_future,
    get_wallet_balance_future,
    find_support_level,
    find_resistance_level,
    get_trade_signal_advanced,
    close_position_future,
    get_real_active_futures_count,
    set_leverage_and_margin,
    validate_order_before_placement,
    place_order_with_retry,
    place_sl_tp_orders,
)
from config import (
    FUTURES_SYMBOLS_LIST as symbols_list,
    FUTURES_WIN_PERCENTAGE as win_percentage_future,
    FUTURES_LOSS_PERCENTAGE as loss_percentage_future,
    FUTURES_BREAKEVEN_TRIGGER_PCT as breakeven_trigger_pct,
    FUTURES_TRAILING_TRIGGER_PCT as trailing_trigger_pct,
    FUTURES_TRAILING_STOP_PCT as trailing_percentage,
    FUTURES_USE_TRAILING_STOP as use_trailing_stop,
    FUTURES_MAX_CONCURRENT_TRADES as MAX_CONCURRENT_TRADES,
    FUTURES_WALLET_PERCENTAGE,
    FUTURES_LEVERAGE,
    FUTURES_MIN_BALANCE,
    FUTURES_MARGIN_TYPE,
    CRASH_COOLDOWN_MINUTES,
)
from trading.crash_protection import crash_protector

# Global storage for user data and tasks
user_data = {}
user_tasks = {}


def reset_trade_state(trade: dict):
    """Reset per-symbol runtime state so old SL/trailing data can't leak."""
    trade["holding_position"] = False
    trade["entry_price"] = 0
    trade["trade_quantity"] = 0
    trade["stop_loss_price"] = 0
    trade["highest_price"] = 0
    trade["lowest_price"] = 0
    trade["stop_price"] = 0
    trade["take_profit_price"] = 0
    trade["final_tp_price"] = 0
    trade["trade_completed"] = False
    trade["position_type"] = None
    trade["tp_hit"] = False
    trade["breakeven_set"] = False
    trade["highest_profit_pct"] = 0.0


def is_trading_active(username_key):
    """Check if a user has an active trading session"""
    if username_key not in user_data:
        return False
    return user_data[username_key].get("bot_status") == "Running"


def get_trading_mode(username_key):
    """Get the current trading mode for a user"""
    if username_key not in user_data:
        return None
    return user_data[username_key].get("trading_mode")


def _get_fresh_binance_client(username_key):
    """Get fresh Binance client for user (no caching)"""
    from trading.client_factory import get_binance_client
    if username_key not in user_data:
        return None
    api_key = user_data[username_key].get("api_key")
    api_secret = user_data[username_key].get("api_secret")
    return get_binance_client(api_key, api_secret)


def initialize_user_session(username_key, telegram_id, api_key, api_secret, coins_list):
    """Initialize a new user trading session"""
    from trading.client_factory import get_binance_client
    
    # RESET crash mode on new session (fresh start after restart)
    crash_protector.reset_crash_mode()
    print(f"[SESSION] 🔄 Crash protection reset for fresh trading session")
    
    # Create the Binance client
    client = get_binance_client(api_key, api_secret)
    
    user_data[username_key] = {
        "telegram_id": telegram_id,
        "api_key": api_key,
        "api_secret": api_secret,
        "client": client,
        "bot_running": False,
        "trade_log": "",
        "error_log": "",
        "trading_mode": None,
        "position_found": False,
        "bot_status": "Not Running",
        "lock": asyncio.Lock(),
        "coins": coins_list,
        "start_bot_cmd": False,
        "active_coins": [],
        "remaining_coins": [],
        "wallet_balance": 0,
        "crash_notification_sent": False,  # Track if crash notification already sent
        "trades": {
            coin: {
                "holding_position": False,
                "entry_price": 0,
                "trade_quantity": 0,
                "stop_loss_price": 0,
                "highest_price": 0,
                "lowest_price": 0,
                "stop_price": 0,
                "take_profit_price": 0,
                "final_tp_price": 0,
                "trade_completed": False,
                "position_type": None,
                "tp_hit": False,
                "breakeven_set": False,
                "highest_profit_pct": 0.0,
            }
            for coin in coins_list
        },
    }
    
    print(f"[SESSION] ✅ Initialized session for {username_key}")
    # Log session initialization
    logger.log_user_action(
        user_id=telegram_id,
        action="SESSION_INIT",
        description=f"Binance futures session initialized with {len(coins_list)} coins",
        status="SUCCESS"
    )
    return user_data[username_key]


async def start_futures_trading(bot, telegram_id, username_key):
    """Start the futures trading loop for a user"""
    print(f"[TRADING] 🚀 Starting futures trading for {username_key}")
    
    if username_key not in user_data:
        print(f"[TRADING] ❌ No session found for {username_key}")
        logger.log_error(
            user_id=telegram_id,
            error_type="SESSION_NOT_FOUND",
            error_message="No session found when starting futures trading",
            status_code=500
        )
        return False
    
    # Reset crash notification flag for fresh start
    user_data[username_key]["crash_notification_sent"] = False
    
    # Set trading mode
    user_data[username_key]["trading_mode"] = "Future Trading"
    user_data[username_key]["bot_status"] = "Running"
    
    # Start the trading loop
    if username_key not in user_tasks:
        user_tasks[username_key] = {}
    
    # Create and store the task
    task = asyncio.create_task(
        trade_loop_future(username_key, bot, telegram_id)
    )
    user_tasks[username_key]["future"] = task
    
    print(f"[TRADING] ✅ Trading task created for {username_key}")
    logger.log_user_action(
        user_id=telegram_id,
        action="TRADING_START",
        description="Futures trading bot started",
        status="SUCCESS"
    )
    return True


async def stop_futures_trading(username_key):
    """Stop the futures trading loop and close ALL open positions on Binance"""
    print(f"[TRADING] 🛑 Stopping futures trading for {username_key}")

    if username_key in user_data:
        telegram_id = user_data[username_key].get("telegram_id")
        user_data[username_key]["bot_status"] = "Stopped"
        # Reset crash notification flag for next session
        user_data[username_key]["crash_notification_sent"] = False

        # Cancel running tasks first so the loop doesn't reopen anything
        if username_key in user_tasks:
            for task_name, task in user_tasks[username_key].items():
                if task and not task.done():
                    task.cancel()
                    print(f"[TRADING] Cancelled {task_name} task for {username_key}")
            user_tasks[username_key].clear()

        # Close ALL open positions on Binance
        client = _get_fresh_binance_client(username_key)
        closed_count = 0
        if client:
            try:
                positions = client.futures_position_information()
                for pos in positions:
                    amt = float(pos.get('positionAmt', 0))
                    if amt != 0:
                        symbol = pos['symbol']
                        pos_type = "LONG" if amt > 0 else "SHORT"
                        print(f"[TRADING] 🔄 Closing {pos_type} {symbol} (qty={abs(amt)})")
                        close_position_future(symbol, abs(amt), client, pos_type)
                        closed_count += 1
                print(f"[TRADING] ✅ All Binance positions closed for {username_key}")
                logger.log_user_action(
                    user_id=telegram_id,
                    action="TRADING_STOP",
                    description=f"Futures trading stopped, closed {closed_count} positions",
                    status="SUCCESS"
                )
            except Exception as e:
                print(f"[TRADING] ⚠️ Error closing Binance positions: {e}")
                logger.log_error(
                    user_id=telegram_id,
                    error_type="POSITION_CLOSE_ERROR",
                    error_message=str(e),
                    status_code=500
                )

        # Reset internal trade state
        for coin in user_data[username_key]["trades"]:
            reset_trade_state(user_data[username_key]["trades"][coin])
        user_data[username_key]["active_coins"].clear()
        
        # 🧹 CLEANUP: Remove session from memory to prevent accumulation
        from utils.cleanup_utils import cleanup_binance_session, cleanup_crash_protection_data
        cleanup_binance_session(username_key)
        cleanup_crash_protection_data(telegram_id)

        return True
    return False


async def trade_loop_future(username: str, bot, telegram_id: int):
    """
    EXACT trading loop from old bot with 2 max concurrent trades
    Simulates trading loop, running until stopped.
    """
    print(f"[TRADE-LOOP] 🚀 ENTERED trade_loop_future for {username} at {datetime.now()}")
    
    # Initialize last_test_message_time
    last_test_message_time = datetime.now()
    
    # Setup active coins
    print(f"[TRADE-LOOP] Step 11: Setting up active coins...")
    if not user_data[username]["active_coins"]:
        user_data[username]["active_coins"] = symbols_list.copy()
        print(f"[TRADE-LOOP] ✅ Active coins initialized with {len(user_data[username]['active_coins'])} coins: {user_data[username]['active_coins'][:5]}...")
    else:
        print(f"[TRADE-LOOP] ℹ️ Using existing active coins: {len(user_data[username]['active_coins'])} coins")
    
    print(f"[TRADE-LOOP] Step 12: 🔄 ENTERING MAIN TRADING LOOP")
    loop_count = 0
    
    while user_data[username]["bot_status"] == "Running":
        loop_count += 1
        
        # === CRASH PROTECTION: Check market health ===
        try:
            client = _get_fresh_binance_client(username)
            crash_result = crash_protector.check_for_crash(client)
            if crash_result.get('is_crashing'):
                print(f"[TRADE-LOOP] 🚨 CRASH DETECTED: {crash_result.get('reason')}")
                
                # Notify user about crash detection (once per hour)
                last_notification_time = user_data[username].get('last_crash_notification_sent', None)
                now = datetime.now()
                should_send = False
                
                if last_notification_time is None:
                    should_send = True
                else:
                    time_since_last = (now - last_notification_time).total_seconds()
                    if time_since_last >= 3600:  # 1 hour = 3600 seconds
                        should_send = True
                
                if should_send:
                    event_type = crash_result.get('event_type', 'crash')
                    drop_pct = crash_result.get('drop_pct', 0)
                    current_price = crash_result.get('current_price', 0)
                    window_high = crash_result.get('window_high', 0)
                    window_low = crash_result.get('window_low', 0)
                    
                    if event_type == 'pump':
                        title = "🚨 MARKET PUMP DETECTED - BINANCE TRADING HALTED"
                        desc = f"📈 <b>BTC pumped {drop_pct:.1f}%</b>\n💰 Price: ${window_low:.2f} → ${current_price:.2f}"
                    else:
                        title = "🚨 MARKET CRASH DETECTED - BINANCE TRADING HALTED"
                        desc = f"📊 <b>BTC dropped {drop_pct:.1f}%</b>\n💰 Price: ${window_high:.2f} → ${current_price:.2f}"
                    
                    try:
                        bot.send_message(
                            chat_id=telegram_id,
                            text=f"<b>{title}</b>\n"
                                 f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                 f"{desc}\n"
                                 f"⏱️ Timeframe: Last 1-Hour Chart (H1)\n\n"
                                 f"🛡️ <b>Actions Taken:</b>\n"
                                 f"✅ All positions closed\n"
                                 f"✅ Trading paused for {CRASH_COOLDOWN_MINUTES // 60} hours\n\n"
                                 f"⏳ <b>Cooldown:</b> {CRASH_COOLDOWN_MINUTES // 60}h active\n\n"
                                 f"💡 <b>To skip waiting:</b> Stop trading and restart from main menu.",
                            parse_mode='HTML'
                        )
                        user_data[username]['last_crash_notification_sent'] = now
                    except Exception as e:
                        print(f"[TRADE-LOOP] ⚠️ Failed to send crash notification: {e}")
                
                # Log crash detection
                logger.log_crash_detection(
                    user_id=telegram_id,
                    crash_type=crash_result.get('type', 'UNKNOWN'),
                    description=crash_result.get('reason', 'Market crash detected'),
                    severity="HIGH"
                )
                # Emergency close all open positions
                crash_protector.emergency_close_all(
                    client,
                    user_data, username, bot, telegram_id
                )
                # Wait for cooldown
                print(f"[TRADE-LOOP] ⏸️ Trading paused — crash cooldown active")
                await asyncio.sleep(60)  # Check every minute during cooldown
                continue
            
            # Check if trading is allowed (daily limits, cooldown, etc.)
            wallet_bal = get_wallet_balance_future(client)
            crash_protector.set_daily_start_balance(username, wallet_bal)
            trading_allowed, block_reason = crash_protector.is_trading_allowed(username, wallet_bal)

            # region agent log
            import json as _json, time as _time; open(r'D:/telegram/new_bot/debug-8958e4.log', 'a').write(_json.dumps({"sessionId": "8958e4", "timestamp": int(_time.time()*1000), "location": "trading_handler.py:350", "hypothesisId": "B", "message": "Binance crash/trading-allowed check", "data": {"username": username, "wallet_bal": wallet_bal, "trading_allowed": trading_allowed, "block_reason": block_reason, "crash_mode": crash_protector.crash_mode}}) + '\n')
            # endregion

            # SAFETY CHECK: If cooldown expired naturally, auto-reset crash mode
            if "Crash cooldown" in block_reason and crash_protector.crash_triggered_at:
                elapsed = (datetime.now() - crash_protector.crash_triggered_at).total_seconds() / 60
                if elapsed >= CRASH_COOLDOWN_MINUTES:
                    print(f"[TRADE-LOOP] 🔄 Crash cooldown expired naturally - resetting crash mode")
                    crash_protector.reset_crash_mode()
                    user_data[username]['last_crash_notification_sent'] = None  # Reset notification timestamp
                    trading_allowed, block_reason = crash_protector.is_trading_allowed(username, wallet_bal)
            
            if not trading_allowed:
                print(f"[TRADE-LOOP] ⛔ Trading blocked: {block_reason}")
                # Notify user about crash cooldown (throttled to 1 hour)
                if "Crash cooldown" in block_reason:
                    now = datetime.now()
                    last_cooldown_notif = user_data[username].get('last_cooldown_notification_sent')
                    should_notify_cooldown = False
                    
                    if not last_cooldown_notif or (now - last_cooldown_notif).total_seconds() >= 3600:
                        should_notify_cooldown = True
                    
                    if should_notify_cooldown:
                        try:
                            # Parse out remaining time from block_reason
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"<b>🚨 Binance Trading Status: PAUSED</b>\n"
                                     f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                                     f"⏸️ <b>Reason:</b> Market Crash Protection\n"
                                     f"📌 <b>Status:</b> {block_reason}\n\n"
                                     f"⏰ <i>Trading will automatically resume after the cooldown period.</i>",
                                parse_mode='HTML'
                            )
                            user_data[username]['last_cooldown_notification_sent'] = now
                        except Exception as e:
                            print(f"[TRADE-LOOP] ⚠️ Failed to send crash cooldown notification: {e}")
                await asyncio.sleep(30)
                continue
        except Exception as e:
            print(f"[TRADE-LOOP] ⚠️ Crash protection check error: {e}")
            logger.log_error(
                user_id=telegram_id,
                error_type="CRASH_CHECK_ERROR",
                error_message=str(e),
                status_code=500
            )
        
        # --- Sync internal holding_position flags with real Binance positions (BIDIRECTIONAL) ---
        try:
            open_positions = client.futures_position_information()
            open_symbols_data = {
                pos["symbol"]: pos 
                for pos in open_positions 
                if float(pos["positionAmt"]) != 0
            }
            open_symbols = set(open_symbols_data.keys())
            
            for coin, trade in user_data[username]["trades"].items():
                # Case 1: Internal says holding, but Binance doesn't have it (MANUALLY CLOSED!)
                if trade["holding_position"] and coin not in open_symbols:
                    print(f"[TRADE-LOOP] 🔄 ⚠️ POSITION CLOSED EXTERNALLY for {coin}! Resetting for new trades...")
                    reset_trade_state(trade)
                    
                    # CRITICAL: Remove from active_coins immediately to allow re-trading
                    if coin in user_data[username]["active_coins"]:
                        user_data[username]["active_coins"].remove(coin)
                        print(f"[TRADE-LOOP] ✅ Removed {coin} from active_coins - ready for new trades")
                
                # Case 2: Internal says NOT holding, but Binance DOES have it (after restart)
                elif not trade["holding_position"] and coin in open_symbols:
                    pos_data = open_symbols_data[coin]
                    entry_price = float(pos_data["entryPrice"])
                    position_amt = abs(float(pos_data["positionAmt"]))
                    position_side = "LONG" if float(pos_data["positionAmt"]) > 0 else "SHORT"
                    
                    # Load existing position into internal state
                    trade["holding_position"] = True
                    trade["entry_price"] = entry_price
                    trade["trade_quantity"] = position_amt
                    trade["position_type"] = position_side
                    trade["trade_completed"] = False
                    trade["tp_hit"] = False
                    trade["breakeven_set"] = False
                    trade["highest_profit_pct"] = 0.0
                    
                    # Set TP/SL based on leveraged strategy (account impact, not price impact)
                    if position_side == "LONG":
                        trade["take_profit_price"] = entry_price * (1 + (win_percentage_future / 100) / FUTURES_LEVERAGE)
                        trade["stop_loss_price"] = entry_price * (1 - (loss_percentage_future / 100) / FUTURES_LEVERAGE)
                    else:
                        trade["take_profit_price"] = entry_price * (1 - (win_percentage_future / 100) / FUTURES_LEVERAGE)
                        trade["stop_loss_price"] = entry_price * (1 + (loss_percentage_future / 100) / FUTURES_LEVERAGE)
                    
                    if coin not in user_data[username]["active_coins"]:
                        user_data[username]["active_coins"].append(coin)
                    
                    print(f"[TRADE-LOOP] 🔄 Loaded existing {position_side} position for {coin} from Binance (Entry: {entry_price}, Qty: {position_amt})")
        except Exception as e:
            print(f"[TRADE-LOOP] ⚠️ Error syncing positions: {e}")

        # --- Enforce ISOLATED + correct leverage on ALL open positions ---
        try:
            client = _get_fresh_binance_client(username)
            for coin, trade in user_data[username]["trades"].items():
                if not trade["holding_position"]:
                    continue
                try:
                    client.futures_change_leverage(symbol=coin, leverage=FUTURES_LEVERAGE)
                except Exception as e:
                    err = str(e)
                    if "No need to change" not in err:
                        print(f"[ENFORCE] ⚠️ Could not set leverage for {coin}: {err}")
                try:
                    client.futures_change_margin_type(symbol=coin, marginType=FUTURES_MARGIN_TYPE.upper())
                except Exception as e:
                    err = str(e)
                    if "No need to change margin type" not in err:
                        print(f"[ENFORCE] ⚠️ Could not set ISOLATED margin for {coin}: {err}")
        except Exception as e:
            print(f"[ENFORCE] ⚠️ Error during margin/leverage enforcement loop: {e}")

        # ✅ IMPROVEMENT: Use REAL Binance position count (not internal state)
        try:
            client = _get_fresh_binance_client(username)
            real_active_trades_count = get_real_active_futures_count(client)
        except Exception as e:
            print(f"[TRADE-LOOP] ⚠️ Could not get real trade count from Binance: {e}. Falling back to internal count.")
            real_active_trades_count = sum(
                1 for c in user_data[username]["trades"] 
                if user_data[username]["trades"][c]["holding_position"]
            )
        
        print(f"[TRADE-LOOP] 🔄 Loop iteration #{loop_count} started at {datetime.now()}")
        print(f"[TRADE-LOOP] 📊 Active trades: {real_active_trades_count}/{MAX_CONCURRENT_TRADES} (limit) [REAL Binance count]")
        
        # --- Replenish active_coins if below MAX_CONCURRENT_TRADES ---
        # ✅ CRITICAL FIX: Replenish to match AVAILABLE TRADE SLOTS, not internal list length
        analysis_slots_available = MAX_CONCURRENT_TRADES - real_active_trades_count
        
        if analysis_slots_available > 0:
            # Clean up closed positions from active_coins to free up space
            coins_to_remove = [
                c for c in user_data[username]["active_coins"]
                if not user_data[username]["trades"][c]["holding_position"] and user_data[username]["trades"][c]["trade_completed"]
            ]
            for coin in coins_to_remove:
                user_data[username]["active_coins"].remove(coin)
                print(f"[TRADE-LOOP] 🧹 Cleaned up completed coin {coin} from active_coins")
            
            # Get coins that are ready to trade (NOT currently holding OR completed)
            available_coins = [
                c for c in symbols_list
                if c not in user_data[username]["active_coins"]  # Not already being analyzed
                and not user_data[username]["trades"][c]["holding_position"]  # Not currently in a position
            ]
            
            # Shuffle to avoid always trading same coins
            import random
            random.shuffle(available_coins)
            
            # Add coins to fill available TRADE SLOTS (not previous list length)
            needed_coins = analysis_slots_available - len([c for c in user_data[username]["active_coins"] if not user_data[username]["trades"][c]["holding_position"]])
            
            if needed_coins > 0:
                print(f"[TRADE-LOOP] 📊 {analysis_slots_available} trade slots available - need {needed_coins} fresh coins for analysis")
                while needed_coins > 0 and available_coins:
                    next_coin = available_coins.pop(0)
                    user_data[username]["active_coins"].append(next_coin)
                    print(f"[TRADE-LOOP] ➕ Added {next_coin} to active_coins (slot {MAX_CONCURRENT_TRADES - analysis_slots_available + 1}/{MAX_CONCURRENT_TRADES}). (Available: {len(available_coins)} more)")
                    needed_coins -= 1
            else:
                print(f"[TRADE-LOOP] ℹ️ Active coins already filled for analysis slots")
        
        # --- Process each active coin ---
        for coin_index, coin in enumerate(list(user_data[username]["active_coins"]), 1):
            if user_data[username]["bot_status"] != "Running":
                break
                
            print(f"[TRADE-LOOP] Processing coin {coin_index}/{len(user_data[username]['active_coins'])}: {coin}")
            
            try:
                await asyncio.sleep(2)
                
                # Periodic status message
                now = datetime.now()
                if (now - last_test_message_time).total_seconds() > 3600:
                    print(f"[TRADE-LOOP] 📨 Sending periodic status message...")
                    try:
                        client_status = _get_fresh_binance_client(username)
                        bal_status = get_wallet_balance_future(client_status)
                        active_status = get_real_active_futures_count(client_status)
                        bot.send_message(
                            chat_id=telegram_id,
                            text=f"📊 <b>Binance Hourly Update</b>\n"
                                 f"💰 Balance: <code>{bal_status:.2f} USDT</code>\n"
                                 f"📈 Active Trades: {active_status}/{MAX_CONCURRENT_TRADES}",
                            parse_mode="HTML"
                        )
                        last_test_message_time = now
                        print(f"[TRADE-LOOP] ✅ Status message sent")
                    except Exception as e:
                        print(f"[TRADE-LOOP] ⚠️ Failed to send status message: {e}")
                
                client = _get_fresh_binance_client(username)
                recent_prices = get_recent_prices_future(
                    coin,
                    client=client
                )
                await asyncio.sleep(1)
                
                if recent_prices is None or len(recent_prices) == 0:
                    print(f"[TRADE-LOOP] ⚠️ Could not fetch prices for {coin}. Skipping.")
                    continue

                current_price = recent_prices[-1][0]
                try:
                    user_data[username]["wallet_balance"] = get_wallet_balance_future(client)
                except Exception as e:
                    print(f"[TRADE-LOOP] ⚠️ Error getting wallet balance for {coin}: {e}")
                    continue
                
                trade_data = user_data[username]["trades"][coin]
                
                # === OPENING NEW POSITIONS ===
                if not trade_data["holding_position"]:
                    print(f"[TRADE-LOOP] 📍 {coin} - No position held, checking entry conditions...")
                    
                    # Check if we've hit the MAX_CONCURRENT_TRADES limit using REAL Binance positions
                    try:
                        real_active_count = get_real_active_futures_count(client)
                    except Exception as e:
                        print(f"[TRADE-LOOP] ⚠️ Error getting active futures count: {e}")
                        continue
                    
                    if real_active_count >= MAX_CONCURRENT_TRADES:
                        print(f"[TRADE-LOOP] ⚠️ Maximum {MAX_CONCURRENT_TRADES} trades already active on Binance ({real_active_count}). Skipping {coin}")
                        continue
                    
                    # Check if position already exists on Binance
                    try:
                        open_positions = client.futures_position_information()
                    except Exception as e:
                        print(f"[TRADE-LOOP] ⚠️ Error getting futures position information for {coin}: {e}")
                        continue

                    user_data[username]["position_found"] = False
                    
                    for position in open_positions:
                        if position["symbol"] == coin and position["positionAmt"] != "0":
                            user_data[username]["position_found"] = True
                            break
                    
                    if user_data[username]["position_found"]:
                        print(f"[TRADE-LOOP] ⚠️ Position already exists for {coin} on Binance")
                        if coin in user_data[username]["active_coins"]:
                            user_data[username]["trades"][coin]["holding_position"] = False
                            user_data[username]["trades"][coin]["trade_completed"] = True
                            user_data[username]["active_coins"].remove(coin)
                            print(f"[TRADE-MANAGEMENT] ⚠️ Removed {coin} from active_coins (position already exists)")
                        continue
                    
                    # Calculate support/resistance levels
                    support_level = find_support_level(
                        coin=coin,
                        client=client
                    )
                    await asyncio.sleep(0.1)
                    
                    resistance_level = find_resistance_level(
                        coin=coin,
                        client=client
                    )
                    await asyncio.sleep(0.1)

                    # ==========================================
                    # ADVANCED SIGNAL GENERATION
                    # Uses RSI + Trend + Momentum + Volume
                    # ==========================================
                    signal, signal_details = get_trade_signal_advanced(
                        api_key=user_data[username]["api_key"],
                        api_secret=user_data[username]["api_secret"],
                        coin=coin,
                        current_price=current_price,
                        support=support_level,
                        resistance=resistance_level
                    )
                    await asyncio.sleep(0.1)

                    # Set conditions based on advanced signal
                    long_condition = (signal == "LONG")
                    short_condition = (signal == "SHORT")

                    # RSI / signal summary — shown for every scanned coin
                    gap_percent = (resistance_level - support_level) / support_level
                    _fmt = lambda v, d=1: f"{v:.{d}f}" if isinstance(v, (int, float)) else str(v)
                    print(f"[SCAN] {coin} @ ${current_price:.4f} | RSI: {_fmt(signal_details.get('rsi'))} | StochRSI %K: {_fmt(signal_details.get('stoch_rsi_k'))} %D: {_fmt(signal_details.get('stoch_rsi_d'))} | Crossover: {signal_details.get('crossover', 'NONE')} | Signal: {signal}")
                    print(f"  S/R: ${support_level:.4f} / ${resistance_level:.4f} | Gap: {gap_percent*100:.1f}% | Trend: {signal_details.get('trend', 'N/A')} | Momentum: {signal_details.get('momentum', 'N/A')} | Vol: {_fmt(signal_details.get('volume_ratio', 1.0), 2)}x")
                    if signal_details.get('reasons'):
                        print(f"  Reasons: {', '.join(signal_details.get('reasons', []))}")

                    # CRITICAL: Check if support and resistance have enough gap
                    # If they're too close, price is ranging and both LONG and SHORT could trigger
                    min_gap_percent = 0.005  # Require at least 0.5% gap (lowered for altcoins)

                    if gap_percent < min_gap_percent:
                        print(f"[TRADE-LOOP] ⚠️ Gap too small for {coin}: {gap_percent*100:.2f}% (need >{min_gap_percent*100:.1f}%). Skipping ranging market.")
                        continue

                    # Only proceed if either condition is met
                    if not long_condition and not short_condition:
                        print(f"[TRADE-LOOP] ⏭️ No entry signal for {coin}. Moving to next coin.")
                        continue
                    
                    # Get exchange info for quantity calculation
                    try:
                        exchange_info = client.futures_exchange_info()
                        min_notional = None
                        lot_size = None
                        max_qty = None
                        
                        for symbol_info in exchange_info["symbols"]:
                            if symbol_info["symbol"] == coin:
                                filters = symbol_info["filters"]
                                min_notional = float(
                                    next(filter(lambda x: x["filterType"] == "MIN_NOTIONAL", filters))["notional"]
                                )
                                lot_size_filter = next(filter(lambda x: x["filterType"] == "LOT_SIZE", filters))
                                lot_size = float(lot_size_filter["stepSize"])
                                max_qty = float(lot_size_filter.get("maxQty", 0)) or None
                                break
                        
                        if min_notional is None or lot_size is None:
                            print(f"[TRADE-LOOP] ⚠️ Could not get exchange info for {coin}")
                            continue
                        
                    except Exception as e:
                        print(f"[TRADE-LOOP] ❌ Error getting exchange info for {coin}: {e}")
                        continue
                    
                    # Calculate quantity based on wallet balance (10% of futures wallet)
                    quantity = None
                    
                    wallet_balance = user_data[username]["wallet_balance"]
                    
                    if wallet_balance < FUTURES_MIN_BALANCE:
                        print(f"[TRADE-LOOP] ⚠️ Wallet balance ${wallet_balance:.2f} too low (min: ${FUTURES_MIN_BALANCE})")
                    else:
                        # Fixed position size — always 10% of wallet, never adjusted for volatility
                        position_pct = FUTURES_WALLET_PERCENTAGE
                        
                        # Position size = wallet_balance * position_pct% * leverage / price
                        position_value = wallet_balance * (position_pct / 100)
                        quantity = (position_value * FUTURES_LEVERAGE) / current_price
                        quantity = (quantity // lot_size) * lot_size
                        
                        # Cap quantity to exchange's maxQty limit (avoids "Quantity greater than max quantity")
                        if max_qty and quantity > max_qty:
                            print(f"[TRADE-LOOP] ⚠️ Quantity {quantity} exceeds maxQty {max_qty} for {coin}. Capping to maxQty.")
                            quantity = (max_qty // lot_size) * lot_size
                        
                        notional_value = current_price * quantity
                        
                        print(f"[TRADE-LOOP] 💵 Position sizing: {position_pct:.1f}% of ${wallet_balance:.2f} = ${position_value:.2f} x {FUTURES_LEVERAGE}x leverage")
                        print(f"[TRADE-LOOP] 💵 Quantity: {quantity}, Notional: ${notional_value:.2f}")
                        print(f"[TRADE-LOOP] 💵 Expected margin required: ${notional_value/FUTURES_LEVERAGE:.2f} (isolated mode)")
                        
                        if notional_value < min_notional:
                            print(f"[TRADE-LOOP] ⚠️ Notional ${notional_value:.2f} < min ${min_notional}. Trying min notional...")
                            # Try to use minimum notional as fallback
                            quantity = (min_notional * 1.05) / current_price
                            quantity = (quantity // lot_size) * lot_size
                            notional_value = current_price * quantity
                            if notional_value < min_notional or (notional_value / FUTURES_LEVERAGE) > wallet_balance * 0.5:
                                print(f"[TRADE-LOOP] ⚠️ Cannot meet min notional for {coin}")
                                quantity = None
                            else:
                                user_data[username]["trades"][coin]["trade_quantity"] = quantity
                        else:
                            user_data[username]["trades"][coin]["trade_quantity"] = quantity
                    
                    if quantity is None:
                        print(f"[TRADE-LOOP] ⏭️ Cannot calculate valid quantity for {coin}. Moving to next coin.")
                        continue
                    
                    # Execute LONG position if conditions met
                    if long_condition:
                        print(f"[TRADE-LOOP] 🚀 Attempting to open LONG position for {coin}...")
                        
                        # ===== CRITICAL: VALIDATE ORDER BEFORE PLACEMENT =====
                        is_valid, error_msg = validate_order_before_placement(
                            client,
                            coin,
                            user_data[username]["trades"][coin]["trade_quantity"],
                            current_price
                        )
                        
                        if not is_valid:
                            print(f"[TRADE-MANAGEMENT] ❌ Order validation failed for {coin}: {error_msg}")
                            if coin in user_data[username]["active_coins"]:
                                user_data[username]["active_coins"].remove(coin)
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"⚠️ <b>Validation Failed</b>\n\n"
                                     f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                     f"❌ <b>Error:</b> {error_msg}",
                                parse_mode='HTML'
                            )
                            continue
                        
                        try:
                            # HARD REQUIREMENT: Must confirm ISOLATED margin before placing order
                            lm_ok, lm_err = set_leverage_and_margin(
                                client, 
                                coin, 
                                FUTURES_LEVERAGE, 
                                FUTURES_MARGIN_TYPE
                            )
                            if not lm_ok:
                                print(f"[TRADE-MANAGEMENT] ❌ ISOLATED margin could not be confirmed for {coin}: {lm_err} — ABORTING trade to prevent CROSS mode order")
                                if coin in user_data[username]["active_coins"]:
                                    user_data[username]["active_coins"].remove(coin)
                                continue
                            
                            async with user_data[username]["lock"]:
                                # Use retry-enabled order placement
                                order, is_success, error_msg = await place_order_with_retry(
                                    client,
                                    coin,
                                    "BUY",
                                    user_data[username]["trades"][coin]["trade_quantity"],
                                    max_retries=3,
                                    backoff_seconds=2
                                )
                                
                                # CRITICAL: Verify order was actually placed
                                if not is_success or order is None:
                                    print(f"[TRADE-MANAGEMENT] ❌ Order placement failed for {coin}: {error_msg}")
                                    if coin in user_data[username]["active_coins"]:
                                        user_data[username]["active_coins"].remove(coin)
                                    # Log trade open failure
                                    logger.log_trade_open(
                                        user_id=telegram_id,
                                        exchange="BINANCE",
                                        symbol=coin,
                                        entry_price=current_price,
                                        quantity=user_data[username]["trades"][coin]["trade_quantity"],
                                        leverage=FUTURES_LEVERAGE,
                                        tp_price=current_price * (1 + (win_percentage_future / 100) / FUTURES_LEVERAGE),
                                        sl_price=current_price * (1 - (loss_percentage_future / 100) / FUTURES_LEVERAGE),
                                        error_msg=error_msg,
                                        status="FAILED",
                                        api_status_code=400
                                    )
                                    bot.send_message(
                                        chat_id=telegram_id,
                                        text=f"❌ <b>Order Failed (LONG)</b>\n\n"
                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                             f"📊 <b>Error:</b> {error_msg}",
                                        parse_mode='HTML'
                                    )
                                    continue
                                
                                user_data[username]["trades"][coin]["holding_position"] = True
                                user_data[username]["trades"][coin]["stop_price"] = 0
                                user_data[username]["trades"][coin]["tp_hit"] = False
                                user_data[username]["trades"][coin]["trade_completed"] = False
                                user_data[username]["trades"][coin]["breakeven_set"] = False
                                user_data[username]["trades"][coin]["highest_profit_pct"] = 0.0
                                user_data[username]["trades"][coin]["entry_price"] = current_price
                                user_data[username]["trades"][coin]["highest_price"] = current_price
                                user_data[username]["trades"][coin]["lowest_price"] = 0
                                user_data[username]["trades"][coin]["final_tp_price"] = current_price * (1 + (1 / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["take_profit_price"] = current_price * (1 + (win_percentage_future / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["stop_loss_price"] = current_price * (1 - (loss_percentage_future / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["position_type"] = "LONG"
                                
                                # Add to active_coins tracking
                                if coin not in user_data[username]["active_coins"]:
                                    user_data[username]["active_coins"].append(coin)
                                
                                print(f"[TRADE-LOOP] ✅ LONG opened for {coin}")

                                # Place SL/TP orders on Binance exchange
                                tp_ok, sl_ok, tp_err, sl_err = place_sl_tp_orders(
                                    client, coin, "LONG",
                                    user_data[username]["trades"][coin]["take_profit_price"],
                                    user_data[username]["trades"][coin]["stop_loss_price"]
                                )
                                if not tp_ok:
                                    print(f"[SL/TP] ⚠️ TP not placed on exchange for {coin}: {tp_err}")
                                if not sl_ok:
                                    print(f"[SL/TP] ⚠️ SL not placed on exchange for {coin}: {sl_err}")

                                # Log successful trade open
                                logger.log_trade_open(
                                    user_id=telegram_id,
                                    exchange="BINANCE",
                                    symbol=coin,
                                    entry_price=current_price,
                                    quantity=user_data[username]["trades"][coin]["trade_quantity"],
                                    leverage=FUTURES_LEVERAGE,
                                    tp_price=user_data[username]["trades"][coin]["take_profit_price"],
                                    sl_price=user_data[username]["trades"][coin]["stop_loss_price"],
                                    order_id=order.get("orderId") if order else None,
                                    status="SUCCESS",
                                    api_status_code=200
                                )
                                crash_protector.record_trade(username)
                                print(f"  Entry: {current_price}")
                                print(f"  TP: {user_data[username]['trades'][coin]['take_profit_price']}")
                                print(f"  SL: {user_data[username]['trades'][coin]['stop_loss_price']}")
                                print(f"  Quantity: {user_data[username]['trades'][coin]['trade_quantity']}")
                                
                                # Get real active count from Binance for notification
                                real_active = get_real_active_futures_count(client)
                                
                                bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"<b>🚀 LONG Position Opened</b>\n"
                                         f"━━━━━━━━━━━━━━━━━\n\n"
                                         f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                         f"📈 <b>Entry:</b> <code>{user_data[username]['trades'][coin]['entry_price']:.6f}</code>\n"
                                         f"🎯 <b>TP:</b> <code>{user_data[username]['trades'][coin]['take_profit_price']:.6f}</code>\n"
                                         f"🛑 <b>SL:</b> <code>{user_data[username]['trades'][coin]['stop_loss_price']:.6f}</code>\n"
                                         f"📊 <b>Qty:</b> <code>{user_data[username]['trades'][coin]['trade_quantity']:.4f}</code>\n"
                                         f"🔢 <b>Active Trades:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                    parse_mode='HTML'
                                )
                        except Exception as e:
                            print(f"[TRADE-MANAGEMENT] ❌ Failed to open LONG for {coin}: {e}")
                            if coin in user_data[username]["active_coins"]:
                                user_data[username]["trades"][coin]["holding_position"] = False
                                user_data[username]["trades"][coin]["trade_completed"] = True
                                user_data[username]["active_coins"].remove(coin)
                    
                    # Execute SHORT position if conditions met
                    elif short_condition:
                        print(f"[TRADE-LOOP] 🔻 Attempting to open SHORT position for {coin}...")
                        
                        # ===== CRITICAL: VALIDATE ORDER BEFORE PLACEMENT =====
                        is_valid, error_msg = validate_order_before_placement(
                            client,
                            coin,
                            user_data[username]["trades"][coin]["trade_quantity"],
                            current_price
                        )
                        
                        if not is_valid:
                            print(f"[TRADE-MANAGEMENT] ❌ Order validation failed for {coin}: {error_msg}")
                            if coin in user_data[username]["active_coins"]:
                                user_data[username]["active_coins"].remove(coin)
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"⚠️ <b>Validation Failed</b>\n\n"
                                     f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                     f"❌ <b>Error:</b> {error_msg}",
                                parse_mode='HTML'
                            )
                            continue
                        
                        try:
                            # HARD REQUIREMENT: Must confirm ISOLATED margin before placing order
                            lm_ok, lm_err = set_leverage_and_margin(
                                client, 
                                coin, 
                                FUTURES_LEVERAGE, 
                                FUTURES_MARGIN_TYPE
                            )
                            if not lm_ok:
                                print(f"[TRADE-MANAGEMENT] ❌ ISOLATED margin could not be confirmed for {coin}: {lm_err} — ABORTING trade to prevent CROSS mode order")
                                if coin in user_data[username]["active_coins"]:
                                    user_data[username]["active_coins"].remove(coin)
                                continue
                            
                            async with user_data[username]["lock"]:
                                # Use retry-enabled order placement
                                order, is_success, error_msg = await place_order_with_retry(
                                    client,
                                    coin,
                                    "SELL",
                                    user_data[username]["trades"][coin]["trade_quantity"],
                                    max_retries=3,
                                    backoff_seconds=2
                                )
                                
                                # CRITICAL: Verify order was actually placed
                                if not is_success or order is None:
                                    print(f"[TRADE-MANAGEMENT] ❌ Order placement failed for {coin}: {error_msg}")
                                    if coin in user_data[username]["active_coins"]:
                                        user_data[username]["active_coins"].remove(coin)
                                    bot.send_message(
                                        chat_id=telegram_id,
                                        text=f"❌ <b>Order Failed (SHORT)</b>\n\n"
                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                             f"📊 <b>Error:</b> {error_msg}",
                                        parse_mode='HTML'
                                    )
                                    continue
                                
                                user_data[username]["trades"][coin]["holding_position"] = True
                                user_data[username]["trades"][coin]["stop_price"] = 0
                                user_data[username]["trades"][coin]["tp_hit"] = False
                                user_data[username]["trades"][coin]["trade_completed"] = False
                                user_data[username]["trades"][coin]["breakeven_set"] = False
                                user_data[username]["trades"][coin]["highest_profit_pct"] = 0.0
                                user_data[username]["trades"][coin]["entry_price"] = current_price
                                user_data[username]["trades"][coin]["lowest_price"] = current_price
                                user_data[username]["trades"][coin]["highest_price"] = 0
                                user_data[username]["trades"][coin]["final_tp_price"] = current_price * (1 - (1 / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["take_profit_price"] = current_price * (1 - (win_percentage_future / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["stop_loss_price"] = current_price * (1 + (loss_percentage_future / 100) / FUTURES_LEVERAGE)
                                user_data[username]["trades"][coin]["position_type"] = "SHORT"
                                
                                # Add to active_coins tracking
                                if coin not in user_data[username]["active_coins"]:
                                    user_data[username]["active_coins"].append(coin)
                                
                                print(f"[TRADE-LOOP] ✅ SHORT opened for {coin}")

                                # Place SL/TP orders on Binance exchange
                                tp_ok, sl_ok, tp_err, sl_err = place_sl_tp_orders(
                                    client, coin, "SHORT",
                                    user_data[username]["trades"][coin]["take_profit_price"],
                                    user_data[username]["trades"][coin]["stop_loss_price"]
                                )
                                if not tp_ok:
                                    print(f"[SL/TP] ⚠️ TP not placed on exchange for {coin}: {tp_err}")
                                if not sl_ok:
                                    print(f"[SL/TP] ⚠️ SL not placed on exchange for {coin}: {sl_err}")

                                crash_protector.record_trade(username)
                                print(f"  Entry: {current_price}")
                                print(f"  TP: {user_data[username]['trades'][coin]['take_profit_price']}")
                                print(f"  SL: {user_data[username]['trades'][coin]['stop_loss_price']}")
                                print(f"  Quantity: {user_data[username]['trades'][coin]['trade_quantity']}")
                                
                                # Get real active count from Binance for notification
                                real_active = get_real_active_futures_count(client)
                                
                                bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"<b>🔻 SHORT Position Opened</b>\n"
                                         f"━━━━━━━━━━━━━━━━━\n\n"
                                         f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                         f"📉 <b>Entry:</b> <code>{user_data[username]['trades'][coin]['entry_price']:.6f}</code>\n"
                                         f"🎯 <b>TP:</b> <code>{user_data[username]['trades'][coin]['take_profit_price']:.6f}</code>\n"
                                         f"🛑 <b>SL:</b> <code>{user_data[username]['trades'][coin]['stop_loss_price']:.6f}</code>\n"
                                         f"📊 <b>Qty:</b> <code>{user_data[username]['trades'][coin]['trade_quantity']:.4f}</code>\n"
                                         f"🔢 <b>Active Trades:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                    parse_mode='HTML'
                                )
                        except Exception as e:
                            print(f"[TRADE-MANAGEMENT] ❌ Failed to open SHORT for {coin}: {e}")
                            if coin in user_data[username]["active_coins"]:
                                user_data[username]["trades"][coin]["holding_position"] = False
                                user_data[username]["trades"][coin]["trade_completed"] = True
                                user_data[username]["active_coins"].remove(coin)
                
                # === MANAGING OPEN POSITIONS ===
                else:
                    print(f"[TRADE-LOOP] 📈 Managing open position for {coin}...")
                    try:
                        current_sl = user_data[username]["trades"][coin]["stop_loss_price"]
                        current_tp = user_data[username]["trades"][coin]["take_profit_price"]
                        entry_price = user_data[username]["trades"][coin]["entry_price"]
                        position_type = user_data[username]["trades"][coin].get("position_type", "LONG")
                        
                        print(f"[TRADE-LOOP] 📊 {coin} ({position_type}) - Price: {current_price}, TP: {current_tp}, SL: {current_sl}")
                        
                        # LONG position management
                        if position_type == "LONG":
                            # Calculate current profit percentage
                            current_profit_pct = ((current_price - entry_price) / entry_price) * 100
                            print(f"[TRADE-LOOP] 💰 {coin} LONG P/L: {current_profit_pct:.2f}%")
                            
                            # Track highest profit for trailing stop
                            if current_profit_pct > user_data[username]["trades"][coin].get("highest_profit_pct", 0):
                                user_data[username]["trades"][coin]["highest_profit_pct"] = current_profit_pct
                            
                            # === BREAKEVEN LOGIC ===
                            # When profit hits breakeven_trigger_pct, move SL to entry price
                            if current_profit_pct >= breakeven_trigger_pct and not user_data[username]["trades"][coin].get("breakeven_set", False):
                                new_sl = entry_price * 1.001  # Slightly above entry for fees
                                user_data[username]["trades"][coin]["stop_loss_price"] = new_sl
                                user_data[username]["trades"][coin]["breakeven_set"] = True
                                current_sl = new_sl
                                print(f"[TRADE-LOOP] ✅ {coin} BREAKEVEN SET | New SL: {new_sl:.6f}")
                                bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"🔒 <b>Breakeven Set (LONG)</b>\n\n"
                                         f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                         f"📊 <b>Profit:</b> <code>{current_profit_pct:.2f}%</code>\n"
                                         f"🛑 <b>New SL:</b> <code>${new_sl:.6f}</code>\n\n"
                                         f"<i>Stop loss moved to breakeven.</i>",
                                    parse_mode='HTML'
                                )
                            
                            # === TRAILING STOP LOGIC ===
                            # When profit hits trailing_trigger_pct, start trailing
                            if current_profit_pct >= trailing_trigger_pct:
                                user_data[username]["trades"][coin]["tp_hit"] = True
                                # Calculate trailing SL: lock in (peak_profit - trailing_percentage)
                                peak_profit = user_data[username]["trades"][coin].get("highest_profit_pct", current_profit_pct)
                                locked_profit_pct = peak_profit - trailing_percentage
                                new_trailing_sl = entry_price * (1 + locked_profit_pct / 100)
                                
                                # Only move SL up, never down
                                if new_trailing_sl > current_sl:
                                    user_data[username]["trades"][coin]["stop_loss_price"] = new_trailing_sl
                                    user_data[username]["trades"][coin]["stop_price"] = new_trailing_sl
                                    current_sl = new_trailing_sl
                                    print(f"[TRADE-LOOP] 📈 {coin} TRAILING SL: {new_trailing_sl:.6f} | Locking {locked_profit_pct:.1f}%")
                            
                            current_sl = user_data[username]["trades"][coin]["stop_loss_price"]
                            current_tp = user_data[username]["trades"][coin]["take_profit_price"]
                            
                            # Determine if we should close the position
                            should_close = False
                            close_reason = ""
                            
                            # Stop loss hit (including breakeven/trailing SL)
                            if current_price <= current_sl:
                                should_close = True
                                close_reason = "STOP_LOSS"
                            # Take profit hit at win_percentage_future (45%)
                            elif current_price >= current_tp:
                                should_close = True
                                close_reason = "TAKE_PROFIT"
                            
                            if should_close:
                                print(f"[TRADE-LOOP] 🛑 Exit condition met for LONG {coin}")
                                # Check if position still exists
                                open_positions = client.futures_position_information()
                                user_data[username]["position_found"] = False
                                
                                for position in open_positions:
                                    if position["symbol"] == coin and position["positionAmt"] != "0":
                                        user_data[username]["position_found"] = True
                                        break
                                
                                if not user_data[username]["position_found"]:
                                    print(f"[TRADE-LOOP] ⚠️ Position not found on Binance - manually closed")
                                    # Position manually closed
                                    if coin in user_data[username]["active_coins"]:
                                        user_data[username]["trades"][coin]["holding_position"] = False
                                        user_data[username]["trades"][coin]["trade_completed"] = True
                                        user_data[username]["trades"][coin]["stop_price"] = 0
                                        user_data[username]["active_coins"].remove(coin)
                                        
                                        active_count = sum(
                                            1 for c in user_data[username]['trades']
                                            if user_data[username]['trades'][c]['holding_position']
                                        )
                                        
                                        profit_loss = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
                                        
                                        bot.send_message(
                                            chat_id=telegram_id,
                                            text=f"<b>🔔 MANUAL CLOSE (LONG)</b>\n"
                                                 f"━━━━━━━━━━━━━━━━━\n\n"
                                                 f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                 f"📈 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                 f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                 f"{('🟢' if profit_loss >= 0 else '🔴')} <b>P/L:</b> <code>{profit_loss:.2f}%</code>\n"
                                                 f"🔢 <b>Active:</b> {active_count}/{MAX_CONCURRENT_TRADES}",
                                            parse_mode='HTML'
                                        )
                                        
                                        print(f"[TRADE-MANAGEMENT] 🔄 Manual LONG close detected for {coin}. Active: {active_count}/{MAX_CONCURRENT_TRADES}")
                                else:
                                    print(f"[TRADE-LOOP] 🔄 Closing LONG position for {coin} via API...")
                                    # Close position via API
                                    try:
                                        async with user_data[username]["lock"]:
                                            order = close_position_future(
                                                coin,
                                                user_data[username]["trades"][coin]["trade_quantity"],
                                                client,
                                                "LONG"
                                            )
                                            
                                            if order:
                                                user_data[username]["trades"][coin]["holding_position"] = False
                                                user_data[username]["trades"][coin]["trade_completed"] = True
                                                user_data[username]["trades"][coin]["stop_price"] = 0
                                                
                                                # Calculate P&L
                                                profit_loss_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
                                                profit_loss_amount = (user_data[username]["trades"][coin]["trade_quantity"] * (current_price - entry_price))
                                                
                                                # Log trade close
                                                logger.log_trade_close(
                                                    user_id=telegram_id,
                                                    exchange="BINANCE",
                                                    symbol=coin,
                                                    entry_price=entry_price,
                                                    exit_price=current_price,
                                                    profit_loss=profit_loss_amount,
                                                    profit_loss_pct=profit_loss_pct,
                                                    order_id=order.get("orderId") if order else None,
                                                    close_reason=close_reason,
                                                    status="SUCCESS",
                                                    api_status_code=200
                                                )
                                                
                                                # Get real active count from Binance
                                                real_active = get_real_active_futures_count(client)
                                                
                                                if current_price <= current_sl:
                                                    bot.send_message(
                                                        chat_id=telegram_id,
                                                        text=f"<b>🔴 STOP LOSS HIT (LONG)</b>\n"
                                                             f"━━━━━━━━━━━━━━━━━\n\n"
                                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                             f"📈 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                             f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                             f"🔴 <b>Loss:</b> <code>{((current_price - entry_price) / entry_price * 100):.2f}%</code>\n"
                                                             f"🔢 <b>Active:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                                        parse_mode='HTML'
                                                    )
                                                else:
                                                    bot.send_message(
                                                        chat_id=telegram_id,
                                                        text=f"<b>🟢 TAKE PROFIT HIT (LONG)</b>\n"
                                                             f"━━━━━━━━━━━━━━━━━\n\n"
                                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                             f"📈 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                             f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                             f"🟢 <b>Profit:</b> <code>{((current_price - entry_price) / entry_price * 100):.2f}%</code>\n"
                                                             f"🔢 <b>Active:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                                        parse_mode='HTML'
                                                    )
                                                
                                                await asyncio.sleep(2)
                                                
                                                if coin in user_data[username]["active_coins"]:
                                                    user_data[username]["active_coins"].remove(coin)
                                                
                                                print(f"[TRADE-MANAGEMENT] ✅ Auto-closed LONG for {coin}. Active: {real_active}/{MAX_CONCURRENT_TRADES}")
                                            else:
                                                # Log failed trade close
                                                logger.log_trade_close(
                                                    user_id=telegram_id,
                                                    exchange="BINANCE",
                                                    symbol=coin,
                                                    entry_price=entry_price,
                                                    exit_price=current_price,
                                                    profit_loss=0,
                                                    profit_loss_pct=0,
                                                    close_reason=close_reason,
                                                    error_msg="Failed to close position via API",
                                                    status="FAILED",
                                                    api_status_code=400
                                                )
                                                print(f"[TRADE-LOOP] ❌ Failed to close LONG position for {coin}")
                                    except Exception as e:
                                        print(f"[TRADE-LOOP] ❌ Error closing LONG position for {coin}: {e}")
                                        bot.send_message(
                                            chat_id=telegram_id,
                                            text=f"<b>❌ Error Closing Position</b>\n\n"
                                                 f"💰 <b>Coin:</b> <code>{coin}</code> (LONG)\n"
                                                 f"⚠️ <b>Error:</b> {str(e)[:100]}",
                                            parse_mode='HTML'
                                        )
                        elif position_type == "SHORT":
                            # Calculate current profit percentage (for SHORT: profit when price goes DOWN)
                            current_profit_pct = ((entry_price - current_price) / entry_price) * 100
                            print(f"[TRADE-LOOP] 💰 {coin} SHORT P/L: {current_profit_pct:.2f}%")
                            
                            # Track highest profit for trailing stop
                            if current_profit_pct > user_data[username]["trades"][coin].get("highest_profit_pct", 0):
                                user_data[username]["trades"][coin]["highest_profit_pct"] = current_profit_pct
                            
                            # === BREAKEVEN LOGIC ===
                            # When profit hits breakeven_trigger_pct, move SL to entry price
                            if current_profit_pct >= breakeven_trigger_pct and not user_data[username]["trades"][coin].get("breakeven_set", False):
                                new_sl = entry_price * 0.999  # Slightly below entry for fees (SHORT)
                                user_data[username]["trades"][coin]["stop_loss_price"] = new_sl
                                user_data[username]["trades"][coin]["breakeven_set"] = True
                                current_sl = new_sl
                                print(f"[TRADE-LOOP] ✅ {coin} BREAKEVEN SET (SHORT) | New SL: {new_sl:.6f}")
                                bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"🔒 <b>Breakeven Set (SHORT)</b>\n\n"
                                         f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                         f"📊 <b>Profit:</b> <code>{current_profit_pct:.2f}%</code>\n"
                                         f"🛑 <b>New SL:</b> <code>${new_sl:.6f}</code>\n\n"
                                         f"<i>Stop loss moved to breakeven.</i>",
                                    parse_mode='HTML'
                                )
                            
                            # === TRAILING STOP LOGIC ===
                            # When profit hits trailing_trigger_pct, start trailing
                            if current_profit_pct >= trailing_trigger_pct:
                                user_data[username]["trades"][coin]["tp_hit"] = True
                                # Calculate trailing SL: lock in (peak_profit - trailing_percentage)
                                peak_profit = user_data[username]["trades"][coin].get("highest_profit_pct", current_profit_pct)
                                locked_profit_pct = peak_profit - trailing_percentage
                                new_trailing_sl = entry_price * (1 - locked_profit_pct / 100)  # For SHORT, SL is above entry
                                
                                # For SHORT, only move SL down, never up
                                if new_trailing_sl < current_sl:
                                    user_data[username]["trades"][coin]["stop_loss_price"] = new_trailing_sl
                                    user_data[username]["trades"][coin]["stop_price"] = new_trailing_sl
                                    current_sl = new_trailing_sl
                                    print(f"[TRADE-LOOP] 📉 {coin} TRAILING SL (SHORT): {new_trailing_sl:.6f} | Locking {locked_profit_pct:.1f}%")
                            
                            current_sl = user_data[username]["trades"][coin]["stop_loss_price"]
                            current_tp = user_data[username]["trades"][coin]["take_profit_price"]
                            
                            # Determine if we should close the position
                            should_close = False
                            close_reason = ""
                            
                            # Stop loss hit (price goes UP for SHORT)
                            if current_price >= current_sl:
                                should_close = True
                                close_reason = "STOP_LOSS"
                            # Take profit hit at win_percentage_future (45%)
                            elif current_price <= current_tp:
                                should_close = True
                                close_reason = "TAKE_PROFIT"
                            
                            if should_close:
                                print(f"[TRADE-LOOP] 🛑 Exit condition met for SHORT {coin}")
                                # Check if position still exists
                                open_positions = client.futures_position_information()
                                user_data[username]["position_found"] = False
                                
                                for position in open_positions:
                                    if position["symbol"] == coin and position["positionAmt"] != "0":
                                        user_data[username]["position_found"] = True
                                        break
                                
                                if not user_data[username]["position_found"]:
                                    print(f"[TRADE-LOOP] ⚠️ Position not found on Binance - manually closed")
                                    # Position manually closed
                                    if coin in user_data[username]["active_coins"]:
                                        user_data[username]["trades"][coin]["holding_position"] = False
                                        user_data[username]["trades"][coin]["trade_completed"] = True
                                        user_data[username]["trades"][coin]["stop_price"] = 0
                                        user_data[username]["active_coins"].remove(coin)
                                        
                                        active_count = sum(
                                            1 for c in user_data[username]['trades']
                                            if user_data[username]['trades'][c]['holding_position']
                                        )
                                        
                                        profit_loss = ((entry_price - current_price) / entry_price * 100) if entry_price else 0
                                        
                                        bot.send_message(
                                            chat_id=telegram_id,
                                            text=f"<b>🔔 MANUAL CLOSE (SHORT)</b>\n"
                                                 f"━━━━━━━━━━━━━━━━━\n\n"
                                                 f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                 f"📉 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                 f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                 f"{('🟢' if profit_loss >= 0 else '🔴')} <b>P/L:</b> <code>{profit_loss:.2f}%</code>\n"
                                                 f"🔢 <b>Active:</b> {active_count}/{MAX_CONCURRENT_TRADES}",
                                            parse_mode='HTML'
                                        )
                                        
                                        print(f"[TRADE-MANAGEMENT] 🔄 Manual SHORT close detected for {coin}. Active: {active_count}/{MAX_CONCURRENT_TRADES}")
                                else:
                                    print(f"[TRADE-LOOP] 🔄 Closing SHORT position for {coin} via API...")
                                    # Close position via API
                                    try:
                                        async with user_data[username]["lock"]:
                                            order = close_position_future(
                                                coin,
                                                user_data[username]["trades"][coin]["trade_quantity"],
                                                client,
                                                "SHORT"
                                            )
                                            
                                            if order:
                                                user_data[username]["trades"][coin]["holding_position"] = False
                                                user_data[username]["trades"][coin]["trade_completed"] = True
                                                user_data[username]["trades"][coin]["stop_price"] = 0
                                                
                                                # Get real active count from Binance
                                                real_active = get_real_active_futures_count(client)
                                                
                                                if current_price >= current_sl:
                                                    bot.send_message(
                                                        chat_id=telegram_id,
                                                        text=f"<b>🔴 STOP LOSS HIT (SHORT)</b>\n"
                                                             f"━━━━━━━━━━━━━━━━━\n\n"
                                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                             f"📉 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                             f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                             f"🔴 <b>Loss:</b> <code>{((entry_price - current_price) / entry_price * 100):.2f}%</code>\n"
                                                             f"🔢 <b>Active:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                                        parse_mode='HTML'
                                                    )
                                                else:
                                                    bot.send_message(
                                                        chat_id=telegram_id,
                                                        text=f"<b>🟢 TAKE PROFIT HIT (SHORT)</b>\n"
                                                             f"━━━━━━━━━━━━━━━━━\n\n"
                                                             f"💰 <b>Coin:</b> <code>{coin}</code>\n"
                                                             f"📉 <b>Entry:</b> <code>${entry_price:.6f}</code>\n"
                                                             f"💸 <b>Exit:</b> <code>${current_price:.6f}</code>\n"
                                                             f"🟢 <b>Profit:</b> <code>{((entry_price - current_price) / entry_price * 100):.2f}%</code>\n"
                                                             f"🔢 <b>Active:</b> {real_active}/{MAX_CONCURRENT_TRADES}",
                                                        parse_mode='HTML'
                                                    )
                                                
                                                await asyncio.sleep(2)
                                                
                                                if coin in user_data[username]["active_coins"]:
                                                    user_data[username]["active_coins"].remove(coin)
                                                
                                                print(f"[TRADE-MANAGEMENT] ✅ Auto-closed SHORT for {coin}. Active: {real_active}/{MAX_CONCURRENT_TRADES}")
                                            else:
                                                print(f"[TRADE-LOOP] ❌ Failed to close SHORT position for {coin}")
                                    except Exception as e:
                                        print(f"[TRADE-LOOP] ❌ Error closing SHORT position for {coin}: {e}")
                                        bot.send_message(
                                            chat_id=telegram_id,
                                            text=f"<b>❌ Error Closing Position</b>\n\n"
                                                 f"💰 <b>Coin:</b> <code>{coin}</code> (SHORT)\n"
                                                 f"⚠️ <b>Error:</b> {str(e)[:100]}",
                                            parse_mode='HTML'
                                        )
                    except Exception as e:
                        print(f"[TRADE-LOOP] ❌ Error managing position for {coin}: {e}")
                        import traceback
                        traceback.print_exc()
            
            except Exception as e:
                print(f"[TRADE-LOOP] ❌ Error processing {coin}: {e}")
                import traceback
                traceback.print_exc()
                
                if not user_data[username]["trades"][coin]["holding_position"]:
                    if coin in user_data[username]["active_coins"]:
                        user_data[username]["trades"][coin]["holding_position"] = False
                        user_data[username]["trades"][coin]["trade_completed"] = True
                        user_data[username]["trades"][coin]["stop_price"] = 0
                        user_data[username]["active_coins"].remove(coin)
                        print(f"[TRADE-MANAGEMENT] ℹ️ Error handler for {coin}. Bot will continue.")
        
        # Sleep between loop iterations
        print(f"[TRADE-LOOP] 😴 Sleeping for 3 seconds before next iteration...")
        await asyncio.sleep(3)
    
    print(f"[TRADE-LOOP] 🛑 Exiting trade loop for {username}")


def get_user_balance(username_key):
    """Get user's wallet balance"""
    if username_key in user_data:
        try:
            print(f"[DEBUG] Fetching balance for {username_key}...")
            client = user_data[username_key]["client"]
            print(f"[DEBUG] Client object: {type(client)}")
            balance = get_wallet_balance_future(client)
            print(f"[DEBUG] Balance fetched successfully: ${balance:.2f}")
            return balance
        except ConnectTimeout as e:
            print(f"[ERROR] Connection timeout while fetching balance for {username_key}: {e}")
            return None
        except BinanceAPIException as e:
            print(f"[ERROR] Binance API error for {username_key}: {e.status_code} - {e.message}")
            return None
        except Exception as e:
            print(f"[ERROR] Unexpected error fetching balance for {username_key}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
    else:
        print(f"[DEBUG] User {username_key} not found in user_data")
    return None


def get_user_balance_with_error_info(username_key):
    """
    Get user's wallet balance and return detailed error information if it fails.
    
    Returns:
        dict: {
            'balance': float or None,
            'error': None or error dict with keys:
                - 'code': error code (-2015, -1021, etc)
                - 'status': HTTP status code
                - 'message': Error message
                - 'user_message': User-friendly explanation
                - 'solution': How to fix the problem
        }
    """
    if username_key not in user_data:
        return {
            'balance': None,
            'error': {
                'code': None,
                'status': 500,
                'message': 'User session not found',
                'user_message': '❌ <b>Session Error</b>\n\nYour trading session was not found.',
                'solution': 'Please stop trading and start again.'
            }
        }
    
    try:
        client = user_data[username_key]["client"]
        balance = get_wallet_balance_future(client)
        return {'balance': balance, 'error': None}
    
    except BinanceAPIException as e:
        error_code = e.status_code
        error_message = str(e)
        
        # Error -2015: Invalid API key, IP, or permissions
        if "-2015" in error_message or error_code == 401:
            return {
                'balance': None,
                'error': {
                    'code': -2015,
                    'status': error_code,
                    'message': error_message,
                    'user_message': (
                        "❌ <b>Binance API Authentication Failed</b>\n\n"
                        "Your API credentials are not working. This usually means one of:\n\n"
                        "1️⃣ <b>Futures Trading Not Enabled</b> (Most Common!)\n"
                        "2️⃣ IP Whitelist Blocking Your Connection\n"
                        "3️⃣ Invalid or Expired API Key\n"
                        "4️⃣ Insufficient API Permissions"
                    ),
                    'solution': (
                        "<b>🔧 How to Fix:</b>\n\n"
                        "1. Go to <b>Binance → Account → API Management</b>\n"
                        "2. Find your API key and click <b>Edit</b>\n"
                        "3. Make sure these are <b>ENABLED ✅</b>:\n"
                        "   • <code>Enable Futures Trading</code>\n"
                        "   • <code>Enable Reading Account Trade Data</code>\n"
                        "   • <code>Enable Changing Leverage</code>\n"
                        "4. Click <b>IP Whitelist</b> and verify your IP is added\n"
                        "5. Restart the bot and try again\n\n"
                        "📌 <i>If still not working, regenerate a NEW API key in Binance.</i>"
                    )
                }
            }
        
        # Error -1021: Timestamp outside acceptable range (IP time sync issue)
        elif "-1021" in error_message or "timestamp" in error_message.lower():
            return {
                'balance': None,
                'error': {
                    'code': -1021,
                    'status': error_code,
                    'message': error_message,
                    'user_message': "❌ <b>Server Time Sync Error</b>\n\nYour server's clock is out of sync with Binance.",
                    'solution': (
                        "<b>🔧 How to Fix:</b>\n\n"
                        "This is a server time synchronization issue.\n\n"
                        "If you're on a VPS, contact your hosting provider and ask them to:\n"
                        "• Synchronize the server time\n"
                        "• Enable NTP (Network Time Protocol)\n\n"
                        "System administrators can run:\n"
                        "<code>ntpdate -s time.nist.gov</code>"
                    )
                }
            }
        
        # Error -1000: Invalid request (bad parameters)
        elif "-1000" in error_message:
            return {
                'balance': None,
                'error': {
                    'code': -1000,
                    'status': error_code,
                    'message': error_message,
                    'user_message': "❌ <b>Invalid API Request</b>\n\nThe API key configuration has an issue.",
                    'solution': "Please contact support with error details."
                }
            }
        
        # Generic API error
        else:
            return {
                'balance': None,
                'error': {
                    'code': error_code,
                    'status': error_code,
                    'message': error_message,
                    'user_message': f"❌ <b>Binance API Error {error_code}</b>\n\n{error_message[:200]}",
                    'solution': "Please verify your API key settings on Binance."
                }
            }
    
    except ConnectTimeout as e:
        return {
            'balance': None,
            'error': {
                'code': 'TIMEOUT',
                'status': 408,
                'message': str(e),
                'user_message': "❌ <b>Connection Timeout</b>\n\nCould not reach Binance servers.",
                'solution': (
                    "<b>🔧 How to Fix:</b>\n\n"
                    "• Check your internet connection\n"
                    "• Binance might be undergoing maintenance\n"
                    "• Try again in a few moments\n"
                    "• If problem persists, check: <code>status.binance.com</code>"
                )
            }
        }
    
    except Exception as e:
        return {
            'balance': None,
            'error': {
                'code': 'UNKNOWN',
                'status': 500,
                'message': str(e),
                'user_message': f"❌ <b>Unexpected Error</b>\n\n{str(e)[:200]}",
                'solution': "Please contact support with the error details above."
            }
        }


def get_trading_status(username_key):
    """Get current trading status for a user"""
    if username_key in user_data:
        return {
            "status": user_data[username_key]["bot_status"],
            "trading_mode": user_data[username_key]["trading_mode"],
            "active_trades": sum(1 for c in user_data[username_key]["trades"] if user_data[username_key]["trades"][c]["holding_position"]),
            "balance": get_user_balance(username_key)
        }
    return None


def get_detailed_trading_status(username_key):
    """Get detailed trading status with P&L information for Binance Futures"""
    if username_key not in user_data:
        return None
    
    try:
        client = user_data[username_key].get("client")
        if not client:
            return None
        
        # Get account information
        account = client.futures_account()
        positions_info = client.futures_position_information()
        
        # Extract key metrics
        total_wallet_balance = float(account.get('totalWalletBalance', 0))
        total_unrealized_pnl = float(account.get('totalUnrealizedProfit', 0))
        total_margin_balance = float(account.get('totalMarginBalance', 0))
        available_balance = float(account.get('availableBalance', 0))
        total_initial_margin = float(account.get('totalInitialMargin', 0))
        total_maint_margin = float(account.get('totalMaintMargin', 0))
        cross_wallet_balance = float(account.get('totalCrossWalletBalance', total_margin_balance))
        
        # Calculate total PnL percentage
        if total_wallet_balance > 0:
            pnl_percentage = (total_unrealized_pnl / total_wallet_balance) * 100
        else:
            pnl_percentage = 0
        
        # Get active positions with details
        active_positions = []
        for pos in positions_info:
            position_amt = float(pos.get('positionAmt', 0))
            if position_amt != 0:
                entry_price = float(pos.get('entryPrice', 0))
                mark_price = float(pos.get('markPrice', 0))
                unrealized_pnl = float(pos.get('unRealizedProfit', 0))
                liquidation_price = float(pos.get('liquidationPrice', 0))

                leverage = str(FUTURES_LEVERAGE)
                
                # Calculate position PnL %
                if entry_price > 0:
                    if position_amt > 0:  # Long
                        pos_pnl_pct = ((mark_price - entry_price) / entry_price) * 100
                        side = "LONG"
                    else:  # Short
                        pos_pnl_pct = ((entry_price - mark_price) / entry_price) * 100
                        side = "SHORT"
                else:
                    pos_pnl_pct = 0
                    side = "LONG" if position_amt > 0 else "SHORT"
                
                active_positions.append({
                    "symbol": pos.get('symbol', 'Unknown'),
                    "side": side,
                    "size": abs(position_amt),
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "current_price": mark_price,  # For compatibility
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_percentage": pos_pnl_pct,
                    "leverage": leverage,
                    "liquidation_price": liquidation_price,
                })
        
        return {
            "status": user_data[username_key]["bot_status"],
            "trading_mode": user_data[username_key]["trading_mode"],
            "active_trades": len(active_positions),
            "balance": total_wallet_balance,
            "margin_balance": total_margin_balance,
            "available_balance": available_balance,
            "cross_wallet_balance": cross_wallet_balance,
            "total_initial_margin": total_initial_margin,
            "total_maint_margin": total_maint_margin,
            "unrealized_pnl": total_unrealized_pnl,
            "pnl_percentage": pnl_percentage,
            "positions": active_positions,
        }
    except Exception as e:
        print(f"[STATS] Error getting detailed status: {e}")
        # Fall back to basic status
        return get_trading_status(username_key)
