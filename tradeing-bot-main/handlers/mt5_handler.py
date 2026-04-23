"""
MT5 Forex Trading Handler for Telegram Bot (MetaAPI Cloud — Multi-User)
Manages MT5 trading sessions for multiple users simultaneously via MetaAPI.
Each user gets their own cloud connection — no local MT5 terminal needed.
"""
import asyncio
import html
import traceback
from datetime import datetime, timedelta

from mt5.mt5_config import (
    FOREX_SYMBOLS,
    LOT_SIZE,
    MAX_CONCURRENT_TRADES,
    WIN_PERCENTAGE,
    LOSS_PERCENTAGE,
    TAKE_PROFIT_PIPS,
    STOP_LOSS_PIPS,
    BREAKEVEN_TRIGGER_PCT,
    TRAILING_TRIGGER_PCT,
    TRAILING_STOP_PCT,
    SIGNAL_TIMEFRAME,
    TREND_TIMEFRAME,
    SUPPORT_RESISTANCE_TIMEFRAME,
    STOCH_RSI_PERIOD,
    STOCH_K_SMOOTH,
    STOCH_D_SMOOTH,
    RSI_PERIOD,
    STOCH_RSI_SHORT_LEVEL,
    STOCH_RSI_BUY_LEVEL,
    SCAN_INTERVAL_SECONDS,
    SLEEP_BETWEEN_SYMBOLS,
)
from config import (
    CRASH_COOLDOWN_MINUTES,
    MT5_CRASH_MONITORING_CANDLES,
    MT5_CRASH_MONITORING_TIMEFRAME,
    get_mt5_balance_based_params,
    MT5_MIN_BALANCE_TO_TRADE,
)
from mt5.mt5_core import (
    MT5UserContext,
    create_user_context,
    connect_mt5,
    disconnect_mt5,
    is_mt5_connected,
    get_current_price,
    get_account_balance,
    get_account_info,
    get_symbol_info,
    get_open_positions,
    get_active_positions_count,
    open_position,
    open_position_with_retry,
    close_position,
    modify_position_sl,
    find_support_level_mt5,
    find_resistance_level_mt5,
    resolve_symbol,
    get_position_realized_pnl,
)
from mt5.mt5_signals import get_trade_signal_mt5
from mt5.mt5_crash_protection import mt5_crash_protector


def _mt5_crash_reference_symbol(username_key):
    """
    Always use Gold (XAU* / GOLD*) for MT5 crash % — not the traded loop symbol.
    Uses broker-resolved name from active_symbols when present.
    """
    syms = mt5_user_data.get(username_key, {}).get("active_symbols") or []
    for s in syms:
        if s and ("XAU" in s.upper() or "GOLD" in s.upper()):
            return s
    return syms[0] if syms else mt5_crash_protector.CRASH_REFERENCE_SYMBOL


# Global storage for MT5 user sessions
mt5_user_data = {}
mt5_user_tasks = {}


def _normalize_ticket(ticket):
    """Store MetaAPI identifiers consistently so sync logic is stable."""
    return str(ticket) if ticket is not None else ""


def _get_positions_for_symbol(username_key, symbol):
    """Return all tracked positions for a symbol."""
    return [
        (ticket, pos)
        for ticket, pos in mt5_user_data[username_key].get("positions", {}).items()
        if pos.get("symbol") == symbol
    ]


def _has_symbol_direction(username_key, symbol, direction):
    """Prevent duplicate same-side positions on the same symbol."""
    return any(
        pos.get("symbol") == symbol and pos.get("direction") == direction
        for pos in mt5_user_data[username_key].get("positions", {}).values()
    )


def _get_mt5_pip_size(symbol, sym_info):
    """Return an effective pip size for MT5 pricing."""
    point = float(sym_info.get("point", 0.00001) or 0.00001)
    digits = int(sym_info.get("digits", 5) or 5)
    symbol_upper = symbol.upper()

    if "XAU" in symbol_upper or "GOLD" in symbol_upper:
        return 0.01

    if digits in (3, 5):
        return point * 10

    return point


def _calculate_mt5_sl_tp(symbol, direction, price, sym_info, tp_pips=None, sl_pips=None):
    """Calculate fixed-pip stop loss and take profit.
    
    Args:
        symbol: Trading pair (e.g., "XAUUSD")
        direction: "BUY" or "SELL"
        price: Current entry price
        sym_info: Symbol info dict with 'digits' and 'point'
        tp_pips: TP distance in pips (uses balance-based default if None)
        sl_pips: SL distance in pips (uses balance-based default if None)
    """
    # Use provided pips or fall back to global config
    if tp_pips is None:
        tp_pips = TAKE_PROFIT_PIPS
    if sl_pips is None:
        sl_pips = STOP_LOSS_PIPS
        
    digits = int(sym_info["digits"])
    pip_size = _get_mt5_pip_size(symbol, sym_info)
    tp_distance = tp_pips * pip_size
    sl_distance = sl_pips * pip_size

    if direction == "BUY":
        sl = round(price - sl_distance, digits)
        tp = round(price + tp_distance, digits)
    else:
        sl = round(price + sl_distance, digits)
        tp = round(price - tp_distance, digits)

    return sl, tp


def is_mt5_trading_active(username_key):
    """Check if MT5 trading is active for a user"""
    if username_key not in mt5_user_data:
        return False
    return mt5_user_data[username_key].get("bot_status") == "Running"


def get_mt5_trading_mode(username_key):
    """Get MT5 trading mode"""
    if username_key not in mt5_user_data:
        return None
    return mt5_user_data[username_key].get("trading_mode")


def initialize_mt5_session(username_key, telegram_id):
    """Initialize MT5 trading session for a user"""
    mt5_user_data[username_key] = {
        "telegram_id": telegram_id,
        "bot_status": "Not Running",
        "trading_mode": None,
        "ctx": None,  # MT5UserContext — set when trading starts
        "lock": asyncio.Lock(),
        "positions": {},  # ticket -> position data
        "active_symbols": FOREX_SYMBOLS.copy(),
        "last_status_time": datetime.now(),
        "crash_notification_sent": False,  # Track if crash notification already sent
        "cooldown_notification_sent": False,  # Track if cooldown notification already sent
        "market_closed_notification_sent": False,  # Track if market closed notification already sent
    }
    print(f"[MT5-SESSION] ✅ Initialized MT5 session for {username_key}")
    return mt5_user_data[username_key]


async def start_mt5_trading(bot, telegram_id, username_key, existing_ctx=None):
    """Start MT5 forex trading loop for a user via MetaAPI"""
    print(f"[MT5-TRADING] 🚀 Starting MT5 trading for {username_key}")

    # RESET crash mode on new session (fresh start after restart)
    mt5_crash_protector.reset_crash_mode()
    print(f"[MT5-TRADING] 🔄 Crash protection reset for fresh trading session")

    if username_key not in mt5_user_data:
        initialize_mt5_session(username_key, telegram_id)
    
    # Reset crash notification flag for fresh start
    mt5_user_data[username_key]["crash_notification_sent"] = False
    mt5_user_data[username_key]["cooldown_notification_sent"] = False
    mt5_user_data[username_key]["market_closed_notification_sent"] = False

    if existing_ctx is not None:
        ctx = existing_ctx
    else:
        from user_control.add_users import get_user_by_telegram_id
        user_data, _ = get_user_by_telegram_id(telegram_id)

        if not user_data:
            print(f"[MT5-TRADING] ❌ User not found in database: {telegram_id}")
            return False

        metaapi_account_id = user_data.get("metaapi_account_id")
        if not metaapi_account_id:
            print(f"[MT5-TRADING] ❌ MetaAPI account ID not found for user: {telegram_id}")
            return False

        ctx = await create_user_context(telegram_id, metaapi_account_id, bot=bot, must_connect=True)
        if ctx is None:
            print(f"[MT5-TRADING] ❌ Cannot connect to MetaAPI for user: {telegram_id}")
            return False

    mt5_user_data[username_key]["ctx"] = ctx

    # Resolve active symbols for this user's broker (e.g. XAUUSDm, GOLD, etc.)
    resolved_symbols = []
    for sym in FOREX_SYMBOLS:
        real_sym = await resolve_symbol(ctx, sym)
        if real_sym and real_sym not in resolved_symbols:
            resolved_symbols.append(real_sym)
        elif not real_sym:
            print(f"[MT5-TRADING] ⚠️ Could not resolve '{sym}' on this broker — skipping")
    
    if not resolved_symbols:
        print(f"[MT5-TRADING] ⚠️ No symbols resolved! Falling back to raw FOREX_SYMBOLS: {FOREX_SYMBOLS}")
        resolved_symbols = list(FOREX_SYMBOLS)
    else:
        print(f"[MT5-TRADING] ✅ Resolved symbols for broker: {resolved_symbols}")
    
    mt5_user_data[username_key]["active_symbols"] = resolved_symbols

    mt5_user_data[username_key]["trading_mode"] = "MT5 Forex Trading"
    mt5_user_data[username_key]["bot_status"] = "Running"

    if username_key not in mt5_user_tasks:
        mt5_user_tasks[username_key] = {}

    task = asyncio.create_task(
        mt5_trade_loop(username_key, bot, telegram_id)
    )
    mt5_user_tasks[username_key]["forex"] = task

    print(f"[MT5-TRADING] ✅ MT5 trading task created for {username_key}")
    return True


async def stop_mt5_trading(username_key):
    """Stop MT5 trading loop and disconnect user"""
    print(f"[MT5-TRADING] 🛑 Stopping MT5 trading for {username_key}")

    if username_key in mt5_user_data:
        telegram_id = mt5_user_data[username_key].get("telegram_id")
        mt5_user_data[username_key]["bot_status"] = "Stopped"
        # Reset crash notification flag for next session
        mt5_user_data[username_key]["crash_notification_sent"] = False
        mt5_user_data[username_key]["cooldown_notification_sent"] = False
        mt5_user_data[username_key]["market_closed_notification_sent"] = False
        mt5_user_data[username_key]["insufficient_funds_notification_sent"] = False

        if username_key in mt5_user_tasks:
            for task_name, task in mt5_user_tasks[username_key].items():
                if task and not task.done():
                    task.cancel()
                    print(f"[MT5-TRADING] Cancelled {task_name} task for {username_key}")
            mt5_user_tasks[username_key].clear()

        # Disconnect MetaAPI connection for this user
        ctx = mt5_user_data[username_key].get("ctx")
        if ctx:
            await disconnect_mt5(ctx.telegram_id)
            mt5_user_data[username_key]["ctx"] = None
        
        # 🧹 CLEANUP: Remove session from memory to prevent accumulation
        from utils.cleanup_utils import cleanup_mt5_session, cleanup_crash_protection_data, cleanup_metaapi_connections
        cleanup_mt5_session(username_key)
        cleanup_crash_protection_data(telegram_id)
        if telegram_id:
            await cleanup_metaapi_connections(telegram_id)

        # Stop crash monitor if no MT5 users are still trading
        any_active = any(
            v.get("bot_status") == "Running"
            for k, v in mt5_user_data.items()
            if k != username_key
        )
        if not any_active:
            mt5_crash_protector.stop_monitor()

        return True
    return False


async def mt5_trade_loop(username: str, bot, telegram_id: int):
    """Main MT5 trading loop — scans forex pairs via MetaAPI, opens/manages positions"""
    print(f"[MT5-LOOP] 🚀 ENTERED mt5_trade_loop for {username} at {datetime.now()}")
    print(
        "[MT5-CONFIG] "
        f"SignalTF={SIGNAL_TIMEFRAME} | TrendTF={TREND_TIMEFRAME} | "
        f"SupportResistanceTF={SUPPORT_RESISTANCE_TIMEFRAME} | "
        f"CrashTF={MT5_CRASH_MONITORING_TIMEFRAME} x {MT5_CRASH_MONITORING_CANDLES}"
    )
    print(
        "[MT5-CONFIG] "
        f"StochRSI: RSI={RSI_PERIOD}, Stoch={STOCH_RSI_PERIOD}, "
        f"K={STOCH_K_SMOOTH}, D={STOCH_D_SMOOTH}, "
        f"Sell>={STOCH_RSI_SHORT_LEVEL}, Buy<={STOCH_RSI_BUY_LEVEL}"
    )

    last_status_time = datetime.now()
    loop_count = 0

    while mt5_user_data[username]["bot_status"] == "Running":
        loop_count += 1

        ctx = mt5_user_data[username].get("ctx")
        if ctx is None:
            print(f"[MT5-LOOP] ❌ No MetaAPI context for {username}")
            await asyncio.sleep(10)
            continue

        # === CRASH PROTECTION ===
        try:
            cr_sym = _mt5_crash_reference_symbol(username)
            crash_result = await mt5_crash_protector.check_for_crash(ctx, reference_symbol=cr_sym)
            if crash_result.get('is_crashing'):
                print(f"[MT5-LOOP] 🚨 CRASH DETECTED: {crash_result.get('reason')}")
                
                # Notify user about crash detection (once per hour)
                last_notification_time = mt5_user_data[username].get('last_crash_notification_sent', None)
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
                        title = "🚨 MARKET PUMP DETECTED - MT5 TRADING HALTED"
                        desc = f"📈 <b>{cr_sym} pumped {drop_pct:.1f}%</b>\n💰 Price: ${window_low:.2f} → ${current_price:.2f}"
                    else:
                        title = "🚨 MARKET CRASH DETECTED - MT5 TRADING HALTED"
                        desc = f"📊 <b>{cr_sym} dropped {drop_pct:.1f}%</b>\n💰 Price: ${window_high:.2f} → ${current_price:.2f}"
                    
                    try:
                        hours = max(1, CRASH_COOLDOWN_MINUTES // 60)
                        bot.send_message(
                            chat_id=telegram_id,
                            text=f"<b>{title}</b>\n"
                                 f"━━━━━━━━━━━━━━━━━━━━\n\n"
                                 f"{desc}\n"
                                 f"⏱️ Timeframe: Today (D1)\n\n"
                                 f"🛡️ <b>Actions Taken:</b>\n"
                                 f"✅ All positions closed\n"
                                 f"✅ Trading paused for {hours} hours\n\n"
                                 f"⏳ <b>Cooldown:</b> {hours} hours active\n\n"
                                 f"💡 <b>To skip waiting:</b> Stop trading and restart from main menu",
                            parse_mode='HTML'
                        )
                        mt5_user_data[username]['last_crash_notification_sent'] = now
                        mt5_user_data[username]['cooldown_notification_sent'] = False  # Reset cooldown flag for fresh crash
                    except Exception as e:
                        print(f"[MT5-LOOP] ⚠️ Failed to send crash notification: {e}")
                
                await mt5_crash_protector.emergency_close_all(
                    ctx, username, mt5_user_data, bot, telegram_id
                )
                print(f"[MT5-LOOP] ⏸️ MT5 Trading paused — crash cooldown active")
                await asyncio.sleep(60)
                continue

            balance = await get_account_balance(ctx)
            mt5_crash_protector.set_daily_start_balance(telegram_id, balance)
            trading_allowed, block_reason = mt5_crash_protector.is_trading_allowed(telegram_id, balance)
            
            # SAFETY CHECK: If cooldown expired naturally, auto-reset crash mode
            if "Crash cooldown" in block_reason and mt5_crash_protector.crash_triggered_at:
                elapsed = (datetime.now() - mt5_crash_protector.crash_triggered_at).total_seconds() / 60
                if elapsed >= CRASH_COOLDOWN_MINUTES:
                    print(f"[MT5-LOOP] 🔄 Crash cooldown expired naturally - resetting crash mode")
                    mt5_crash_protector.reset_crash_mode()
                    mt5_user_data[username]['last_crash_notification_sent'] = None  # Reset notification timestamp
                    mt5_user_data[username]['cooldown_notification_sent'] = False  # Reset cooldown flag
                    trading_allowed, block_reason = mt5_crash_protector.is_trading_allowed(telegram_id, balance)
            
            if not trading_allowed:
                print(f"[MT5-LOOP] ⛔ Trading blocked: {block_reason}")
                # Notify user about crash cooldown (throttled to 1 hour)
                if "Crash cooldown" in block_reason:
                    now = datetime.now()
                    last_cooldown_notif = mt5_user_data[username].get('last_cooldown_notification_sent')
                    should_notify_cooldown = False
                    
                    if not last_cooldown_notif or (now - last_cooldown_notif).total_seconds() >= 3600:
                        should_notify_cooldown = True
                    
                    if should_notify_cooldown:
                        try:
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"<b>🚨 XM Forex Gold (MT5) Trading Status: PAUSED</b>\n"
                                     f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                                     f"⏸️ <b>Reason:</b> Market Crash Protection\n"
                                     f"📌 <b>Status:</b> {block_reason}\n\n"
                                     f"⏰ <i>Trading will automatically resume after the cooldown period.</i>",
                                parse_mode='HTML'
                            )
                            # Update the hourly throttle timestamp
                            mt5_user_data[username]['last_cooldown_notification_sent'] = now
                            mt5_user_data[username]['cooldown_notification_sent'] = True
                        except Exception as e:
                            print(f"[MT5-LOOP] ⚠️ Failed to send crash notification: {e}")
                await asyncio.sleep(30)
                continue
        except Exception as e:
            print(f"[MT5-LOOP] ⚠️ Crash protection check error: {e}")

        # Reconnect if needed
        if not await is_mt5_connected(telegram_id):
            print(f"[MT5-LOOP] ⚠️ MetaAPI disconnected, attempting reconnect...")
            user_data_db, _ = None, None
            try:
                from user_control.add_users import get_user_by_telegram_id
                user_data_db, _ = get_user_by_telegram_id(telegram_id)
            except Exception:
                pass
            if user_data_db:
                metaapi_account_id = user_data_db.get("metaapi_account_id")
                if metaapi_account_id:
                    new_ctx = await create_user_context(telegram_id, metaapi_account_id, bot=bot, must_connect=True)
                    if new_ctx:
                        mt5_user_data[username]["ctx"] = new_ctx
                        ctx = new_ctx
                    else:
                        print(f"[MT5-LOOP] ❌ Reconnect failed, waiting 30s...")
                        await asyncio.sleep(30)
                        continue
                else:
                    await asyncio.sleep(30)
                    continue
            else:
                await asyncio.sleep(30)
                continue

        # Sync positions with MetaAPI
        try:
            await sync_mt5_positions(username, ctx, bot, telegram_id)
            active_count = await get_active_positions_count(ctx, magic_only=True)
            balance = await get_account_balance(ctx)
        except Exception as e:
            print(f"[MT5-LOOP] ⚠️ Error syncing positions or balance: {e}")
            await asyncio.sleep(5)
            continue

        print(f"\n[MT5-LOOP] 🔄 Loop #{loop_count} | User: {username} | Balance: ${balance:.2f} | Active: {active_count}/{MAX_CONCURRENT_TRADES}")

        # Periodic status message (every 1 hour)
        now = datetime.now()
        if (now - last_status_time).total_seconds() > 3600:
            try:
                bot.send_message(
                    chat_id=telegram_id,
                    text=f"📊 <b>MT5 Hourly Update</b>\n"
                         f"💰 Balance: <code>${balance:.2f}</code>\n"
                         f"📈 Active Trades: {active_count}/{MAX_CONCURRENT_TRADES}",
                    parse_mode='HTML'
                )
                last_status_time = now
            except Exception as e:
                print(f"[MT5-LOOP] ⚠️ Failed to send status: {e}")

        # Process each symbol (iterate over a copy so we can mutate the list)
        active_symbols = mt5_user_data[username].get("active_symbols", list(FOREX_SYMBOLS))
        for symbol in list(active_symbols):
            if mt5_user_data[username]["bot_status"] != "Running":
                break

            try:
                await asyncio.sleep(SLEEP_BETWEEN_SYMBOLS)

                # Manage any existing positions for this symbol first.
                for ticket, _ in _get_positions_for_symbol(username, symbol):
                    await manage_mt5_position(username, ticket, bot, telegram_id, ctx)

                # Skip if at max trades
                if active_count >= MAX_CONCURRENT_TRADES:
                    continue

                # Analyze for new entry
                bid, ask = await get_current_price(ctx, symbol)
                if bid is None:
                    print(f"[MT5-LOOP] ❌ {symbol} — no price from broker, removing from active list")
                    if symbol in active_symbols:
                        active_symbols.remove(symbol)
                        mt5_user_data[username]["active_symbols"] = active_symbols
                    # If all symbols exhausted, attempt full re-resolution
                    if not active_symbols:
                        print(f"[MT5-LOOP] 🔄 All symbols exhausted — re-resolving from FOREX_SYMBOLS...")
                        for sym in FOREX_SYMBOLS:
                            real = await resolve_symbol(ctx, sym)
                            if real and real not in active_symbols:
                                active_symbols.append(real)
                        mt5_user_data[username]["active_symbols"] = active_symbols
                        print(f"[MT5-LOOP] ✅ Re-resolved symbols: {active_symbols}")
                    continue

                sym_info = await get_symbol_info(ctx, symbol)
                if sym_info is None:
                    print(f"[MT5-LOOP] ❌ {symbol} — no symbol spec from broker, removing from active list")
                    if symbol in active_symbols:
                        active_symbols.remove(symbol)
                        mt5_user_data[username]["active_symbols"] = active_symbols
                    if not active_symbols:
                        print(f"[MT5-LOOP] 🔄 All symbols exhausted — re-resolving from FOREX_SYMBOLS...")
                        for sym in FOREX_SYMBOLS:
                            real = await resolve_symbol(ctx, sym)
                            if real and real not in active_symbols:
                                active_symbols.append(real)
                        mt5_user_data[username]["active_symbols"] = active_symbols
                        print(f"[MT5-LOOP] ✅ Re-resolved symbols: {active_symbols}")
                    continue

                support = await find_support_level_mt5(ctx, symbol)
                resistance = await find_resistance_level_mt5(ctx, symbol)

                if support is None or resistance is None:
                    print(f"[MT5-LOOP] ⏭️ Skipping {symbol} — cannot calculate support/resistance")
                    continue

                # gap_pct = (resistance - support) / support
                # if gap_pct < 0.0005:
                #     print(f"[MT5-LOOP] ⏭️ Skipping {symbol} — S/R gap too small ({gap_pct*100:.2f}%)")
                #     continue
                gap_pct = (resistance - support) / support
                print(f"[MT5-LOOP] 📐 {symbol} S/R: support={support:.5f} resistance={resistance:.5f} gap={gap_pct*100:.3f}%")
                if gap_pct < 0.0005:
                    print(f"[MT5-LOOP] ⏭️ Skipping {symbol} — S/R gap too small ({gap_pct*100:.3f}%), need >= 0.050%")
                    continue

                print(f"[MT5-LOOP] 🔬 [{username}] Running signal analysis for {symbol}...")
                signal, details = await get_trade_signal_mt5(ctx, symbol, bid, support, resistance)

                k = details.get('stoch_rsi_k', '-')
                d = details.get('stoch_rsi_d', '-')
                crossover = details.get('crossover', '-')
                score = details.get('score', '-')
                reasons = details.get('reasons', [])
                price_pos = details.get('price_position', None)
                print(f"[MT5-LOOP] {symbol} @ {bid:.5f} | StochRSI K={k}, D={d}, {crossover} | Signal={signal} | Score={score}")
                if reasons:
                    print(f"[MT5-LOOP]   Reasons: {' | '.join(str(r) for r in reasons)}")
                if price_pos is not None:
                    print(f"[MT5-LOOP]   Price position in S/R range: {price_pos*100:.1f}% | Support={support:.5f} | Resistance={resistance:.5f} | Bid={bid:.5f}")

                if signal == "NO_TRADE":
                    continue

                if _has_symbol_direction(username, symbol, signal):
                    print(f"[MT5-LOOP] ⏭️ Skipping {symbol} {signal} — same direction already open")
                    continue

                # ===== LOW BALANCE GUARD =====
                # Refresh balance before every trade attempt (it may have changed)
                balance = await get_account_balance(ctx)
                if balance < MT5_MIN_BALANCE_TO_TRADE:
                    print(f"[MT5-LOOP] ⚠️ Balance too low to trade: ${balance:.2f} < ${MT5_MIN_BALANCE_TO_TRADE} minimum")
                    if not mt5_user_data[username].get('low_balance_notification_sent', False):
                        try:
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"⚠️ <b>Balance Too Low to Trade</b>\n"
                                     f"━━━━━━━━━━━━━━━━━\n\n"
                                     f"💰 <b>Current Balance:</b> <code>${balance:.2f}</code>\n"
                                     f"🔒 <b>Minimum Required:</b> <code>${MT5_MIN_BALANCE_TO_TRADE:.2f}</code>\n\n"
                                     f"🛑 Trading is paused to protect your account.\n\n"
                                     f"💡 <i>Please top up your broker account to resume trading.</i>",
                                parse_mode='HTML'
                            )
                            mt5_user_data[username]['low_balance_notification_sent'] = True
                        except Exception as _e:
                            print(f"[MT5-LOOP] ⚠️ Failed to send low-balance notification: {_e}")
                    continue
                else:
                    # Reset the notification flag when balance recovers
                    mt5_user_data[username]['low_balance_notification_sent'] = False

                # Get balance-based parameters for lot size and pip ranges
                # Note: balance was already fetched freshly in the low-balance guard above
                balance_params = get_mt5_balance_based_params(balance)
                lot_size = balance_params["lot"]
                tp_pips = balance_params["tp_pips"]
                sl_pips = balance_params["sl_pips"]

                # Calculate SL/TP with balance-based pip values
                price = ask if signal == "BUY" else bid
                digits = sym_info["digits"]

                sl, tp = _calculate_mt5_sl_tp(symbol, signal, price, sym_info, tp_pips=tp_pips, sl_pips=sl_pips)

                print(f"[MT5-LOOP] 💰 Balance=${balance:.2f} | Balance tier: Lot={lot_size}, TP={tp_pips}pip, SL={sl_pips}pip")
                print(f"[MT5-LOOP] 📋 Order details: {signal} {symbol} | Lot={lot_size} | Entry~{price:.5f} | SL={sl:.5f} | TP={tp:.5f} | Digits={digits}")
                print(f"[MT5-LOOP]   SL distance: {abs(price - sl):.5f} ({sl_pips} pips) | TP distance: {abs(tp - price):.5f} ({tp_pips} pips)")

                # ===== PRE-TRADE CRASH SAFETY CHECK (Gold drawdown — same as main loop) =====
                crash_ref = _mt5_crash_reference_symbol(username)
                is_safe_to_trade, safety_reason = await mt5_crash_protector.is_safe_to_open_position(
                    ctx, reference_symbol=crash_ref
                )
                if not is_safe_to_trade:
                    print(f"[MT5-LOOP] ⚠️ TRADE BLOCKED for {symbol}: {safety_reason}")
                    continue

                # Place order via MetaAPI with retry logic
                comment = f"StochRSI {signal}"
                result, is_success, error_msg = await open_position_with_retry(
                    ctx, symbol, signal, lot_size, sl, tp, comment, 
                    max_retries=3, backoff_seconds=2
                )

                print(f"[MT5-LOOP] 📬 Order result: success={is_success} | error={error_msg} | raw={result}")

                if result is not None and is_success:
                    position_id = _normalize_ticket(result.get('positionId', result.get('orderId', '')))
                    open_price = result.get('openPrice', result.get('price', price))

                    # Verify fill price from broker — MetaAPI result does not always
                    # include the actual execution price; reading the open position
                    # gives the true broker fill price and avoids slippage mismatch.
                    try:
                        await asyncio.sleep(1)
                        live_positions = await get_open_positions(ctx, magic_only=False)
                        for lp in live_positions:
                            if _normalize_ticket(lp.get('ticket')) == position_id:
                                broker_fill = lp.get('price_open', 0)
                                if broker_fill and broker_fill > 0:
                                    if abs(broker_fill - open_price) > 0.0001:
                                        print(f"[MT5-LOOP] 📌 Fill price corrected: {open_price:.5f} → {broker_fill:.5f}")
                                    open_price = broker_fill
                                break
                    except Exception as _ep:
                        print(f"[MT5-LOOP] ⚠️ Could not verify fill price: {_ep}")

                    print(f"[MT5-LOOP] ✅ Opened {signal} {symbol} @ {open_price:.5f} | Ticket={position_id}")
                    mt5_user_data[username]["positions"][position_id] = {
                        "ticket": position_id,
                        "symbol": symbol,
                        "direction": signal,
                        "entry": open_price,
                        "opened_balance": balance,
                        "sl": sl,
                        "tp": tp,
                        "volume": lot_size,
                        "highest_profit_pips": 0.0,
                        "breakeven_set": False,
                        "trailing_active": False,
                    }
                    active_count += 1

                    # Send Telegram notification
                    emoji = "🚀" if signal == "BUY" else "🔻"
                    bot.send_message(
                        chat_id=telegram_id,
                        text=f"<b>{emoji} {signal} Position Opened (MT5)</b>\n"
                             f"━━━━━━━━━━━━━━━━━\n\n"
                             f"💱 <b>Pair:</b> <code>{html.escape(symbol)}</code>\n"
                             f"📈 <b>Entry:</b> <code>{open_price:.5f}</code>\n"
                             f"🎯 <b>TP:</b> <code>{tp:.5f}</code>\n"
                             f"🛑 <b>SL:</b> <code>{sl:.5f}</code>\n"
                             f"📊 <b>Lot:</b> <code>{lot_size}</code>\n"
                             f"📈 <b>Balance:</b> <code>${balance:.2f}</code>\n"
                             f"🔢 <b>Active:</b> {active_count}/{MAX_CONCURRENT_TRADES}",
                        parse_mode='HTML'
                    )
                    mt5_crash_protector.record_trade(telegram_id)
                elif error_msg:
                    print(f"[MT5-LOOP] ❌ Order failed for {symbol}: {error_msg}")
                    
                    # Detect market closed and notify user once
                    if "market is closed" in error_msg.lower() and not mt5_user_data[username].get('market_closed_notification_sent', False):
                        try:
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"<b>🚫 FOREX MARKET CLOSED</b>\n"
                                     f"━━━━━━━━━━━━━━━━━\n\n"
                                     f"📅 <b>Status:</b> Market is closed\n\n"
                                     f"⏰ <i>Waiting for market to open...</i>\n\n"
                                     f"ℹ️ <i>Bot will resume trading when market reopens.</i>",
                                parse_mode='HTML'
                            )
                            mt5_user_data[username]['market_closed_notification_sent'] = True
                        except Exception as e:
                            print(f"[MT5-LOOP] ⚠️ Failed to send market closed notification: {e}")

                    # Detect insufficient funds and notify user once
                    if "not enough money" in error_msg.lower() and not mt5_user_data[username].get('insufficient_funds_notification_sent', False):
                        try:
                            bot.send_message(
                                chat_id=telegram_id,
                                text=f"<b>⚠️ INSUFFICIENT FUNDS</b>\n"
                                     f"━━━━━━━━━━━━━━━━━\n\n"
                                     f"❌ <b>Trade Rejected:</b> Not enough money to open position.\n"
                                     f"💱 <b>Pair:</b> {symbol}\n\n"
                                     f"Please top up your broker account to resume trading.",
                                parse_mode='HTML'
                            )
                            mt5_user_data[username]['insufficient_funds_notification_sent'] = True
                        except Exception as e:
                            print(f"[MT5-LOOP] ⚠️ Failed to send insufficient funds notification: {e}")

            except Exception as e:
                print(f"[MT5-LOOP] ❌ Critical error in loop for {username}: {e}")
                import traceback
                traceback.print_exc()

        print(f"[MT5-LOOP] 😴 Sleeping {SCAN_INTERVAL_SECONDS}s...")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    print(f"[MT5-LOOP] 🛑 Exiting trade loop for {username}")


async def sync_mt5_positions(username, ctx, bot=None, telegram_id=None):
    """Sync internal state with actual MetaAPI positions"""
    open_pos = await get_open_positions(ctx, magic_only=True)
    if open_pos is None:
        print(f"[MT5-SYNC] ⚠️ Skipping sync for {username} — REST API failure")
        return
    open_tickets = {_normalize_ticket(p["ticket"]): p for p in open_pos}

    # Update last-known broker profit for every STILL-OPEN position.
    # The 'profit' field from get_open_positions is the broker's live P&L
    # (includes swap + commission). We snapshot it every cycle so that if
    # deal history is unavailable at close time we still have real broker data.
    for ticket, broker_pos in open_tickets.items():
        if ticket in mt5_user_data[username].get("positions", {}):
            mt5_user_data[username]["positions"][ticket]["last_broker_profit"] = broker_pos.get("profit")

    # Remove closed positions
    for ticket, pos in list(mt5_user_data[username]["positions"].items()):
        if ticket not in open_tickets:
            tracked_positions_before_close = len(mt5_user_data[username].get("positions", {}))
            symbol = pos.get("symbol", "Unknown")
            direction = pos.get("direction", "Unknown")
            entry_price = pos.get("entry", 0)
            volume = pos.get("volume", 0)

            print(f"[MT5-SYNC] Position closed on broker | Ticket={ticket} | {symbol} {direction}")

            realized_pnl = None
            pnl_pct = 0.0
            pnl_emoji = ""
            pnl_source = ""

            try:
                # ── Priority 1: deal history (broker's exact realised P&L) ──────
                for attempt in range(3):
                    real_profit, _ = await get_position_realized_pnl(ctx, ticket)
                    if real_profit is not None:
                        realized_pnl = real_profit
                        pnl_source = "Realized"
                        print(f"[MT5-SYNC] ✅ Broker deal P&L for {ticket}: ${real_profit:.2f} (attempt {attempt+1})")
                        break
                    if attempt < 2:
                        print(f"[MT5-SYNC] ⏳ Deal not yet in history, retrying ({attempt+1}/3)…")
                        await asyncio.sleep(1.0)

                # ── Priority 2: account balance delta (final realized result) ─────
                # MT5 bot is configured for a single concurrent position, so when the
                # last tracked position closes, balance delta is the most reliable
                # broker-final fallback if history APIs lag or return partial data.
                if realized_pnl is None:
                    open_balance = pos.get("opened_balance")
                    if (
                        tracked_positions_before_close == 1
                        and len(open_tickets) == 0
                        and open_balance is not None
                    ):
                        current_balance = await get_account_balance(ctx)
                        if current_balance and current_balance > 0:
                            realized_pnl = float(current_balance) - float(open_balance)
                            pnl_source = "Balance Delta"
                            print(
                                f"[MT5-SYNC] 💰 Using balance delta for {ticket}: "
                                f"${realized_pnl:.2f} (current=${current_balance:.2f} - open=${float(open_balance):.2f})"
                            )
                        else:
                            print(
                                f"[MT5-SYNC] ⚠️ Skipping balance-delta fallback for {ticket} "
                                f"because current_balance lookup failed: {current_balance!r}"
                            )

                # ── Priority 3: last live snapshot from open-position list ───────
                if realized_pnl is None:
                    last_snap = pos.get("last_broker_profit")
                    if last_snap is not None:
                        realized_pnl = float(last_snap)
                        pnl_source = "Snapshot"
                        print(f"[MT5-SYNC] 📸 Using last broker profit snapshot for {ticket}: ${realized_pnl:.2f}")

                print(f"[MT5-SYNC] Final P&L decision: {realized_pnl!r} via {pnl_source!r}")

                # ── Derive percentage from real P&L (no price calculation) ───────
                if realized_pnl is not None and entry_price > 0 and volume > 0:
                    sym_upper = symbol.upper()
                    if "XAU" in sym_upper or "GOLD" in sym_upper:
                        price_diff = realized_pnl / (100.0 * volume)
                    elif "JPY" in sym_upper:
                        price_diff = realized_pnl / (1000.0 * volume)
                    else:
                        price_diff = realized_pnl / (100000.0 * volume)

                    if direction == "BUY":
                        pnl_pct = (price_diff / entry_price) * 100
                    else:
                        pnl_pct = (-price_diff / entry_price) * 100

                    pnl_emoji = "🟢" if realized_pnl >= 0 else "🔴"

            except Exception as e:
                print(f"[MT5-SYNC] ⚠️ Could not retrieve P&L: {e}")

            del mt5_user_data[username]["positions"][ticket]
            if bot and telegram_id:
                try:
                    message = f"ℹ️ <b>Position Closed on Broker</b>\n\n"
                    message += f"💱 <b>Pair:</b> <code>{symbol}</code>\n"
                    message += f"📌 <b>Side:</b> <code>{direction}</code>\n"
                    message += f"🎫 <b>Ticket:</b> <code>{ticket}</code>\n"

                    if realized_pnl is not None:
                        pnl_sign = "+" if realized_pnl >= 0 else ""
                        pct_str = f" ({pnl_sign}{pnl_pct:.2f}%)" if pnl_pct != 0.0 else ""
                        message += f"\n{pnl_emoji} <b>P&L ({pnl_source}):</b> <code>{pnl_sign}${realized_pnl:.2f}{pct_str}</code>\n"
                    else:
                        message += f"\n⏳ <b>P&L:</b> <i>Pending from broker</i>\n"

                    message += f"\n🔢 <b>Active:</b> {len(open_tickets)}/{MAX_CONCURRENT_TRADES}\n\n"
                    message += f"<i>The bot will search for a new trade while a slot is free.</i>"

                    bot.send_message(
                        chat_id=telegram_id,
                        text=message,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    print(f"[MT5-SYNC] ⚠️ Failed to send close notification: {e}")

    # Add externally opened positions
    for ticket, p in open_tickets.items():
        if ticket not in mt5_user_data[username]["positions"]:
            mt5_user_data[username]["positions"][ticket] = {
                "ticket": ticket,
                "symbol": p["symbol"],
                "direction": p["type"],
                "entry": p["price_open"],
                "sl": p["sl"],
                "tp": p["tp"],
                "volume": p["volume"],
                "highest_profit_pips": 0.0,
                "breakeven_set": False,
                "trailing_active": False,
            }
            print(f"[MT5-SYNC] Loaded existing {p['type']} for {p['symbol']} | Ticket={ticket}")


async def manage_mt5_position(username, ticket, bot, telegram_id, ctx):
    """
    Manage open position with breakeven and trailing stop logic via MetaAPI.
    
    All thresholds are treated as PIPS (not percentages) for Forex/Gold scalping:
    - At BREAKEVEN_TRIGGER_PCT pips (20): Move SL to entry + 1 pip (breakeven)
    - At TRAILING_TRIGGER_PCT pips (15): Start trailing stop  
    - Trailing: SL follows at TRAILING_STOP_PCT pips (5) below peak profit
    """
    pos = mt5_user_data[username]["positions"].get(ticket)
    if not pos:
        return

    symbol = pos.get("symbol")
    if not symbol:
        return

    bid, ask = await get_current_price(ctx, symbol)
    if bid is None:
        return

    entry = pos["entry"]
    direction = pos["direction"]
    current_sl = pos["sl"]

    # Get pip size for this symbol (0.01 for Gold, broker-aware for others)
    sym_info = await get_symbol_info(ctx, symbol)
    if sym_info is None:
        return
    pip_size = _get_mt5_pip_size(symbol, sym_info)

    # Calculate current P/L in pips
    if direction == "BUY":
        current_price = bid
        pnl_pips = (current_price - entry) / pip_size
    else:
        current_price = ask
        pnl_pips = (entry - current_price) / pip_size

    # Update highest profit tracking (in pips)
    if pnl_pips > pos.get("highest_profit_pips", 0.0):
        pos["highest_profit_pips"] = pnl_pips

    print(f"[MT5-MANAGE] {symbol} {direction} | Entry: {entry:.5f} | P/L: {pnl_pips:.1f} pips | Peak: {pos.get('highest_profit_pips', 0.0):.1f} pips")

    # === BREAKEVEN LOGIC (triggered at BREAKEVEN_TRIGGER_PCT pips) ===
    if pnl_pips >= BREAKEVEN_TRIGGER_PCT and not pos.get("breakeven_set", False):
        if direction == "BUY":
            new_sl = entry + (1 * pip_size)  # +1 pip above entry to cover spread
        else:
            new_sl = entry - (1 * pip_size)  # -1 pip below entry for SELL
        
        result = await modify_position_sl(ctx, ticket, new_sl)
        if result:
            pos["sl"] = new_sl
            pos["breakeven_set"] = True
            print(f"[MT5-MANAGE] ✅ {symbol} BREAKEVEN SET | New SL: {new_sl:.5f}")
            bot.send_message(
                chat_id=telegram_id,
                text=f"🔒 <b>Breakeven Set</b>\n\n"
                     f"💱 <b>Pair:</b> <code>{symbol}</code>\n"
                     f"📊 <b>Profit:</b> <code>{pnl_pips:.1f} pips</code>\n"
                     f"🛑 <b>New SL:</b> <code>{new_sl:.5f}</code>\n\n"
                     f"<i>Stop loss moved to breakeven to protect capital.</i>",
                parse_mode='HTML'
            )
        return

    # === TRAILING STOP LOGIC (triggered at TRAILING_TRIGGER_PCT pips) ===
    if pnl_pips >= TRAILING_TRIGGER_PCT:
        pos["trailing_active"] = True
        
        # Lock in (peak_pips - trail_distance) pips of profit
        locked_pips = pos["highest_profit_pips"] - TRAILING_STOP_PCT
        
        if direction == "BUY":
            new_sl = entry + (locked_pips * pip_size)
            if new_sl > pos["sl"]:
                result = await modify_position_sl(ctx, ticket, new_sl)
                if result:
                    pos["sl"] = new_sl
                    print(f"[MT5-MANAGE] 📈 {symbol} TRAILING | New SL: {new_sl:.5f} | Locking {locked_pips:.1f} pips")
        else:
            new_sl = entry - (locked_pips * pip_size)
            if new_sl < pos["sl"]:
                result = await modify_position_sl(ctx, ticket, new_sl)
                if result:
                    pos["sl"] = new_sl
                    print(f"[MT5-MANAGE] 📉 {symbol} TRAILING | New SL: {new_sl:.5f} | Locking {locked_pips:.1f} pips")


async def get_mt5_balance_async(telegram_id):
    """Get MT5 account balance for a user via MetaAPI (async)"""
    username_key = f"user_{telegram_id}"
    if username_key not in mt5_user_data:
        return None
    ctx = mt5_user_data[username_key].get("ctx")
    if ctx is None:
        return None
    try:
        return await get_account_balance(ctx)
    except Exception:
        return None


def get_mt5_balance(telegram_id):
    """Get MT5 account balance — sync wrapper for callback handler compatibility."""
    try:
        from utils.bg_loop import loop
        import asyncio
        future = asyncio.run_coroutine_threadsafe(
            get_mt5_balance_async(telegram_id), loop
        )
        return future.result(timeout=15)
    except Exception as e:
        print(f"[MT5-BALANCE] Async balance fallback: {e}")
        return None


def get_mt5_trading_status(username_key):
    """Get MT5 trading status for a user"""
    if username_key not in mt5_user_data:
        return None

    return {
        "status": mt5_user_data[username_key].get("bot_status", "Unknown"),
        "trading_mode": mt5_user_data[username_key].get("trading_mode"),
        "active_trades": len(mt5_user_data[username_key].get("positions", {})),
        "balance": None,  # Use async version to get real-time balance
    }


async def get_detailed_mt5_status_async(username_key):
    """Get detailed MT5 trading status with P&L information (async)"""
    if username_key not in mt5_user_data:
        return None

    ctx = mt5_user_data[username_key].get("ctx")
    if ctx is None:
        return get_mt5_trading_status(username_key)

    try:
        # Get full account info via MetaAPI
        account_info = await get_account_info(ctx)

        balance = account_info.get('balance', 0)
        equity = account_info.get('equity', 0)
        margin = account_info.get('margin', 0)
        free_margin = account_info.get('free_margin', 0)
        unrealized_pnl = account_info.get('profit', 0)
        leverage = account_info.get('leverage', 0)
        currency = account_info.get('currency', 'USD')

        # Calculate PnL percentage
        if balance > 0:
            pnl_percentage = (unrealized_pnl / balance) * 100
        else:
            pnl_percentage = 0

        # Get open positions with details
        open_positions = await get_open_positions(ctx, magic_only=True)

        positions_detail = []
        for pos in open_positions:
            entry_price = pos.get('price_open', 0)
            current_price = pos.get('price_current', 0)
            pos_profit = pos.get('profit', 0)
            pos_type = pos.get('type', 'Unknown')

            # Calculate position PnL %
            if entry_price > 0:
                if pos_type == "BUY":
                    pos_pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pos_pnl_pct = ((entry_price - current_price) / entry_price) * 100
            else:
                pos_pnl_pct = 0

            positions_detail.append({
                "ticket": pos.get('ticket'),
                "symbol": pos.get('symbol', 'Unknown'),
                "side": pos_type,
                "volume": pos.get('volume', 0),
                "entry_price": entry_price,
                "current_price": current_price,
                "sl": pos.get('sl', 0),
                "tp": pos.get('tp', 0),
                "profit": pos_profit,
                "pnl_percentage": pos_pnl_pct,
                "time": pos.get('time'),
            })

        return {
            "status": mt5_user_data[username_key].get("bot_status", "Unknown"),
            "trading_mode": mt5_user_data[username_key].get("trading_mode"),
            "active_trades": len(positions_detail),
            "balance": balance,
            "equity": equity,
            "margin": margin,
            "free_margin": free_margin,
            "unrealized_pnl": unrealized_pnl,
            "pnl_percentage": pnl_percentage,
            "leverage": leverage,
            "currency": currency,
            "positions": positions_detail,
        }
    except Exception as e:
        print(f"[MT5-STATS] Error getting detailed status: {e}")
        return get_mt5_trading_status(username_key)


def get_detailed_mt5_status(username_key):
    """Get detailed status — sync wrapper for callback handler compatibility."""
    # Try to run async version if event loop is available
    try:
        from utils.bg_loop import loop
        import asyncio
        future = asyncio.run_coroutine_threadsafe(
            get_detailed_mt5_status_async(username_key), loop
        )
        return future.result(timeout=15)
    except Exception as e:
        print(f"[MT5-STATS] Async status fallback: {e}")
        return get_mt5_trading_status(username_key)
