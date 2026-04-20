"""
MT5 Forex Trading Bot — Standalone runner via MetaAPI Cloud
Scans forex pairs using StochRSI signals, opens/manages positions via MetaAPI.

Usage:
    uv run mt5_bot.py <metaapi_account_id>

Example:
    uv run mt5_bot.py abc123-def456-ghi789

Before running:
    1. Install dependencies:  uv sync
    2. Set METAAPI_TOKEN in config.py
    3. Provision an MT5 account via MetaAPI dashboard or the bot registration flow
"""
import asyncio
import sys
import argparse
from datetime import datetime, timedelta

from mt5.mt5_config import (
    FOREX_SYMBOLS,
    LOT_SIZE,
    MAX_CONCURRENT_TRADES,
    WIN_PERCENTAGE,
    LOSS_PERCENTAGE,
    BREAKEVEN_TRIGGER_PCT,
    TRAILING_TRIGGER_PCT,
    TRAILING_STOP_PCT,
    SCAN_INTERVAL_SECONDS,
    SLEEP_BETWEEN_SYMBOLS,
    MIN_SIGNAL_SCORE,
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
    close_position,
    modify_position_sl,
    find_support_level_mt5,
    find_resistance_level_mt5,
)
from mt5.mt5_signals import get_trade_signal_mt5


# =====================================================
# INTERNAL STATE
# =====================================================
positions_state = {}  # symbol -> {ticket, direction, entry, sl, tp, ...}


async def sync_positions(ctx):
    """Sync internal state with actual MetaAPI open positions."""
    open_pos = await get_open_positions(ctx, magic_only=True)
    open_symbols = {p["symbol"] for p in open_pos}

    # Remove closed positions from state
    for sym in list(positions_state.keys()):
        if sym not in open_symbols:
            print(f"[SYNC] Position for {sym} no longer open — removing from state")
            del positions_state[sym]

    # Add positions opened externally / after restart
    for p in open_pos:
        sym = p["symbol"]
        if sym not in positions_state:
            direction = p["type"]
            entry = p["price_open"]
            positions_state[sym] = {
                "ticket": p["ticket"],
                "direction": direction,
                "entry": entry,
                "sl": p["sl"],
                "tp": p["tp"],
                "volume": p["volume"],
                "highest_profit_pct": 0.0,  # Track peak profit for trailing
                "breakeven_set": False,      # Flag: SL moved to breakeven
                "trailing_active": False,    # Flag: trailing stop active
            }
            print(f"[SYNC] Loaded existing {direction} for {sym} @ {entry} (ticket {p['ticket']})")


def calculate_sl_tp(direction, price, sym_info):
    """Calculate SL and TP from config percentages."""
    if direction == "BUY":
        sl = price * (1 - LOSS_PERCENTAGE / 100)
        tp = price * (1 + WIN_PERCENTAGE / 100)
    else:
        sl = price * (1 + LOSS_PERCENTAGE / 100)
        tp = price * (1 - WIN_PERCENTAGE / 100)

    digits = sym_info["digits"]
    return round(sl, digits), round(tp, digits)


# =====================================================
# MAIN LOOP
# =====================================================
async def trading_loop(ctx):
    """One full scan of all forex symbols via MetaAPI."""
    await sync_positions(ctx)

    active_count = await get_active_positions_count(ctx)
    balance = await get_account_balance(ctx)
    print(f"\n{'='*60}")
    print(f"[SCAN] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
          f"Balance: ${balance:.2f} | Active: {active_count}/{MAX_CONCURRENT_TRADES}")
    print(f"{'='*60}")

    for symbol in FOREX_SYMBOLS:
        # ---------- Manage existing position ----------
        if symbol in positions_state:
            await manage_position(ctx, symbol)
            await asyncio.sleep(1)
            continue

        # ---------- Skip if at max trades ----------
        if active_count >= MAX_CONCURRENT_TRADES:
            continue

        # ---------- Analyze for new entry ----------
        try:
            bid, ask = await get_current_price(ctx, symbol)
            if bid is None:
                print(f"[SCAN] Could not get price for {symbol}, skipping")
                continue

            sym_info = await get_symbol_info(ctx, symbol)
            if sym_info is None:
                continue

            support = await find_support_level_mt5(ctx, symbol)
            resistance = await find_resistance_level_mt5(ctx, symbol)

            if support is None or resistance is None:
                continue

            gap_pct = (resistance - support) / support
            if gap_pct < 0.0005:
                print(f"[SCAN] {symbol} gap too small ({gap_pct*100:.3f}%), skipping ranging market")
                continue

            signal, details = await get_trade_signal_mt5(ctx, symbol, bid, support, resistance)

            print(f"[SCAN] {symbol} @ {bid:.5f} | "
                  f"StochRSI K={details.get('stoch_rsi_k', '-')}, D={details.get('stoch_rsi_d', '-')} "
                  f"Crossover={details.get('crossover', '-')} | "
                  f"RSI={details.get('rsi', '-')} | Trend={details.get('trend', '-')} | "
                  f"Signal={signal}")

            if signal == "NO_TRADE":
                continue

            # Calculate SL/TP
            price = ask if signal == "BUY" else bid
            sl, tp = calculate_sl_tp(signal, price, sym_info)

            # Place order via MetaAPI
            comment = f"StochRSI {signal}"
            result = await open_position(ctx, symbol, signal, LOT_SIZE, sl, tp, comment)

            if result is not None:
                position_id = result.get('positionId', result.get('orderId', ''))
                positions_state[symbol] = {
                    "ticket": position_id,
                    "direction": signal,
                    "entry": price,
                    "sl": sl,
                    "tp": tp,
                    "volume": LOT_SIZE,
                    "highest_profit_pct": 0.0,
                    "breakeven_set": False,
                    "trailing_active": False,
                }
                active_count += 1

                print(f"[TRADE] Opened {signal} {symbol} @ {price:.5f} | SL: {sl:.5f} TP: {tp:.5f}")
                print(f"[TRADE] Active positions: {active_count}/{MAX_CONCURRENT_TRADES}")

        except Exception as e:
            print(f"[SCAN] Error processing {symbol}: {e}")

        await asyncio.sleep(SLEEP_BETWEEN_SYMBOLS)


async def manage_position(ctx, symbol):
    """Manage an open position with breakeven and trailing stop logic via MetaAPI."""
    state = positions_state[symbol]
    bid, ask = await get_current_price(ctx, symbol)
    if bid is None:
        return

    entry = state["entry"]
    direction = state["direction"]
    ticket = state["ticket"]
    current_sl = state["sl"]

    if direction == "BUY":
        current_price = bid
        pnl_pct = ((current_price - entry) / entry) * 100
    else:
        current_price = ask
        pnl_pct = ((entry - current_price) / entry) * 100

    if pnl_pct > state["highest_profit_pct"]:
        state["highest_profit_pct"] = pnl_pct

    print(f"[MANAGE] {symbol} {direction} | Entry: {entry:.5f} | "
          f"Current: {current_price:.5f} | P/L: {pnl_pct:.2f}% | Peak: {state['highest_profit_pct']:.2f}%")

    # === BREAKEVEN LOGIC ===
    if pnl_pct >= BREAKEVEN_TRIGGER_PCT and not state["breakeven_set"]:
        if direction == "BUY":
            new_sl = entry * 1.001
        else:
            new_sl = entry * 0.999
        
        result = await modify_position_sl(ctx, ticket, new_sl)
        if result:
            state["sl"] = new_sl
            state["breakeven_set"] = True
            print(f"[MANAGE] ✅ {symbol} BREAKEVEN SET | New SL: {new_sl:.5f} (was {current_sl:.5f})")
        return

    # === TRAILING STOP LOGIC ===
    if pnl_pct >= TRAILING_TRIGGER_PCT:
        state["trailing_active"] = True
        locked_profit_pct = state["highest_profit_pct"] - TRAILING_STOP_PCT
        
        if direction == "BUY":
            new_sl = entry * (1 + locked_profit_pct / 100)
            if new_sl > state["sl"]:
                result = await modify_position_sl(ctx, ticket, new_sl)
                if result:
                    state["sl"] = new_sl
                    print(f"[MANAGE] 📈 {symbol} TRAILING SL | New SL: {new_sl:.5f} | Locking {locked_profit_pct:.1f}% profit")
        else:
            new_sl = entry * (1 - locked_profit_pct / 100)
            if new_sl < state["sl"]:
                result = await modify_position_sl(ctx, ticket, new_sl)
                if result:
                    state["sl"] = new_sl
                    print(f"[MANAGE] 📉 {symbol} TRAILING SL | New SL: {new_sl:.5f} | Locking {locked_profit_pct:.1f}% profit")


# =====================================================
# ENTRY POINT
# =====================================================
async def async_main(metaapi_account_id: str):
    """Async entry point for standalone MetaAPI trading bot."""
    STANDALONE_TELEGRAM_ID = 0  # Dummy ID for standalone mode

    print("=" * 60)
    print("   MT5 FOREX TRADING BOT (StochRSI via MetaAPI)")
    print("   Platform: XM via MetaAPI Cloud")
    print("=" * 60)

    ctx = await create_user_context(STANDALONE_TELEGRAM_ID, metaapi_account_id)
    if ctx is None:
        print("[FATAL] Cannot connect to MetaAPI. Exiting.")
        sys.exit(1)

    info = await get_account_info(ctx)
    print(f"\nAccount: {info.get('login')} | Balance: ${info.get('balance', 0):.2f} | "
          f"Leverage: 1:{info.get('leverage', 0)} | Server: {info.get('server')}\n")

    try:
        while True:
            if not await is_mt5_connected(STANDALONE_TELEGRAM_ID):
                print("[WARN] MetaAPI disconnected — reconnecting...")
                ctx = await create_user_context(STANDALONE_TELEGRAM_ID, metaapi_account_id)
                if ctx is None:
                    print("[WARN] Reconnect failed, retrying in 30s...")
                    await asyncio.sleep(30)
                    continue

            await trading_loop(ctx)

            print(f"\n[SLEEP] Waiting {SCAN_INTERVAL_SECONDS}s before next scan...\n")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[EXIT] Bot stopped by user")
    finally:
        await disconnect_mt5(STANDALONE_TELEGRAM_ID)
        print("[EXIT] Done.")


def main():
    parser = argparse.ArgumentParser(description="MT5 Forex Trading Bot (StochRSI via MetaAPI)")
    parser.add_argument("metaapi_account_id", type=str, help="MetaAPI account ID for the MT5 account")
    args = parser.parse_args()

    asyncio.run(async_main(args.metaapi_account_id))


if __name__ == "__main__":
    main()
