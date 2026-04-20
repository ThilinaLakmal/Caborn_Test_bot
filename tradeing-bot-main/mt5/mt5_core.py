"""
MT5 Connection & Core Trading Functions (MetaAPI Cloud)
Handles MT5 operations via MetaAPI for multi-user support.
All functions are async and take a context (ctx) parameter.

The MT5UserContext dataclass holds connection info for a specific user,
enabling multiple users to trade simultaneously through MetaAPI cloud.
"""
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import asyncio

from mt5.mt5_config import (
    MAGIC_NUMBER, DEVIATION, SUPPORT_RESISTANCE_TIMEFRAME,
)

# GLOBAL ANALYTICS CACHE
# Shares calculations like S/R and Trend across ALL users for common symbols.
# Structure: (symbol, timeframe) -> {"support": float, "resistance": float, "timestamp": datetime}
_global_sr_cache = {}
_global_sr_lock = asyncio.Lock()


# =====================================================
# USER CONTEXT — passed to all core functions
# =====================================================

@dataclass
class MT5UserContext:
    """
    Holds MetaAPI connection info for a single user.
    Each user gets their own context — enabling multi-user trading.
    """
    connection: Any           # MetaAPI RpcMetaApiConnection
    telegram_id: int          # User's Telegram ID
    metaapi_account_id: str   # User's MetaAPI account ID


# =====================================================
# METAAPI TIMEFRAME MAPPING
# =====================================================
TIMEFRAME_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "1H": "1h",
    "H4": "4h",
    "4H": "4h",
    "D1": "1d",
    "1D": "1d",
    "W1": "1w",
}


def _timeframe_to_minutes(timeframe):
    """Convert timeframe string to minutes."""
    return {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
    }.get(timeframe.upper(), 15)


# =====================================================
# CONNECTION (delegated to metaapi_manager)
# =====================================================

async def connect_mt5(telegram_id, metaapi_account_id, bot=None):
    """
    Connect to MT5 via MetaAPI for a specific user.
    """
    for attempt in range(1, 4):
        try:
            from mt5.metaapi_manager import get_user_connection
            # Use must_connect=True here because connect_mt5() is usually 
            # called when we WANT an active trading session.
            connection = await get_user_connection(telegram_id, metaapi_account_id, bot=bot, must_connect=True)
            if connection is not None:
                return True
            print(f"[MT5-CORE] ⚠️ Connection attempt {attempt}/3 returned None. Retrying...")
        except Exception as e:
            print(f"[MT5-CORE] ❌ Connection attempt {attempt}/3 failed: {e}")
        
        if attempt < 3:
            await asyncio.sleep(5)
            
    return False


async def disconnect_mt5(telegram_id):
    """Disconnect user's MetaAPI connection."""
    from mt5.metaapi_manager import disconnect_user
    await disconnect_user(telegram_id)


async def is_mt5_connected(telegram_id):
    """Check if user's MetaAPI connection is active."""
    from mt5.metaapi_manager import is_user_connected
    return await is_user_connected(telegram_id)


async def get_connection(telegram_id, metaapi_account_id, bot=None, must_connect=False):
    """Get the MetaAPI RPC connection for a user."""
    from mt5.metaapi_manager import get_user_connection
    return await get_user_connection(telegram_id, metaapi_account_id, bot=bot, must_connect=must_connect)


async def create_user_context(telegram_id, metaapi_account_id, bot=None, must_connect=False):
    """
    Create an MT5UserContext for a user — connects if needed.
 
    Args:
        telegram_id: User's Telegram ID
        metaapi_account_id: User's MetaAPI account ID
        bot: Optional Telegram bot instance for alerts
        must_connect: If True, forces the websocket to connect.
 
    Returns:
        MT5UserContext or None on failure
    """
    connection = await get_connection(telegram_id, metaapi_account_id, bot=bot, must_connect=must_connect)
    if connection is None:
        return None
    return MT5UserContext(
        connection=connection,
        telegram_id=telegram_id,
        metaapi_account_id=metaapi_account_id,
    )


async def _get_account_information_with_retry(ctx, attempts=3, delay_seconds=1.5):
    """Fetch account info with retry and one connection refresh on timeout."""
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            from utils.rate_limiter import rate_limiter
            await rate_limiter.metaapi_before_request()
            
            return await ctx.connection.get_account_information()
        except Exception as e:
            last_error = e
            print(f"[MT5] Account info request failed (attempt {attempt}/{attempts}): {e}")

            # On the second failed attempt, try refreshing the user's cached connection.
            if attempt == 2:
                try:
                    from mt5.metaapi_manager import get_user_connection
                    fresh_connection = await get_user_connection(
                        ctx.telegram_id,
                        ctx.metaapi_account_id,
                    )
                    if fresh_connection is not None and fresh_connection is not ctx.connection:
                        ctx.connection = fresh_connection
                        print("[MT5] 🔄 Refreshed MetaAPI connection after account-info timeout")
                except Exception as refresh_error:
                    print(f"[MT5] Connection refresh failed: {refresh_error}")

            if attempt < attempts:
                await asyncio.sleep(delay_seconds)

    raise last_error


# =====================================================
# ACCOUNT INFO
# =====================================================

async def get_account_balance(ctx):
    """Get account balance via REST API (Scalable to 1000+ users)."""
    try:
        from mt5.metaapi_manager import get_user_balance_rest
        return await get_user_balance_rest(ctx.telegram_id, ctx.metaapi_account_id)
    except Exception as e:
        print(f"[MT5] Error getting REST balance: {e}")
        return 0.0


async def get_account_equity(ctx):
    """Get account equity."""
    try:
        info = await _get_account_information_with_retry(ctx)
        return info.get('equity', 0.0)
    except Exception as e:
        print(f"[MT5] Error getting equity: {e}")
        return 0.0


async def get_account_info(ctx):
    """
    Get basic account info via REST for scaling.
    Note: Full info like leverage might require RPC, but for the main loop,
    balance is often enough. Uses REST to avoid 1000 active websockets.
    """
    try:
        balance = await get_account_balance(ctx)
        return {
            "balance": balance,
            "equity": balance, # Mock for REST
            "currency": "USD",
        }
    except Exception as e:
        print(f"[MT5] Error getting account info: {e}")
        return {}


# =====================================================
# MARKET DATA
# =====================================================

async def get_candles(ctx, symbol, timeframe="M15", count=100):
    """
    Fetch OHLCV candle data via MetaAPI historical data endpoint.

    Args:
        ctx: MT5UserContext
        symbol: Forex pair (e.g., "XAUUSD")
        timeframe: Timeframe string (M1, M5, M15, M30, H1, H4, D1, W1)
        count: Number of candles to fetch

    Returns:
        list of dicts with keys: time, open, high, low, close, volume
        or None on error
    """
    tf = TIMEFRAME_MAP.get(timeframe.upper())
    if tf is None:
        print(f"[MT5] Invalid timeframe: {timeframe}")
        return None

    try:
        from mt5.metaapi_manager import get_historical_candles_shared

        raw_candles = await get_historical_candles_shared(
            ctx.telegram_id, ctx.metaapi_account_id, symbol, tf, count
        )

        if not raw_candles or len(raw_candles) == 0:
            print(f"[MT5] No candle data for {symbol} {timeframe}")
            return None

        candles = []
        for c in raw_candles:
            time_val = c.get('time')
            if isinstance(time_val, str):
                try:
                    time_val = datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                except Exception:
                    time_val = datetime.utcnow()
            elif time_val is None:
                time_val = datetime.utcnow()

            candles.append({
                "time": time_val,
                "open": float(c.get('open', 0)),
                "high": float(c.get('high', 0)),
                "low": float(c.get('low', 0)),
                "close": float(c.get('close', 0)),
                "volume": float(c.get('tickVolume', c.get('volume', 0))),
            })

        # Normalize ordering to oldest -> newest for all indicator logic.
        # MetaAPI REST may return candles in descending (newest-first) order,
        # which would reverse the RSI/StochRSI calculation and produce wrong values.
        candles.sort(key=lambda c: c["time"])

        # DEBUG: verify actual candle interval matches requested timeframe
        if len(candles) >= 2:
            t0 = candles[-2]["time"]
            t1 = candles[-1]["time"]
            if hasattr(t0, 'timestamp') and hasattr(t1, 'timestamp'):
                delta_sec = abs(t1.timestamp() - t0.timestamp())
                print(f"[MT5-TF-CHECK] {symbol} requested={timeframe}({tf}) | candle interval={delta_sec/60:.0f}min | last={t1} | count={len(candles)}")

        return candles

    except Exception as e:
        print(f"[MT5] Error getting candles for {symbol}: {e}")
        return None


async def get_close_prices(ctx, symbol, timeframe="M15", count=100):
    """Fetch close prices as a list of floats."""
    candles = await get_candles(ctx, symbol, timeframe, count)
    if candles is None:
        return None
    return [c["close"] for c in candles]


async def get_current_price(ctx, symbol):
    """
    Get current bid/ask for a symbol (shared across users).

    Returns:
        tuple: (bid, ask) or (None, None) on error
    """
    try:
        from mt5.metaapi_manager import get_current_price_shared
        return await get_current_price_shared(ctx.telegram_id, ctx.metaapi_account_id, symbol)
    except Exception as e:
        print(f"[MT5] Error getting shared price for {symbol}: {e}")
        return None, None


async def get_symbol_info(ctx, symbol, silent=False):
    """Get symbol details (point, digits, trade sizes, etc.) - shared across users."""
    try:
        from mt5.metaapi_manager import get_symbol_info_shared
        return await get_symbol_info_shared(ctx.telegram_id, ctx.metaapi_account_id, symbol)
    except Exception as e:
        if not silent:
            print(f"[MT5] Error getting shared symbol info for {symbol}: {e}")
        return None

async def resolve_symbol(ctx, base_symbol):
    """
    Probes the broker for the correct symbol name variant.
    Tries many common broker-specific suffixes and names.
    Returns the first working variant, or None if nothing works.
    """
    if any(x in base_symbol.upper() for x in ["XAUUSD", "GOLD"]):
        # Comprehensive list of gold symbol variants across brokers
        # GOLD first — XM Global and many brokers list gold as "GOLD"
        alts = [
            "GOLD",         # XM Global, many CFD brokers
            "GOLDm",        # XM Micro accounts
            "GOLD.",        # XM dot variant
            "XAUUSD",       # Standard (IC Markets, Pepperstone, OANDA, Forex.com)
            "XAUUSDm",      # XM suffix
            "XAUUSD.a",     # Admirals / Admiral Markets
            "XAUUSD.pro",   # Pro accounts
            "XAUUSD.c",     # Cent accounts
            "XAUUSD_m",     # Some ECN brokers
            "XAUUSD+",      # Plus variants
            "GOLD.a",       # Admirals Gold
            "XAU/USD",      # Slash format (some platforms)
            "XAUUSD_i",     # Institutional variant
            "XAUUSD_ECN",   # ECN variant
            "XAUUSDH",      # Some hedge accounts
            "XAUUSDF",      # Some fixed spread accounts
        ]
    else:
        # Generic symbol: try base + common suffixes
        alts = [
            base_symbol,
            base_symbol + "m",
            base_symbol + ".a",
            base_symbol + ".pro",
            base_symbol + ".c",
            base_symbol + "+",
        ]

    for alt in alts:
        try:
            spec = await get_symbol_info(ctx, alt, silent=True)
            if spec is not None:
                print(f"[MT5-RESOLVE] ✅ '{base_symbol}' → '{alt}' on this broker")
                return alt
        except Exception:
            pass
    
    print(f"[MT5-RESOLVE] ❌ Could not find '{base_symbol}' in any known variant on this broker")
    return None

# =====================================================
# SUPPORT / RESISTANCE (from H4 candles)
# =====================================================

async def find_support_level_mt5(ctx, symbol, timeframe=SUPPORT_RESISTANCE_TIMEFRAME, count=200):
    """Find support level using 10th percentile of lows from candles."""
    # 1. Check global SR cache
    cache_key = (symbol, timeframe)
    async with _global_sr_lock:
        cache_entry = _global_sr_cache.get(cache_key)
        if cache_entry and (datetime.now() - cache_entry["timestamp"]).total_seconds() < 900: # 15 min cache
            if "support" in cache_entry:
                # print(f"[MT5] ♻️ Using SHARED Support for {symbol}")
                return cache_entry["support"]

    # 2. Not in cache — calculate
    candles = await get_candles(ctx, symbol, timeframe, count)
    if candles is None or len(candles) < 5:
        bid, _ = await get_current_price(ctx, symbol)
        return bid * 0.998 if bid else None

    lows = sorted([c["low"] for c in candles])
    idx = max(1, int(len(lows) * 0.1))
    support = lows[idx]
    
    # 3. Update cache (both S and R since we usually fetch them together)
    async with _global_sr_lock:
        if cache_key not in _global_sr_cache:
            _global_sr_cache[cache_key] = {"timestamp": datetime.now()}
        _global_sr_cache[cache_key]["support"] = support
        _global_sr_cache[cache_key]["timestamp"] = datetime.now()

    print(f"[MT5-SUPPORT] {symbol}: Min={min(lows):.5f}, Support(10%)={support:.5f}")
    return support


async def find_resistance_level_mt5(ctx, symbol, timeframe=SUPPORT_RESISTANCE_TIMEFRAME, count=200):
    """Find resistance level using 90th percentile of highs from candles."""
    # 1. Check global SR cache
    cache_key = (symbol, timeframe)
    async with _global_sr_lock:
        cache_entry = _global_sr_cache.get(cache_key)
        if cache_entry and (datetime.now() - cache_entry["timestamp"]).total_seconds() < 900: # 15 min cache
            if "resistance" in cache_entry:
                # print(f"[MT5] ♻️ Using SHARED Resistance for {symbol}")
                return cache_entry["resistance"]

    # 2. Not in cache — calculate
    candles = await get_candles(ctx, symbol, timeframe, count)
    if candles is None or len(candles) < 5:
        _, ask = await get_current_price(ctx, symbol)
        return ask * 1.002 if ask else None

    highs = sorted([c["high"] for c in candles])
    idx = min(len(highs) - 2, int(len(highs) * 0.9))
    resistance = highs[idx]
    
    # 3. Update cache
    async with _global_sr_lock:
        if cache_key not in _global_sr_cache:
            _global_sr_cache[cache_key] = {"timestamp": datetime.now()}
        _global_sr_cache[cache_key]["resistance"] = resistance
        _global_sr_cache[cache_key]["timestamp"] = datetime.now()

    print(f"[MT5-RESISTANCE] {symbol}: Max={max(highs):.5f}, Resistance(90%)={resistance:.5f}")
    return resistance


# =====================================================
# ORDER EXECUTION
# =====================================================

async def open_position(ctx, symbol, direction, lot, sl, tp, comment=""):
    """
    Open a market position via MetaAPI.
    Lazily upgrades to websocket (RPC) connection if not already connected.
    """
    try:
        # ENSURE WEBSOCKET — check our own ws_initialized tracking, not account status
        from mt5.metaapi_manager import _ws_initialized
        if ctx.telegram_id not in _ws_initialized:
            print(f"[MT5-CORE] 🔌 WebSocket not initialized, upgrading for {direction} {symbol}...")
            from mt5.metaapi_manager import get_user_connection
            new_conn = await get_user_connection(ctx.telegram_id, ctx.metaapi_account_id, must_connect=True)
            if new_conn:
                ctx.connection = new_conn

        options = {
            'comment': comment,
            'magic': MAGIC_NUMBER,
        }

        if direction.upper() == "BUY":
            result = await ctx.connection.create_market_buy_order(
                symbol, lot, sl, tp, options
            )
        elif direction.upper() == "SELL":
            result = await ctx.connection.create_market_sell_order(
                symbol, lot, sl, tp, options
            )
        else:
            print(f"[MT5] ❌ Invalid direction: {direction}")
            return None

        if result and result.get('stringCode') == 'TRADE_RETCODE_DONE':
            order_id = result.get('orderId', result.get('positionId', ''))
            open_price = result.get('price', 0)
            print(f"[MT5] ✅ {direction} {lot} {symbol} @ {open_price} | SL: {sl:.5f} TP: {tp:.5f} | Order: {order_id}")
            return result
        else:
            string_code = result.get('stringCode', '') if result else ''
            message = result.get('message', '') if result else ''
            print(f"[MT5] ❌ Order rejected: {string_code} | {message}")
            return None

    except Exception as e:
        print(f"[MT5] ❌ Error opening position: {e}")
        raise


async def open_position_with_retry(ctx, symbol, direction, lot, sl, tp, comment="", max_retries=3, backoff_seconds=2):
    """
    Open a market position via MetaAPI with retry logic.
    Uses exponential backoff for transient failures.

    Returns:
        (result_dict, success: bool, error_msg: str)
    """
    attempt = 0
    last_error = None
    
    # Non-retryable error fragments — abort immediately on these
    NON_RETRYABLE = [
        'TRADE_RETCODE_INVALID_PRICE',
        'TRADE_RETCODE_INVALID_VOLUME',
        'TRADE_RETCODE_MARKET_CLOSED',
        'TRADE_RETCODE_NO_MONEY',
        'Market is closed',
        'market is closed',
        'trading disabled',
        'Trade is disabled',
        'invalid volume',
        'invalid price',
        'Invalid stops',
        'invalid stops',
        'not enough money',
        'Not enough money',
    ]

    while attempt < max_retries:
        try:
            print(f"[MT5-ORDER-RETRY] 🔄 Attempting {direction} {symbol} (Attempt {attempt + 1}/{max_retries})")
            
            result = await open_position(ctx, symbol, direction, lot, sl, tp, comment)
            
            if result and result.get('stringCode') == 'TRADE_RETCODE_DONE':
                print(f"[MT5-ORDER-RETRY] ✅ Order placed successfully on attempt {attempt + 1}")
                return result, True, None
            else:
                last_error = result.get('message', result.get('stringCode', 'Unknown error')) if result else 'No result'
                print(f"[MT5-ORDER-RETRY] ⚠️ Order failed (attempt {attempt + 1}): {last_error}")
                
                # Don't retry on permanent/non-transient errors
                if any(nr in str(last_error) for nr in NON_RETRYABLE) or (
                    result and any(nr in result.get('stringCode', '') for nr in NON_RETRYABLE)
                ):
                    print(f"[MT5-ORDER-RETRY] ❌ Non-retryable error — aborting retries: {last_error}")
                    return result, False, last_error
        
        except Exception as e:
            last_error = str(e)
            is_non_retryable = any(nr.lower() in last_error.lower() for nr in NON_RETRYABLE)
            if is_non_retryable:
                print(f"[MT5-ORDER-RETRY] ❌ Non-retryable error — aborting immediately: {last_error}")
                return None, False, last_error
            else:
                import traceback
                print(f"[MT5-ORDER-RETRY] ⚠️ Attempt {attempt + 1} exception: {last_error}")
                traceback.print_exc()
                # Refresh connection on transient websocket/timeout errors
                if any(k in last_error.lower() for k in ['timeout', 'disconnect', 'not connected', 'websocket']):
                    try:
                        from mt5.metaapi_manager import get_user_connection
                        fresh = await get_user_connection(ctx.telegram_id, ctx.metaapi_account_id)
                        if fresh is not None:
                            ctx.connection = fresh
                            print(f"[MT5-ORDER-RETRY] 🔄 Refreshed connection before retry")
                    except Exception:
                        pass
        
        attempt += 1
        
        # Exponential backoff
        if attempt < max_retries:
            wait_time = backoff_seconds * (2 ** (attempt - 1))
            print(f"[MT5-ORDER-RETRY] ⏳ Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
    
    print(f"[MT5-ORDER-RETRY] ❌ Failed after {max_retries} attempts: {last_error}")
    return None, False, last_error


async def close_position(ctx, position_id, max_retries=3, backoff_seconds=2):
    """
    Close an open position by position ID.
    Lazily upgrades to websocket (RPC) if needed.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            # ENSURE WEBSOCKET
            if not await is_mt5_connected(ctx.telegram_id):
                print(f"[MT5-CORE] 🔌 Upgrading to websocket for Close {position_id}...")
                from mt5.metaapi_manager import get_user_connection
                new_conn = await get_user_connection(ctx.telegram_id, ctx.metaapi_account_id, must_connect=True)
                if new_conn:
                    ctx.connection = new_conn

            result = await ctx.connection.close_position(str(position_id))

            if result and result.get('stringCode') == 'TRADE_RETCODE_DONE':
                print(f"[MT5] ✅ Closed position {position_id}")
                return result
            else:
                error_msg = (result.get('message', 'Unknown error') if result else 'No result')
                print(f"[MT5] ❌ Close failed for {position_id}: {error_msg}")
                return None  # broker-level rejection — don't retry

        except Exception as e:
            last_error = str(e)
            print(f"[MT5] ⚠️ Error closing position {position_id} (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(backoff_seconds * attempt)

    print(f"[MT5] ❌ close_position failed after {max_retries} attempts: {last_error}")
    return None


async def modify_position_sl(ctx, position_id, new_sl, new_tp=None, max_retries=3, backoff_seconds=2):
    """
    Modify the stop loss (and optionally take profit) of an open position,
    with retry on transient connection errors.

    Args:
        ctx: MT5UserContext
        position_id: Position ID (string)
        new_sl: New stop loss price
        new_tp: New take profit price (optional)

    Returns:
        dict (MetaAPI trade result) or None on failure
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            modify_options = {'stopLoss': new_sl}
            if new_tp is not None:
                modify_options['takeProfit'] = new_tp

            result = await ctx.connection.modify_position(str(position_id), modify_options)

            if result and result.get('stringCode') == 'TRADE_RETCODE_DONE':
                print(f"[MT5] Modified position {position_id} | New SL: {new_sl:.5f}")
                return result
            else:
                error_msg = (result.get('message', 'Unknown error') if result else 'No result')
                print(f"[MT5] Modify failed for {position_id}: {error_msg}")
                return None  # broker-level rejection — don't retry

        except Exception as e:
            last_error = str(e)
            print(f"[MT5] Error modifying position {position_id} (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                if any(k in last_error.lower() for k in ['timeout', 'disconnect', 'not connected', 'websocket']):
                    try:
                        from mt5.metaapi_manager import get_user_connection
                        fresh = await get_user_connection(ctx.telegram_id, ctx.metaapi_account_id)
                        if fresh is not None:
                            ctx.connection = fresh
                            print(f"[MT5] 🔄 Refreshed connection before modify retry")
                    except Exception:
                        pass
                await asyncio.sleep(backoff_seconds * attempt)

    print(f"[MT5] ❌ modify_position_sl failed after {max_retries} attempts: {last_error}")
    return None


# =====================================================
# POSITION QUERIES
# =====================================================

async def get_open_positions(ctx, magic_only=True):
    """
    Get all open positions via REST API (Scalable to 1000+ users).
    Avoids opening a websocket connection for routine monitoring.
    """
    try:
        from mt5.metaapi_manager import get_user_positions_rest
        positions = await get_user_positions_rest(ctx.telegram_id, ctx.metaapi_account_id)
        if positions is None:
            return None  # Failure - distinguish from empty list []
        if not positions:
            return []

        result = []
        for pos in positions:
            pos_magic = pos.get('magic', 0)
            if magic_only and pos_magic != MAGIC_NUMBER:
                continue

            # Normalize position type
            raw_type = str(pos.get('type', ''))
            if 'BUY' in raw_type.upper():
                pos_type = "BUY"
            elif 'SELL' in raw_type.upper():
                pos_type = "SELL"
            else:
                pos_type = raw_type

            time_val = pos.get('time', datetime.utcnow())
            if isinstance(time_val, str):
                try:
                    time_val = datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                except Exception:
                    time_val = datetime.utcnow()

            result.append({
                "ticket": pos.get('id'),
                "symbol": pos.get('symbol', ''),
                "type": pos_type,
                "volume": pos.get('volume', 0),
                "price_open": pos.get('openPrice', 0),
                "price_current": pos.get('currentPrice', 0),
                "sl": pos.get('stopLoss', 0),
                "tp": pos.get('takeProfit', 0),
                "profit": (pos.get('profit', 0)
                           + pos.get('swap', 0)
                           + pos.get('commission', 0)),
                "magic": pos_magic,
                "comment": pos.get('comment', ''),
                "time": time_val,
            })
        return result

    except Exception as e:
        print(f"[MT5] Error getting positions: {e}")
        return []


async def get_active_positions_count(ctx, magic_only=True):
    """Get count of currently open positions. Returns 0 if fetch fails."""
    positions = await get_open_positions(ctx, magic_only)
    if positions is None:
        return 0 # Handle API failure safely
    return len(positions)


# =====================================================
# ORDER & DEAL HISTORY (For P&L Tracking)
# =====================================================

# MQL5 ENUM_DEAL_ENTRY: IN=0, OUT=1, INOUT=2, OUT_BY=3
_MT5_DEAL_ENTRY_OUT = frozenset((1, 2, 3))


def _deal_read(deal, key, default=None):
    """Read deal fields from MetaAPI dicts or SDK objects (camelCase / snake_case)."""
    if isinstance(deal, dict):
        if key in deal:
            return deal[key]
        alt = {"positionId": "position_id", "entryType": "entry_type"}.get(key)
        if alt and alt in deal:
            return deal[alt]
        return deal.get(key, default)
    v = getattr(deal, key, None)
    if v is not None:
        return v
    alt = {"positionId": "position_id", "entryType": "entry_type"}.get(key)
    if alt:
        return getattr(deal, alt, default)
    return default


def _is_closing_deal_entry(entry_type) -> bool:
    """True if this deal closes (or opens+closes) a position — not a bare IN entry."""
    if entry_type is None:
        return False
    if isinstance(entry_type, (int, float)):
        return int(entry_type) in _MT5_DEAL_ENTRY_OUT
    if isinstance(entry_type, str):
        st = entry_type.strip()
        if st.isdigit():
            return int(st) in _MT5_DEAL_ENTRY_OUT
    s = str(entry_type).upper()
    return "OUT" in s


async def get_history_deals(ctx, lookback_hours=24):
    """
    Get history deals for the last N hours via time-range fallback.
    Tries multiple MetaAPI method names for compatibility.
    """
    try:
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=lookback_hours)

        for method_name in ('get_deals_by_time_range', 'get_deals'):
            method = getattr(ctx.connection, method_name, None)
            if method is None:
                continue
            try:
                deals = await method(start_time, end_time)
            except Exception as e:
                print(f"[MT5] {method_name} failed: {e}")
                continue

            if deals is None:
                continue

            # Convert to a plain list so subscript access always works,
            # regardless of whether MetaAPI returns a list, generator, or
            # custom iterable.
            try:
                deals = list(deals)
            except Exception:
                pass

            print(f"[MT5] Fetched {len(deals)} deals via {method_name}")
            return deals

        return []
    except Exception as e:
        print(f"[MT5] Error fetching history deals: {e}")
        return []

def _is_real_deal(deal) -> bool:
    """True if this is a proper deal dict/object (not a bare string/ID)."""
    if isinstance(deal, str):
        return False
    if isinstance(deal, dict):
        return "profit" in deal or "entryType" in deal or "symbol" in deal
    return hasattr(deal, "profit") or hasattr(deal, "entryType")


async def get_position_realized_pnl(ctx, ticket):
    """
    Calculate realized P&L for a specific position ticket from history.
    Returns (profit_amount, percent) or (None, None) if not found.

    Strategy:
      1. Try get_deals_by_position(ticket) — direct MetaAPI lookup.
         Some SDK versions return bare ID strings instead of deal objects;
         if so, discard and fall through.
      2. Fall back to time-range search filtered by positionId.
      3. Accept any deal whose entryType indicates a close (OUT / OUT_BY / INOUT).
    """
    target_ticket = str(ticket)

    # ── Step 1: direct position lookup ──────────────────────────────────────
    deals = []
    for method_name in ('get_deals_by_position', 'get_history_deals_by_position'):
        method = getattr(ctx.connection, method_name, None)
        if method is None:
            continue
        try:
            raw = await method(target_ticket)
            if raw:
                if all(_is_real_deal(d) for d in raw):
                    deals = raw
                    print(f"[MT5] Found {len(deals)} deals for position {ticket} via {method_name}")
                    break
                else:
                    print(f"[MT5-DEBUG] {method_name} returned non-deal items "
                          f"(e.g. {type(raw[0]).__name__}: {str(raw[0])[:80]}), skipping to time-range")
        except Exception as e:
            print(f"[MT5] {method_name}({ticket}) failed: {e}")

    # ── Step 2: time-range fallback ──────────────────────────────────────────
    if not deals:
        all_deals = await get_history_deals(ctx, lookback_hours=48)
        deals = [
            d for d in all_deals
            if _is_real_deal(d) and str(_deal_read(d, "positionId") or "") == target_ticket
        ]
        if deals:
            print(f"[MT5] Found {len(deals)} deals for position {ticket} via time-range search")

    if not deals:
        print(f"[MT5] No history deals found for position {ticket}")
        return None, None

    # ── Step 3: sum P&L and detect closing deal ──────────────────────────────
    total_profit = 0.0
    found_closing_deal = False

    for deal in deals:
        profit = float(_deal_read(deal, "profit") or 0)
        swap = float(_deal_read(deal, "swap") or 0)
        commission = float(_deal_read(deal, "commission") or 0)
        total_profit += profit + swap + commission

        et = _deal_read(deal, "entryType")
        if _is_closing_deal_entry(et):
            found_closing_deal = True

    if not found_closing_deal and len(deals) >= 2:
        found_closing_deal = True

    if found_closing_deal:
        print(f"[MT5] Realized P&L for position {ticket}: ${total_profit:.2f}")
        return total_profit, 0.0

    return None, None

