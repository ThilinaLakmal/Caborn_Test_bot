"""
MEXC Trading Handler
Manages MEXC futures trading sessions and the automated trade loop.
Structure mirrors handlers/trading_handler.py for Binance.
"""
import asyncio
from datetime import datetime, timedelta

from trading.mexc_client_factory import get_mexc_client
from trading.mexc_future_trading import (
    get_mexc_wallet_balance,
    get_mexc_recent_prices,
    find_mexc_support_level,
    find_mexc_resistance_level,
    get_mexc_trade_signal,
    long_mexc_trade,
    short_mexc_trade,
    close_mexc_position,
    get_mexc_open_positions,
    get_mexc_position_for_symbol,
    get_mexc_current_price,
    get_mexc_detailed_status,
    get_real_active_mexc_count,
    _calculate_mexc_quantity,
)
from trading.crash_protection import crash_protector
from config import (
    MEXC_FUTURES_SYMBOLS_LIST as mexc_symbols_list,
    MEXC_WIN_PERCENTAGE,
    MEXC_LOSS_PERCENTAGE,
    MEXC_BREAKEVEN_TRIGGER_PCT,
    MEXC_TRAILING_TRIGGER_PCT,
    MEXC_TRAILING_STOP_PCT,
    MEXC_USE_TRAILING_STOP,
    MEXC_MAX_CONCURRENT_TRADES as MAX_CONCURRENT_TRADES,
    MEXC_WALLET_PERCENTAGE,
    MEXC_LEVERAGE,
    MEXC_MARGIN_TYPE,
    MEXC_MIN_BALANCE,
    MEXC_CRASH_LOWER_THRESHOLD_PCT,
    MEXC_CRASH_UPPER_THRESHOLD_PCT,
)

# ─────────────────────────────────────────────────────────
# In-memory session stores
# ─────────────────────────────────────────────────────────
mexc_user_data: dict = {}    # username_key -> session dict
mexc_user_tasks: dict = {}   # username_key -> asyncio tasks


# ─────────────────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────────────────

def is_mexc_trading_active(username_key: str) -> bool:
    if username_key not in mexc_user_data:
        return False
    return mexc_user_data[username_key].get("bot_status") == "Running"


def get_mexc_trading_mode(username_key: str) -> str | None:
    if username_key not in mexc_user_data:
        return None
    return mexc_user_data[username_key].get("trading_mode")


def _get_fresh_mexc_client(username_key: str):
    """Get fresh MEXC client for user (no caching)"""
    if username_key not in mexc_user_data:
        return None
    api_key = mexc_user_data[username_key].get("api_key")
    api_secret = mexc_user_data[username_key].get("api_secret")
    return get_mexc_client(api_key, api_secret)


def initialize_mexc_session(username_key: str, telegram_id: int,
                             api_key: str, api_secret: str,
                             coins_list: list) -> dict:
    """Create and store a new MEXC trading session."""
    mexc_user_data[username_key] = {
        "telegram_id": telegram_id,
        "api_key": api_key,
        "api_secret": api_secret,
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

    print(f"[MEXC-SESSION] ✅ Initialized for {username_key}")
    return mexc_user_data[username_key]


async def start_mexc_trading(bot, telegram_id: int, username_key: str) -> bool:
    """Launch the MEXC futures trade loop as an asyncio task."""
    print(f"[MEXC-TRADING] 🚀 Starting for {username_key}")

    if username_key not in mexc_user_data:
        print(f"[MEXC-TRADING] ❌ No session for {username_key}")
        return False

    # Reset crash notification flag for fresh start
    mexc_user_data[username_key]["crash_notification_sent"] = False
    
    mexc_user_data[username_key]["trading_mode"] = "MEXC Futures"
    mexc_user_data[username_key]["bot_status"] = "Running"

    if username_key not in mexc_user_tasks:
        mexc_user_tasks[username_key] = {}

    task = asyncio.create_task(mexc_trade_loop(username_key, bot, telegram_id))
    mexc_user_tasks[username_key]["mexc_future"] = task

    print(f"[MEXC-TRADING] ✅ Task created for {username_key}")
    return True


async def stop_mexc_trading(username_key: str) -> bool:
    """Stop the MEXC trade loop and close ALL open positions on MEXC."""
    print(f"[MEXC-TRADING] 🛑 Stopping for {username_key}")

    if username_key in mexc_user_data:
        telegram_id = mexc_user_data[username_key].get("telegram_id")
        mexc_user_data[username_key]["bot_status"] = "Stopped"
        # Reset crash notification flag for next session
        mexc_user_data[username_key]["crash_notification_sent"] = False

        # Cancel running tasks first
        if username_key in mexc_user_tasks:
            for name, task in mexc_user_tasks[username_key].items():
                if task and not task.done():
                    task.cancel()
                    print(f"[MEXC-TRADING] Cancelled task {name}")
            mexc_user_tasks[username_key].clear()

        # Close ALL open positions on MEXC
        client = _get_fresh_mexc_client(username_key)
        if client:
            try:
                positions = get_mexc_open_positions(client)
                for pos in positions:
                    symbol = pos.get('symbol', '')
                    # MEXC SDK returns symbol in format like 'BTCUSDT', no conversion needed
                    qty = abs(float(pos.get('executedQty') or pos.get('qty', 0)))
                    side = pos.get('side', 'BUY').upper()
                    pos_type = "LONG" if side == 'BUY' else "SHORT"
                    if qty > 0:
                        print(f"[MEXC-TRADING] 🔄 Closing {pos_type} {symbol} (qty={qty})")
                        close_mexc_position(symbol, qty, client, pos_type)
                print(f"[MEXC-TRADING] ✅ All MEXC positions closed for {username_key}")
            except Exception as e:
                print(f"[MEXC-TRADING] ⚠️ Error closing MEXC positions: {e}")

        # Reset internal trade state
        for coin in mexc_user_data[username_key]["trades"]:
            mexc_user_data[username_key]["trades"][coin]["holding_position"] = False
            mexc_user_data[username_key]["trades"][coin]["position_type"] = None
        mexc_user_data[username_key]["active_coins"].clear()
        
        # 🧹 CLEANUP: Remove session from memory to prevent accumulation
        from utils.cleanup_utils import cleanup_mexc_session, cleanup_crash_protection_data
        cleanup_mexc_session(username_key)
        cleanup_crash_protection_data(telegram_id)

        return True
    return False


def get_mexc_user_balance(username_key: str) -> float | None:
    """Return the USDT wallet balance for the user's MEXC session."""
    if username_key not in mexc_user_data:
        return None
    try:
        client = _get_fresh_mexc_client(username_key)
        return get_mexc_wallet_balance(client)
    except Exception as e:
        print(f"[MEXC-BALANCE] Error for {username_key}: {e}")
        return None


def get_mexc_user_balance_with_error_info(username_key: str) -> dict:
    """
    Get MEXC balance with detailed error information.
    
    Returns:
        dict: {'balance': float or None, 'error': error_dict or None}
    """
    if username_key not in mexc_user_data:
        return {
            'balance': None,
            'error': {
                'message': "User session not found",
                'user_message': "❌ Session Error - Please restart trading.",
                'solution': "Stop and restart MEXC trading."
            }
        }
    
    try:
        client = _get_fresh_mexc_client(username_key)
        balance = get_mexc_wallet_balance(client)
        return {'balance': balance, 'error': None}
    except Exception as e:
        error_msg = str(e).lower()
        
        # Invalid API key
        if 'api_key_invalid' in error_msg or 'invalid api' in error_msg or '401' in error_msg:
            return {
                'balance': None,
                'error': {
                    'message': str(e),
                    'user_message': (
                        "❌ <b>MEXC API Authentication Failed</b>\n\n"
                        "Your MEXC API credentials are not working."
                    ),
                    'solution': (
                        "<b>🔧 How to Fix:</b>\n\n"
                        "1. Go to <b>MEXC → Profile → API Management</b>\n"
                        "2. Make sure these are <b>ENABLED ✅</b>:\n"
                        "   • <code>Futures/Contract Trading</code> permission\n"
                        "   • <code>Read Account Trade Data</code>\n"
                        "3. Check if API key is still active\n"
                        "4. Try generating a NEW API key if still not working\n"
                        "5. Update your API credentials in the bot and try again"
                    )
                }
            }
        
        # Connection error
        elif 'connection' in error_msg or 'timeout' in error_msg:
            return {
                'balance': None,
                'error': {
                    'message': str(e),
                    'user_message': "❌ <b>Connection Error</b>\n\nCould not reach MEXC servers.",
                    'solution': "• Check your internet connection\n• Try again in a few moments\n• MEXC might be under maintenance"
                }
            }
        
        # Generic error
        else:
            return {
                'balance': None,
                'error': {
                    'message': str(e),
                    'user_message': f"❌ <b>MEXC Error</b>\n\n{str(e)[:200]}",
                    'solution': "Please verify your MEXC API key settings."
                }
            }


def get_mexc_trading_status(username_key: str) -> dict | None:
    """Return a basic status dict for the MEXC session."""
    if username_key not in mexc_user_data:
        return None
    data = mexc_user_data[username_key]
    return {
        "status": data.get("bot_status", "Unknown"),
        "trading_mode": data.get("trading_mode", "MEXC Futures"),
        "active_trades": sum(
            1 for t in data["trades"].values() if t.get("holding_position")
        ),
    }


def get_detailed_mexc_status(username_key: str) -> dict | None:
    """Return full P&L status dict for the MEXC session."""
    if username_key not in mexc_user_data:
        return None
    try:
        client = _get_fresh_mexc_client(username_key)
        status = get_mexc_detailed_status(client)
        if status:
            status["status"] = mexc_user_data[username_key].get("bot_status", "Unknown")
        return status
    except Exception as e:
        print(f"[MEXC-STATUS] Error for {username_key}: {e}")
        return None


# ─────────────────────────────────────────────────────────
# Main MEXC Trade Loop
# ─────────────────────────────────────────────────────────

async def mexc_trade_loop(username: str, bot, telegram_id: int):
    """
    Automated MEXC futures trading loop.
    Mirrors trade_loop_future() from trading_handler.py.
    """
    print(f"[MEXC-LOOP] 🚀 Started for {username} at {datetime.now()}")
    
    # RESET crash mode on new session (fresh start after restart)
    crash_protector.reset_crash_mode()
    print(f"[MEXC-LOOP] 🔄 Crash protection reset for fresh trading session")

    last_status_msg = datetime.now()

    # Initialise active coin list
    if not mexc_user_data[username]["active_coins"]:
        mexc_user_data[username]["active_coins"] = mexc_symbols_list.copy()
        print(f"[MEXC-LOOP] Using {len(mexc_symbols_list)} coins")

    loop_count = 0

    while mexc_user_data[username]["bot_status"] == "Running":
        loop_count += 1

        try:
            # ── Daily P&L status message every hour ────────────────
            now = datetime.now()
            if (now - last_status_msg).total_seconds() > 3600:
                last_status_msg = now
                try:
                    client = _get_fresh_mexc_client(username)
                    bal = get_mexc_wallet_balance(client)
                    active = get_real_active_mexc_count(client)
                    bot.send_message(
                        telegram_id,
                        f"📊 <b>MEXC Hourly Update</b>\n"
                        f"💰 Balance: <code>{bal:.2f} USDT</code>\n"
                        f"📈 Active Trades: {active}/{MAX_CONCURRENT_TRADES}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # ── Wallet balance check ───────────────────────────────
            client = _get_fresh_mexc_client(username)
            wallet_bal = get_mexc_wallet_balance(client)
            mexc_user_data[username]["wallet_balance"] = wallet_bal

            if wallet_bal < MEXC_MIN_BALANCE:
                print(f"[MEXC-LOOP] ⛔ Balance too low: {wallet_bal:.2f} USDT")
                await asyncio.sleep(60)
                continue

            # === CRASH PROTECTION: Check market health ===
            try:
                crash_result = crash_protector.check_for_crash(
                    client, 
                    threshold_override=MEXC_CRASH_LOWER_THRESHOLD_PCT,
                    upper_threshold_override=MEXC_CRASH_UPPER_THRESHOLD_PCT
                )
                if crash_result.get('is_crashing'):
                    print(f"[MEXC-LOOP] 🚨 CRASH DETECTED: {crash_result.get('reason')}")
                    
                    # Notify user about crash detection (once per hour)
                    last_notification_time = mexc_user_data[username].get('last_crash_notification_sent', None)
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
                            title = "🚨 MARKET PUMP DETECTED - MEXC TRADING HALTED"
                            desc = f"📈 <b>BTC pumped {drop_pct:.1f}%</b>\n💰 Price: ${window_low:.2f} → ${current_price:.2f}"
                        else:
                            title = "🚨 MARKET CRASH DETECTED - MEXC TRADING HALTED"
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
                                     f"✅ Trading paused for 24 hours\n\n"
                                     f"⏳ <b>Cooldown:</b> 24 hours active\n\n"
                                     f"💡 <b>To skip waiting:</b> Stop trading and restart from main menu.",
                                parse_mode='HTML'
                            )
                            mexc_user_data[username]['last_crash_notification_sent'] = now
                        except Exception as e:
                            print(f"[MEXC-LOOP] ⚠️ Failed to send crash notification: {e}")
                    
                    # Emergency close all open MEXC positions
                    crash_protector.emergency_close_all(
                        client,
                        mexc_user_data, username, bot, telegram_id, exchange="MEXC"
                    )
                    # Wait for cooldown
                    print(f"[MEXC-LOOP] ⏸️ Trading paused — crash cooldown active")
                    await asyncio.sleep(60)  # Check every minute during cooldown
                    continue
                
                # Check if trading is allowed (daily limits, cooldown, etc.)
                crash_protector.set_daily_start_balance(username, wallet_bal)
                trading_allowed, block_reason = crash_protector.is_trading_allowed(username, wallet_bal)
                
                # SAFETY CHECK: If cooldown expired naturally, auto-reset crash mode
                if "Crash cooldown" in block_reason and crash_protector.crash_triggered_at:
                    elapsed = (datetime.now() - crash_protector.crash_triggered_at).total_seconds() / 60
                    if elapsed >= (24 * 60): # 1440 min
                        print(f"[MEXC-LOOP] 🔄 Crash cooldown expired naturally - resetting crash mode")
                        crash_protector.reset_crash_mode()
                        mexc_user_data[username]['last_crash_notification_sent'] = None
                        trading_allowed, block_reason = crash_protector.is_trading_allowed(username, wallet_bal)
                
                if not trading_allowed:
                    print(f"[MEXC-LOOP] ⛔ Trading blocked: {block_reason}")
                    if "Crash cooldown" in block_reason:
                        now = datetime.now()
                        last_cooldown_notif = mexc_user_data[username].get('last_cooldown_notification_sent')
                        should_notify_cooldown = False
                        if not last_cooldown_notif or (now - last_cooldown_notif).total_seconds() >= 3600:
                            should_notify_cooldown = True
                            
                        if should_notify_cooldown:
                            try:
                                bot.send_message(
                                    chat_id=telegram_id,
                                    text=f"<b>🚨 MEXC Trading Status: PAUSED</b>\n"
                                         f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                                         f"⏸️ <b>Reason:</b> Market Crash Protection\n"
                                         f"📌 <b>Status:</b> {block_reason}\n\n"
                                         f"⏰ <i>Trading will automatically resume after the cooldown period.</i>",
                                    parse_mode='HTML'
                                )
                                mexc_user_data[username]['last_cooldown_notification_sent'] = now
                            except Exception as e:
                                print(f"[MEXC-LOOP] ⚠️ Failed to send crash cooldown notification: {e}")
                    await asyncio.sleep(30)
                    continue

            except Exception as crash_err:
                print(f"[MEXC-LOOP] Error checking crash protection: {crash_err}")

            # ── Sync internal flags with real MEXC positions ───────
            try:
                open_positions = get_mexc_open_positions(client)
                open_symbols = set()
                for pos in open_positions:
                    # MEXC SDK returns symbol directly in format like 'BTCUSDT'
                    symbol = pos.get("symbol", "")
                    if symbol:
                        open_symbols.add(symbol)

                for coin in mexc_user_data[username]["trades"]:
                    trade = mexc_user_data[username]["trades"][coin]
                    if trade["holding_position"] and coin not in open_symbols:
                        print(f"[MEXC-LOOP] 🔄 Sync: {coin} position closed externally")
                        trade["holding_position"] = False
                        trade["position_type"] = None
                        trade["entry_price"] = 0

                for coin in open_symbols:
                    if coin in mexc_user_data[username]["trades"]:
                        if not mexc_user_data[username]["trades"][coin]["holding_position"]:
                            for pos in open_positions:
                                if pos.get("symbol", "") == coin:
                                    qty = abs(float(pos.get("executedQty") or pos.get('qty', 0)))
                                    entry = float(pos.get("price", 0))
                                    side = "LONG" if pos.get("side", "BUY").upper() == "BUY" else "SHORT"
                                    mexc_user_data[username]["trades"][coin]["holding_position"] = True
                                    mexc_user_data[username]["trades"][coin]["entry_price"] = entry
                                    mexc_user_data[username]["trades"][coin]["trade_quantity"] = qty
                                    mexc_user_data[username]["trades"][coin]["position_type"] = side
                                    print(f"[MEXC-LOOP] 🔄 Sync: detected real {side} on {coin}")
                                    break
            except Exception as sync_err:
                print(f"[MEXC-LOOP] Sync error: {sync_err}")

            # ── Count active positions ─────────────────────────────
            active_count = sum(
                1 for t in mexc_user_data[username]["trades"].values()
                if t.get("holding_position")
            )

            # ── Manage open positions (TP/SL/trailing stop) ────────
            for coin, trade in mexc_user_data[username]["trades"].items():
                if not trade.get("holding_position"):
                    continue

                try:
                    current_price = get_mexc_current_price(coin, client)
                    if current_price <= 0:
                        continue

                    entry = trade["entry_price"]
                    qty = trade["trade_quantity"]
                    pos_type = trade["position_type"]
                    sl = trade["stop_loss_price"]
                    tp = trade["take_profit_price"]

                    if entry <= 0:
                        continue

                    # Profit % from entry
                    if pos_type == "LONG":
                        pct = (current_price - entry) / entry * 100 * MEXC_LEVERAGE
                    else:
                        pct = (entry - current_price) / entry * 100 * MEXC_LEVERAGE

                    trade["highest_profit_pct"] = max(trade.get("highest_profit_pct", 0), pct)

                    # ── Trailing stop update ───────────────────────
                    if MEXC_USE_TRAILING_STOP and pct >= MEXC_TRAILING_TRIGGER_PCT:
                        if pos_type == "LONG":
                            trail_stop_price = current_price * (1 - MEXC_TRAILING_STOP_PCT / 100)
                            if trail_stop_price > trade.get("stop_price", 0):
                                trade["stop_price"] = trail_stop_price
                                print(f"[MEXC-LOOP] 📈 {coin}: trailing SL → {trail_stop_price:.6f}")
                        else:
                            trail_stop_price = current_price * (1 + MEXC_TRAILING_STOP_PCT / 100)
                            if trail_stop_price < trade.get("stop_price", float("inf")):
                                trade["stop_price"] = trail_stop_price
                                print(f"[MEXC-LOOP] 📉 {coin}: trailing SL → {trail_stop_price:.6f}")

                    # ── Breakeven move ──────────────────────────────
                    if not trade.get("breakeven_set") and pct >= MEXC_BREAKEVEN_TRIGGER_PCT:
                        if pos_type == "LONG":
                            trade["stop_loss_price"] = entry * 1.001
                        else:
                            trade["stop_loss_price"] = entry * 0.999
                        trade["breakeven_set"] = True
                        print(f"[MEXC-LOOP] ⚖️ {coin}: SL moved to breakeven")
                        sl = trade["stop_loss_price"]

                    # ── Check TP ───────────────────────────────────
                    if tp > 0:
                        tp_hit = (pos_type == "LONG" and current_price >= tp) or \
                                 (pos_type == "SHORT" and current_price <= tp)
                        if tp_hit:
                            print(f"[MEXC-LOOP] 🎯 TP HIT {coin} @ {current_price:.6f}")
                            close_mexc_position(coin, qty, client, pos_type)
                            trade["holding_position"] = False
                            pnl = (current_price - entry) * qty if pos_type == "LONG" else (entry - current_price) * qty
                            try:
                                bot.send_message(
                                    telegram_id,
                                    f"🎯 <b>MEXC Take Profit Hit!</b>\n"
                                    f"📌 {coin} | {pos_type}\n"
                                    f"💵 Entry: <code>{entry:.6f}</code>\n"
                                    f"✅ Close: <code>{current_price:.6f}</code>\n"
                                    f"💰 Est. P&L: <code>{pnl:.4f} USDT</code>",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                            continue

                    # ── Check SL / trailing SL ─────────────────────
                    # CRITICAL: Re-read SL values fresh to avoid stale cached values
                    sl = trade["stop_loss_price"]  # Re-read after any breakeven/trailing updates
                    effective_sl = trade.get("stop_price") or sl
                    sl_hit = False
                    if effective_sl and effective_sl > 0:
                        sl_hit = (pos_type == "LONG" and current_price <= effective_sl) or \
                                  (pos_type == "SHORT" and current_price >= effective_sl)

                    if sl_hit:
                        print(f"[MEXC-LOOP] 🛑 SL HIT {coin} @ {current_price:.6f}")
                        close_mexc_position(coin, qty, client, pos_type)
                        trade["holding_position"] = False
                        pnl = (current_price - entry) * qty if pos_type == "LONG" else (entry - current_price) * qty
                        try:
                            bot.send_message(
                                telegram_id,
                                f"🛑 <b>MEXC Stop-Loss Hit</b>\n"
                                f"📌 {coin} | {pos_type}\n"
                                f"💵 Entry: <code>{entry:.6f}</code>\n"
                                f"❌ Close: <code>{current_price:.6f}</code>\n"
                                f"💰 Est. P&L: <code>{pnl:.4f} USDT</code>",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                except Exception as pos_err:
                    print(f"[MEXC-LOOP] Position management error for {coin}: {pos_err}")

            # ── Look for new trade opportunities ───────────────────
            active_count = sum(
                1 for t in mexc_user_data[username]["trades"].values()
                if t.get("holding_position")
            )

            if active_count < MAX_CONCURRENT_TRADES:
                # Rotate through coin list
                if not mexc_user_data[username]["remaining_coins"]:
                    mexc_user_data[username]["remaining_coins"] = mexc_user_data[username]["active_coins"].copy()

                coin = mexc_user_data[username]["remaining_coins"].pop(0)
                trade = mexc_user_data[username]["trades"].get(coin)

                if trade and not trade.get("holding_position"):
                    try:
                        current_price = get_mexc_current_price(coin, client)
                        if current_price <= 0:
                            await asyncio.sleep(1)
                            continue

                        support = find_mexc_support_level(coin, client)
                        resistance = find_mexc_resistance_level(coin, client)

                        signal, details = get_mexc_trade_signal(
                            coin, client,
                            current_price=current_price,
                            support=support,
                            resistance=resistance,
                        )
                        print(f"[MEXC-LOOP] {coin}: signal={signal} (L={details.get('long_score', 0):.1f} S={details.get('short_score', 0):.1f})")

                        if signal in ("LONG", "SHORT"):
                            qty = _calculate_mexc_quantity(wallet_bal, current_price, MEXC_LEVERAGE, MEXC_WALLET_PERCENTAGE)
                            if qty <= 0:
                                continue

                            if signal == "LONG":
                                order = long_mexc_trade(coin, qty, MEXC_LEVERAGE, client)
                                if order:
                                    tp_price = current_price * (1 + MEXC_WIN_PERCENTAGE / 100 / MEXC_LEVERAGE)
                                    sl_price = current_price * (1 - MEXC_LOSS_PERCENTAGE / 100 / MEXC_LEVERAGE)
                                    trade["holding_position"] = True
                                    trade["entry_price"] = current_price
                                    trade["trade_quantity"] = qty
                                    trade["position_type"] = "LONG"
                                    trade["take_profit_price"] = tp_price
                                    trade["stop_loss_price"] = sl_price
                                    trade["stop_price"] = sl_price
                                    trade["breakeven_set"] = False
                                    trade["highest_profit_pct"] = 0.0
                                    bot.send_message(
                                        telegram_id,
                                        f"🚀 <b>MEXC LONG Opened</b>\n"
                                        f"📌 {coin}\n"
                                        f"💵 Entry: <code>{current_price:.6f}</code>\n"
                                        f"🎯 TP: <code>{tp_price:.6f}</code>\n"
                                        f"🛑 SL: <code>{sl_price:.6f}</code>\n"
                                        f"⚡ Leverage: {MEXC_LEVERAGE}x",
                                        parse_mode="HTML",
                                    )
                                else:
                                    bot.send_message(
                                        telegram_id,
                                        f"⚠️ <b>MEXC LONG Skipped</b>\n"
                                        f"📌 {coin}\n"
                                        f"❌ Could not set ISOLATED {MEXC_LEVERAGE}x or order failed",
                                        parse_mode="HTML",
                                    )

                            else:  # SHORT
                                order = short_mexc_trade(coin, qty, MEXC_LEVERAGE, client)
                                if order:
                                    tp_price = current_price * (1 - MEXC_WIN_PERCENTAGE / 100 / MEXC_LEVERAGE)
                                    sl_price = current_price * (1 + MEXC_LOSS_PERCENTAGE / 100 / MEXC_LEVERAGE)
                                    trade["holding_position"] = True
                                    trade["entry_price"] = current_price
                                    trade["trade_quantity"] = qty
                                    trade["position_type"] = "SHORT"
                                    trade["take_profit_price"] = tp_price
                                    trade["stop_loss_price"] = sl_price
                                    trade["stop_price"] = sl_price
                                    trade["breakeven_set"] = False
                                    trade["highest_profit_pct"] = 0.0
                                    bot.send_message(
                                        telegram_id,
                                        f"📉 <b>MEXC SHORT Opened</b>\n"
                                        f"📌 {coin}\n"
                                        f"💵 Entry: <code>{current_price:.6f}</code>\n"
                                        f"🎯 TP: <code>{tp_price:.6f}</code>\n"
                                        f"🛑 SL: <code>{sl_price:.6f}</code>\n"
                                        f"⚡ Leverage: {MEXC_LEVERAGE}x",
                                        parse_mode="HTML",
                                    )
                                else:
                                    bot.send_message(
                                        telegram_id,
                                        f"⚠️ <b>MEXC SHORT Skipped</b>\n"
                                        f"📌 {coin}\n"
                                        f"❌ Could not set ISOLATED {MEXC_LEVERAGE}x or order failed",
                                        parse_mode="HTML",
                                    )

                    except Exception as sig_err:
                        print(f"[MEXC-LOOP] Signal error for {coin}: {sig_err}")

            await asyncio.sleep(10)

        except asyncio.CancelledError:
            print(f"[MEXC-LOOP] ✋ Cancelled for {username}")
            break
        except Exception as loop_err:
            print(f"[MEXC-LOOP] ❌ Unexpected error: {loop_err}")
            await asyncio.sleep(30)

    print(f"[MEXC-LOOP] 🏁 Exited for {username}")
