from binance.client import Client
from binance.enums import *
import statistics
import time
import asyncio
from datetime import datetime, timedelta

from utils.logging_service import logger
from utils.precision import adjust_quantity
# from model.ai_model import load_model, preprocess_data, create_model, save_model
from binance.exceptions import BinanceAPIException
import numpy as np

from trading.config import (
    TRADE_SYMBOL,
    TRADE_QUANTITY,
    LOOKBACK,
    TAKE_PROFIT_MULTIPLIER,
    STOP_LOSS_MULTIPLIER,
    MIN_GAP,
)

# Replace these with actual preprocessing parameters
MIN_PRICE = 30000  # Example min price used during normalization
MAX_PRICE = 65000  # Example max price used during normalization

bot_status = "Not running"
error_log = "No Errors"

# Track last time threshold entries were triggered for each coin (prevents repeated triggers within monitoring window)
last_threshold_entry_time = {}  # Format: {coin: datetime}


# Helper function to get real active trades count from Binance
def get_real_active_futures_count(client):
    """Return the real number of active futures trades from Binance."""
    try:
        positions = client.futures_position_information()
        return sum(1 for p in positions if float(p["positionAmt"]) != 0)
    except Exception as e:
        print(f"[ERROR] Could not fetch real active trades from Binance: {e}")
        return 0


# Function to calculate support level
def find_support_level(api_key=None, api_secret=None, coin=None, client=None):
    """
    Find support level using historical lows from 4-hour candles.
    Uses the lowest low from recent candles as support.
    CRITICAL: Ensures support < resistance (never returns invalid levels)
    """
    # Use provided client or create new one
    if client is None:
        from trading.client_factory import get_binance_client
        client = get_binance_client(api_key, api_secret)

    try:
        # Fetch 4-hour candles for better support/resistance levels
        klines = client.futures_klines(
            symbol=coin, interval="4h", limit=24  # Last 4 days of 4h candles
        )
        
        # Extract low prices
        recent_lows = [float(kline[3]) for kline in klines]  # kline[3] is 'low'
        
        # Use 10th percentile of lows as support (avoids extreme wicks)
        recent_lows_sorted = sorted(recent_lows)
        percentile_index = max(1, int(len(recent_lows_sorted) * 0.1))
        support_level = recent_lows_sorted[percentile_index]
        
        if support_level <= 0:
            raise ValueError(f"Invalid support level: {support_level}")
        
        print(f"[SUPPORT] {coin}: Min={min(recent_lows):.6f}, Support(10%)={support_level:.6f}")
        
        return support_level
        
    except Exception as e:
        print(f"[SUPPORT] Error for {coin}: {e}")
        # Fallback: get current price and subtract 2%
        try:
            ticker = client.futures_symbol_ticker(symbol=coin)
            current_price = float(ticker['price'])
            support = current_price * 0.98
            if support <= 0:
                print(f"[SUPPORT] WARNING: Invalid fallback support for {coin}: {support}")
                return None
            return support
        except Exception as fallback_err:
            print(f"[SUPPORT] Fallback failed for {coin}: {fallback_err}")
            return None


# REMOVED DUPLICATE find_resistance_level function
# The version at line ~137 is the one actually used


def find_resistance_level(api_key=None, api_secret=None, coin=None, min_volume=10, client=None):
    """
    Find resistance level using historical highs from 4-hour candles.
    Uses the highest high from recent candles as resistance.
    CRITICAL: Ensures resistance > support (never returns invalid levels)
    """
    # Use provided client or create new one
    if client is None:
        from trading.client_factory import get_binance_client
        client = get_binance_client(api_key, api_secret)

    try:
        # Fetch 4-hour candles for better support/resistance levels
        klines = client.futures_klines(
            symbol=coin, interval="4h", limit=24  # Last 4 days of 4h candles
        )
        
        # Extract high prices
        recent_highs = [float(kline[2]) for kline in klines]  # kline[2] is 'high'
        
        # Use 90th percentile of highs as resistance (avoids extreme wicks)
        recent_highs_sorted = sorted(recent_highs)
        percentile_index = min(len(recent_highs_sorted) - 2, int(len(recent_highs_sorted) * 0.9))
        resistance_level = recent_highs_sorted[percentile_index]
        
        if resistance_level <= 0:
            raise ValueError(f"Invalid resistance level: {resistance_level}")
        
        print(f"[RESISTANCE] {coin}: Max={max(recent_highs):.6f}, Resistance(90%)={resistance_level:.6f}")
        
        return resistance_level
        
    except Exception as e:
        print(f"[RESISTANCE] Error for {coin}: {e}")
        # Fallback: get current price and add 2%
        try:
            ticker = client.futures_symbol_ticker(symbol=coin)
            current_price = float(ticker['price'])
            resistance = current_price * 1.02
            if resistance <= 0:
                print(f"[RESISTANCE] WARNING: Invalid fallback resistance for {coin}: {resistance}")
                return None
            return resistance
        except Exception as fallback_err:
            print(f"[RESISTANCE] Fallback failed for {coin}: {fallback_err}")
            return None


def get_wallet_balance_future(client):
    """Fetch USDT balance from Binance Futures account"""
    try:
        acc_balance = client.futures_account_balance()
        
        usdt_balance = None
        for check_balance in acc_balance:
            if check_balance["asset"] == "USDT":
                usdt_balance = check_balance["balance"]
                break
        
        if usdt_balance is None:
            print(f"[ERROR] USDT asset not found in account balance!")
            raise ValueError("USDT not found in account balance")
            
        return float(usdt_balance)
    except Exception as e:
        print(f"[ERROR] Error in get_wallet_balance_future: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise


def get_recent_prices_future(
    symbol, is_futures=True, interval="1m", lookback=100, api_key="", api_secret="", client=None
):
    """
    Fetch recent prices for a symbol as a NumPy array.

    Args:
        symbol (str): Trading pair (e.g., 'BTCUSDT').
        is_futures (bool): Whether to fetch futures prices. Default is True.
        interval (str): Candlestick interval (e.g., '1m', '5m'). Default is '1m'.
        lookback (int): Number of recent prices to fetch. Default is 50.
        client: Binance client instance (optional, will create if not provided)

    Returns:
        np.ndarray: Array of recent closing prices.
    """
    # Use provided client or create new one
    if client is None:
        from trading.client_factory import get_binance_client
        client = get_binance_client(api_key, api_secret)
    try:
        # Use the futures or spot Kline endpoint
        if is_futures:
            klines = client.futures_klines(
                symbol=symbol, interval=interval, limit=lookback
            )
        else:
            klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)

        # Extract closing prices from the Kline data
        closing_prices = [float(kline[4]) for kline in klines]

        # Return as a NumPy array
        return np.array(closing_prices).reshape(-1, 1)

    except Exception as e:
        print(f"Error fetching recent prices for {symbol}: {e}")
        return None


def place_order_future(symbol, quantity, side, leverage, client, margin_type="ISOLATED"):
    try:
        # Set leverage and margin type for the symbol
        set_leverage_and_margin(client, symbol, leverage, margin_type)

        # precise the quantity
        quantity = adjust_quantity(symbol, quantity, client)
        print(f"Adjusted quantity for {symbol}: {quantity}")

        # Place the market order
        order = client.futures_create_order(
            symbol=symbol,
            side=side,  # 'BUY' for long, 'SELL' for short
            type="MARKET",
            quantity=quantity,
        )

        print(f"Market order placed: {order}")
        return order

    except Exception as e:
        print(f"Error placing market order for {symbol}: {e}")
        return None


def long_trade_future(symbol, quantity, leverage, client, margin_type="ISOLATED"):
    # Ensure leverage and margin type are set for the trade
    set_leverage_and_margin(client, symbol, leverage, margin_type)

    try:
        # Place the market BUY order to open a long position
        order = place_order_future(symbol, quantity, "BUY", leverage, client, margin_type)
        if order:
            print(f"Opened long position for {symbol}, Quantity: {quantity}")
            return order
        else:
            print(f"Failed to open long position for {symbol}")
            return None

    except Exception as e:
        print(f"Error opening long position for {symbol}: {e}")
        return None


def short_trade_future(symbol, quantity, leverage, client, margin_type="ISOLATED"):
    """Open a SHORT position (sell) with specified leverage and margin type"""
    # Ensure leverage and margin type are set for the trade
    set_leverage_and_margin(client, symbol, leverage, margin_type)

    try:
        # Place the market SELL order to open a short position
        order = place_order_future(symbol, quantity, "SELL", leverage, client, margin_type)
        if order:
            print(f"Opened short position for {symbol}, Quantity: {quantity}")
            return order
        else:
            print(f"Failed to open short position for {symbol}")
            return None

    except Exception as e:
        print(f"Error opening short position for {symbol}: {e}")
        return None


def close_position_future(symbol, quantity, client, position_type="LONG"):
    """Close a futures position by placing opposite order
    
    Args:
        symbol: Trading symbol (e.g., 'BTCUSDT')
        quantity: Amount to close
        client: Binance client object
        position_type: "LONG" or "SHORT" - determines which order to place
    """
    try:
        # Cancel any open SL/TP orders first so they don't conflict with the close
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
            print(f"[CLOSE] Cancelled open orders for {symbol}")
        except Exception as e:
            print(f"[CLOSE] Could not cancel open orders for {symbol}: {e}")

        # Always resolve the live position first. This avoids stale cached
        # quantities and lets us send a reduce-only close for the exact size
        # Binance currently shows as open.
        side = "SELL" if position_type == "LONG" else "BUY"
        attempts = 3
        last_error = None

        for attempt in range(1, attempts + 1):
            positions = client.futures_position_information(symbol=symbol)
            open_positions = [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]

            if not open_positions:
                print(f"[CLOSE] No open Binance position found for {symbol}")
                return {"status": "FILLED", "symbol": symbol, "executedQty": "0"}

            matching_position = None
            for pos in open_positions:
                amt = float(pos.get("positionAmt", 0))
                if position_type == "LONG" and amt > 0:
                    matching_position = pos
                    break
                if position_type == "SHORT" and amt < 0:
                    matching_position = pos
                    break

            if matching_position is None:
                matching_position = open_positions[0]

            live_qty = abs(float(matching_position.get("positionAmt", 0)))
            close_qty = adjust_quantity(symbol, live_qty or quantity, client)

            if close_qty <= 0:
                print(f"[CLOSE] Invalid close quantity for {symbol}: {close_qty}")
                return None

            order_params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": close_qty,
                "reduceOnly": True,
            }

            position_side = matching_position.get("positionSide")
            if position_side and position_side != "BOTH":
                order_params["positionSide"] = position_side

            try:
                order = client.futures_create_order(**order_params)
            except BinanceAPIException as e:
                last_error = f"{e.status_code}: {e.message}"
                print(f"[CLOSE] Attempt {attempt}/{attempts} failed for {symbol}: {last_error}")
                if attempt < attempts:
                    time.sleep(1.5)
                continue

            remaining_positions = client.futures_position_information(symbol=symbol)
            still_open = False
            for pos in remaining_positions:
                amt = float(pos.get("positionAmt", 0))
                if position_side and position_side != "BOTH":
                    if pos.get("positionSide") != position_side:
                        continue
                if position_type == "LONG" and amt > 0:
                    still_open = True
                    break
                if position_type == "SHORT" and amt < 0:
                    still_open = True
                    break

            if not still_open:
                print(f"Closed {position_type} position for {symbol}, Quantity: {close_qty}")
                return order

            last_error = "Position still open after close order"
            print(f"[CLOSE] Attempt {attempt}/{attempts} submitted for {symbol}, but position remains open")
            if attempt < attempts:
                time.sleep(1.5)

        print(f"Error closing {position_type} position for {symbol}: {last_error}")
        return None
    except Exception as e:
        print(f"Error closing {position_type} position for {symbol}: {e}")
        return None


def set_leverage_and_margin(client, symbol, leverage, margin_type):
    """
    Set leverage and margin type for a symbol in Binance Futures.
    Verifies that settings were actually applied before returning.

    Returns:
        tuple: (success: bool, error_msg: str or None)
    """
    margin_ok = False
    leverage_ok = False
    errors = []

    # 1. Set margin type
    try:
        client.futures_change_margin_type(
            symbol=symbol,
            marginType=margin_type.upper()
        )
        print(f"[MARGIN] ✅ {symbol} margin set to {margin_type}")
        margin_ok = True
    except Exception as e:
        err_str = str(e)
        if "No need to change margin type" in err_str:
            margin_ok = True
        else:
            print(f"[MARGIN] ❌ {symbol}: {e}")
            errors.append(f"Margin type: {err_str[:120]}")

    # 2. Set leverage
    try:
        result = client.futures_change_leverage(symbol=symbol, leverage=leverage)
        actual = int(result.get('leverage', 0)) if isinstance(result, dict) else 0
        if actual == leverage:
            print(f"[LEVERAGE] ✅ {symbol} leverage set to {leverage}x")
            leverage_ok = True
        else:
            print(f"[LEVERAGE] ⚠️ {symbol} requested {leverage}x but got {actual}x")
            errors.append(f"Leverage mismatch: requested {leverage}x, got {actual}x")
    except Exception as e:
        print(f"[LEVERAGE] ❌ {symbol}: {e}")
        errors.append(f"Leverage: {str(e)[:120]}")

    # 3. Verify by querying actual position settings
    if margin_ok and leverage_ok:
        try:
            positions = client.futures_position_information(symbol=symbol)
            for pos in positions:
                actual_margin = pos.get('marginType', '').upper()
                actual_lev = int(pos.get('leverage', 0))
                if actual_margin != margin_type.upper():
                    margin_ok = False
                    errors.append(f"Verification failed: margin is {actual_margin}, expected {margin_type.upper()}")
                    print(f"[VERIFY] ❌ {symbol} margin is {actual_margin}, expected {margin_type.upper()}")
                if actual_lev != leverage:
                    leverage_ok = False
                    errors.append(f"Verification failed: leverage is {actual_lev}x, expected {leverage}x")
                    print(f"[VERIFY] ❌ {symbol} leverage is {actual_lev}x, expected {leverage}x")
                break
        except Exception as e:
            print(f"[VERIFY] ⚠️ Could not verify settings for {symbol}: {e}")

    if not margin_ok or not leverage_ok:
        return False, "; ".join(errors)

    return True, None


# =====================================================
# IMPROVED TRADING INDICATORS
# =====================================================

def _compute_rsi_series(close_prices, period=14):
    """
    Compute a full RSI series from a list of close prices.
    Returns a list of RSI values (same length as close_prices, first `period` values are None).
    """
    if len(close_prices) < period + 1:
        return [50.0] * len(close_prices)
    
    rsi_values = [None] * period
    
    # Initial average gain/loss (SMA)
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = close_prices[i] - close_prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - (100.0 / (1.0 + rs)))
    
    # Smoothed (Wilder's) for the rest
    for i in range(period + 1, len(close_prices)):
        change = close_prices[i] - close_prices[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))
    
    return rsi_values


def calculate_stochastic_rsi(
    api_key=None, api_secret=None, coin=None,
    rsi_period=14, stoch_period=14, k_smooth=3, d_smooth=3,
    interval="1h", client=None
):
    """
    Calculate the Stochastic RSI indicator.
    
    StochRSI = (RSI - min(RSI, n)) / (max(RSI, n) - min(RSI, n))
    %K = SMA(StochRSI, k_smooth)  (scaled 0-100)
    %D = SMA(%K, d_smooth)
    
    Signals:
        %K < 20            -> Oversold  (potential LONG)
        %K > 80            -> Overbought (potential SHORT)
        %K crosses above %D -> Bullish crossover
        %K crosses below %D -> Bearish crossover
    
    Returns:
        dict: {
            'k': float (0-100),
            'd': float (0-100),
            'rsi': float (0-100),
            'crossover': 'BULLISH' | 'BEARISH' | 'NONE'
        }
    """
    if client is None:
        from trading.client_factory import get_binance_client
        client = get_binance_client(api_key, api_secret)
    
    try:
        # Wilder's RSI needs many bars to converge (EMA-style warm-up).
        # With too few bars the initial seed dominates and values diverge from charts.
        # 500 bars (~21 days of 1h data) gives full convergence.
        candles_needed = 500
        klines = client.futures_klines(symbol=coin, interval=interval, limit=candles_needed)
        close_prices = [float(kline[4]) for kline in klines]
        
        if len(close_prices) < rsi_period + stoch_period:
            return {'k': 50.0, 'd': 50.0, 'rsi': 50.0, 'crossover': 'NONE'}
        
        # Step 1: Compute full RSI series
        rsi_series = _compute_rsi_series(close_prices, rsi_period)
        
        # Filter out None values
        valid_rsi = [r for r in rsi_series if r is not None]
        
        if len(valid_rsi) < stoch_period:
            return {'k': 50.0, 'd': 50.0, 'rsi': valid_rsi[-1] if valid_rsi else 50.0, 'crossover': 'NONE'}
        
        # Step 2: Apply Stochastic formula to RSI values
        stoch_rsi_raw = []
        for i in range(stoch_period - 1, len(valid_rsi)):
            window = valid_rsi[i - stoch_period + 1 : i + 1]
            rsi_min = min(window)
            rsi_max = max(window)
            if rsi_max - rsi_min == 0:
                stoch_rsi_raw.append(50.0)  # No variation
            else:
                stoch_rsi_raw.append(((valid_rsi[i] - rsi_min) / (rsi_max - rsi_min)) * 100.0)
        
        if len(stoch_rsi_raw) < k_smooth:
            return {'k': 50.0, 'd': 50.0, 'rsi': valid_rsi[-1], 'crossover': 'NONE'}
        
        # Step 3: Smooth StochRSI with SMA to get %K
        k_values = []
        for i in range(k_smooth - 1, len(stoch_rsi_raw)):
            k_val = sum(stoch_rsi_raw[i - k_smooth + 1 : i + 1]) / k_smooth
            k_values.append(k_val)
        
        if len(k_values) < d_smooth:
            return {'k': k_values[-1] if k_values else 50.0, 'd': 50.0, 'rsi': valid_rsi[-1], 'crossover': 'NONE'}
        
        # Step 4: Smooth %K with SMA to get %D
        d_values = []
        for i in range(d_smooth - 1, len(k_values)):
            d_val = sum(k_values[i - d_smooth + 1 : i + 1]) / d_smooth
            d_values.append(d_val)
        
        current_k = k_values[-1]
        current_d = d_values[-1]
        
        # Step 5: Detect crossover (compare last 2 K/D values)
        crossover = 'NONE'
        if len(k_values) >= 2 and len(d_values) >= 2:
            prev_k = k_values[-2]
            prev_d = d_values[-2]
            
            if prev_k <= prev_d and current_k > current_d:
                crossover = 'BULLISH'  # %K crossed above %D
            elif prev_k >= prev_d and current_k < current_d:
                crossover = 'BEARISH'  # %K crossed below %D
        
        return {
            'k': round(current_k, 2),
            'd': round(current_d, 2),
            'rsi': round(valid_rsi[-1], 2),
            'crossover': crossover
        }
    
    except Exception as e:
        print(f"[STOCH-RSI] Error calculating Stochastic RSI for {coin}: {e}")
        return {'k': 50.0, 'd': 50.0, 'rsi': 50.0, 'crossover': 'NONE'}


def get_trend_with_ma(api_key=None, api_secret=None, coin=None, short_period=20, long_period=50):
    """
    Determine trend direction using EMA crossover.
    
    Returns:
        str: "UPTREND", "DOWNTREND", or "SIDEWAYS"
        float: short_ma value
        float: long_ma value
    """
    from trading.client_factory import get_binance_client
    client = get_binance_client(api_key, api_secret)
    
    try:
        # Fetch candles for MA calculation
        klines = client.futures_klines(symbol=coin, interval="1h", limit=long_period + 10)
        close_prices = [float(kline[4]) for kline in klines]
        
        if len(close_prices) < long_period:
            return "SIDEWAYS", 0, 0
        
        # Calculate EMAs
        short_ma = sum(close_prices[-short_period:]) / short_period
        long_ma = sum(close_prices[-long_period:]) / long_period
        
        # Calculate trend strength
        ma_diff_percent = ((short_ma - long_ma) / long_ma) * 100
        
        if ma_diff_percent > 0.5:  # Short MA significantly above Long MA
            return "UPTREND", short_ma, long_ma
        elif ma_diff_percent < -0.5:  # Short MA significantly below Long MA
            return "DOWNTREND", short_ma, long_ma
        else:
            return "SIDEWAYS", short_ma, long_ma
    
    except Exception as e:
        print(f"[TREND] Error calculating trend for {coin}: {e}")
        return "SIDEWAYS", 0, 0


def detect_momentum_shift(api_key=None, api_secret=None, coin=None):
    """
    Detect if there's a momentum shift (reversal) happening.
    Uses last 3 candles to detect bullish/bearish reversals.
    
    Returns:
        str: "BULLISH_REVERSAL", "BEARISH_REVERSAL", or "NO_SIGNAL"
    """
    from trading.client_factory import get_binance_client
    client = get_binance_client(api_key, api_secret)
    
    try:
        # Fetch recent candles
        klines = client.futures_klines(symbol=coin, interval="1h", limit=5)
        
        if len(klines) < 3:
            return "NO_SIGNAL"
        
        # Extract OHLC data for last 3 candles
        candles = []
        for kline in klines[-3:]:
            candles.append({
                'open': float(kline[1]),
                'high': float(kline[2]),
                'low': float(kline[3]),
                'close': float(kline[4]),
                'volume': float(kline[5])
            })
        
        # Check for bullish reversal patterns
        # 1. Last candle is green (close > open)
        # 2. Last candle closed higher than previous candle's close
        # 3. Previous candles were red (bearish)
        
        last_candle = candles[-1]
        prev_candle = candles[-2]
        prev_prev_candle = candles[-3]
        
        last_is_green = last_candle['close'] > last_candle['open']
        prev_is_red = prev_candle['close'] < prev_candle['open']
        
        # Bullish reversal: previous red, current green closing above previous close
        if last_is_green and prev_is_red and last_candle['close'] > prev_candle['close']:
            # Check for significant body size (not just a doji)
            body_size = abs(last_candle['close'] - last_candle['open'])
            candle_range = last_candle['high'] - last_candle['low']
            if candle_range > 0 and body_size / candle_range > 0.4:
                return "BULLISH_REVERSAL"
        
        # Bearish reversal: previous green, current red closing below previous close
        last_is_red = last_candle['close'] < last_candle['open']
        prev_is_green = prev_candle['close'] > prev_candle['open']
        
        if last_is_red and prev_is_green and last_candle['close'] < prev_candle['close']:
            body_size = abs(last_candle['close'] - last_candle['open'])
            candle_range = last_candle['high'] - last_candle['low']
            if candle_range > 0 and body_size / candle_range > 0.4:
                return "BEARISH_REVERSAL"
        
        return "NO_SIGNAL"
    
    except Exception as e:
        print(f"[MOMENTUM] Error detecting momentum for {coin}: {e}")
        return "NO_SIGNAL"


def get_volume_confirmation(api_key=None, api_secret=None, coin=None):
    """
    Check if current volume is above average (confirms the move).
    
    Returns:
        bool: True if volume is above average, False otherwise
        float: volume ratio (current/average)
    """
    from trading.client_factory import get_binance_client
    client = get_binance_client(api_key, api_secret)
    
    try:
        # Fetch recent candles
        klines = client.futures_klines(symbol=coin, interval="1h", limit=20)
        volumes = [float(kline[5]) for kline in klines]
        
        if len(volumes) < 10:
            return False, 1.0
        
        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)  # Average of all except last
        current_volume = volumes[-1]
        
        if avg_volume == 0:
            return False, 1.0
        
        volume_ratio = current_volume / avg_volume
        
        # Volume is significant if it's at least 80% of average
        return volume_ratio >= 0.8, volume_ratio
    
    except Exception as e:
        print(f"[VOLUME] Error checking volume for {coin}: {e}")
        return False, 1.0


def get_trade_signal_advanced(api_key=None, api_secret=None, coin=None, current_price=None, support=None, resistance=None):
    """
    Advanced trade signal using Stochastic RSI as the primary indicator,
    combined with trend, momentum, and volume confirmation.
    
    Returns:
        str: "LONG", "SHORT", or "NO_TRADE"
        dict: Signal details with reasons
    """
    signal_details = {
        'signal': 'NO_TRADE',
        'stoch_rsi_k': None,
        'stoch_rsi_d': None,
        'rsi': None,
        'crossover': None,
        'trend': None,
        'momentum': None,
        'volume_confirmed': False,
        'reasons': []
    }
    
    try:
        # 1. Calculate Stochastic RSI (primary indicator)
        stoch_rsi = calculate_stochastic_rsi(api_key=api_key, api_secret=api_secret, coin=coin)
        signal_details['stoch_rsi_k'] = stoch_rsi['k']
        signal_details['stoch_rsi_d'] = stoch_rsi['d']
        signal_details['rsi'] = stoch_rsi['rsi']
        signal_details['crossover'] = stoch_rsi['crossover']
        
        k = stoch_rsi['k']
        d = stoch_rsi['d']
        rsi = stoch_rsi['rsi']
        crossover = stoch_rsi['crossover']
        
        # 2. Get trend direction
        trend, short_ma, long_ma = get_trend_with_ma(api_key=api_key, api_secret=api_secret, coin=coin)
        signal_details['trend'] = trend
        
        # 3. Check for momentum shift
        momentum = detect_momentum_shift(api_key=api_key, api_secret=api_secret, coin=coin)
        signal_details['momentum'] = momentum
        
        # 4. Get volume confirmation
        volume_ok, volume_ratio = get_volume_confirmation(api_key=api_key, api_secret=api_secret, coin=coin)
        signal_details['volume_confirmed'] = volume_ok
        signal_details['volume_ratio'] = volume_ratio
        
        # Calculate price position relative to support/resistance
        # CRITICAL: Validate support/resistance are in correct order
        if support and resistance and resistance > support:
            price_range = resistance - support
            price_position = (current_price - support) / price_range
            # Clamp to [0, 1] to prevent invalid positions
            price_position = max(0, min(1, price_position))
        else:
            # Invalid S/R levels - use neutral position
            print(f"[SIGNAL] WARNING: Invalid S/R for {coin} (Support={support}, Resistance={resistance})")
            price_position = 0.5
        
        signal_details['price_position'] = price_position
        
        # ==========================================
        # THRESHOLD TRIGGERS + SCORING (MT5-inspired)
        # ==========================================
        from config import (
            FUTURES_STOCH_RSI_BUY_LEVEL,
            FUTURES_STOCH_RSI_SHORT_LEVEL,
            FUTURES_THRESHOLD_MONITORING_WINDOW_MINUTES
        )

        strong_buy_trigger = round(float(k), 2) <= round(float(FUTURES_STOCH_RSI_BUY_LEVEL), 2)
        strong_sell_trigger = round(float(k), 2) >= round(float(FUTURES_STOCH_RSI_SHORT_LEVEL), 2)

        # ---------- LONG score ----------
        long_score = 0.0

        if strong_buy_trigger:
            long_score += 4
            signal_details['reasons'].append(f"StochRSI buy trigger hit (%K={k:.2f} <= {FUTURES_STOCH_RSI_BUY_LEVEL})")
        elif k < 20:
            long_score += 2
            signal_details['reasons'].append(f"StochRSI oversold (%K={k:.1f})")
        elif k < 35:
            long_score += 1
            signal_details['reasons'].append(f"StochRSI low (%K={k:.1f})")

        if crossover == 'BULLISH':
            long_score += 2
            signal_details['reasons'].append(f"StochRSI bullish crossover (%K={k:.1f} > %D={d:.1f})")

        if rsi < 45:
            long_score += 1
            signal_details['reasons'].append(f"RSI supports LONG ({rsi:.1f})")

        if trend == "UPTREND":
            long_score += 1.5
            signal_details['reasons'].append("Uptrend confirmed (MA crossover)")
        elif trend == "SIDEWAYS":
            long_score += 0.5

        if momentum == "BULLISH_REVERSAL":
            long_score += 1.5
            signal_details['reasons'].append("Bullish reversal pattern")

        if price_position < 0.35:
            long_score += 1
            signal_details['reasons'].append(f"Price near support ({price_position*100:.0f}%)")
        elif price_position < 0.5:
            long_score += 0.5

        if volume_ok and volume_ratio > 1.2:
            long_score += 1
            signal_details['reasons'].append(f"Strong volume ({volume_ratio:.1f}x avg)")

        # ---------- SHORT score ----------
        short_score = 0.0

        if strong_sell_trigger:
            short_score += 4
            signal_details['reasons'].append(f"StochRSI short trigger hit (%K={k:.2f} >= {FUTURES_STOCH_RSI_SHORT_LEVEL})")
        elif k > 80:
            short_score += 2
            signal_details['reasons'].append(f"StochRSI overbought (%K={k:.1f})")
        elif k > 65:
            short_score += 1
            signal_details['reasons'].append(f"StochRSI high (%K={k:.1f})")

        if crossover == 'BEARISH':
            short_score += 2
            signal_details['reasons'].append(f"StochRSI bearish crossover (%K={k:.1f} < %D={d:.1f})")

        if rsi > 55:
            short_score += 1
            signal_details['reasons'].append(f"RSI supports SHORT ({rsi:.1f})")

        if trend == "DOWNTREND":
            short_score += 1.5
            signal_details['reasons'].append("Downtrend confirmed (MA crossover)")
        elif trend == "SIDEWAYS":
            short_score += 0.5

        if momentum == "BEARISH_REVERSAL":
            short_score += 1.5
            signal_details['reasons'].append("Bearish reversal pattern")

        if price_position > 0.65:
            short_score += 1
            signal_details['reasons'].append(f"Price near resistance ({price_position*100:.0f}%)")
        elif price_position > 0.5:
            short_score += 0.5

        if volume_ok and volume_ratio > 1.2:
            short_score += 1

        signal_details['long_score'] = long_score
        signal_details['short_score'] = short_score

        # ==========================================
        # THRESHOLD-BASED ENTRY DECISION
        # ==========================================
        now = datetime.now()
        time_since_last_threshold = None

        if coin in last_threshold_entry_time:
            time_since_last_threshold = (now - last_threshold_entry_time[coin]).total_seconds() / 60

        allow_threshold_entry = (time_since_last_threshold is None or
                                 time_since_last_threshold >= FUTURES_THRESHOLD_MONITORING_WINDOW_MINUTES)

        # region agent log
        import json as _json, time as _time; open(r'D:/telegram/new_bot/debug-8958e4.log', 'a').write(_json.dumps({"sessionId": "8958e4", "timestamp": int(_time.time()*1000), "location": "future_trading.py:908", "hypothesisId": "A", "message": "Binance threshold check", "data": {"coin": coin, "k": round(float(k), 2), "d": round(float(d), 2), "buy_level": FUTURES_STOCH_RSI_BUY_LEVEL, "short_level": FUTURES_STOCH_RSI_SHORT_LEVEL, "strong_buy": bool(strong_buy_trigger), "strong_sell": bool(strong_sell_trigger), "allow_entry": bool(allow_threshold_entry), "cooldown_min": time_since_last_threshold, "long_score": round(long_score, 2), "short_score": round(short_score, 2)}}) + '\n')
        # endregion

        if strong_buy_trigger:
            if allow_threshold_entry:
                last_threshold_entry_time[coin] = now
                signal_details['signal'] = 'LONG'
                print(f"[SIGNAL] ⚡ {coin}: THRESHOLD BUY triggered (StochRSI K={k:.2f}, score={long_score:.1f})")
                return signal_details['signal'], signal_details
            else:
                print(f"[SIGNAL] ⏳ {coin}: StochRSI touching BUY level but in cooldown ({time_since_last_threshold:.1f}min of {FUTURES_THRESHOLD_MONITORING_WINDOW_MINUTES}min)")
        elif strong_sell_trigger:
            if allow_threshold_entry:
                last_threshold_entry_time[coin] = now
                signal_details['signal'] = 'SHORT'
                print(f"[SIGNAL] ⚡ {coin}: THRESHOLD SELL triggered (StochRSI K={k:.2f}, score={short_score:.1f})")
                return signal_details['signal'], signal_details
            else:
                print(f"[SIGNAL] ⏳ {coin}: StochRSI touching SELL level but in cooldown ({time_since_last_threshold:.1f}min of {FUTURES_THRESHOLD_MONITORING_WINDOW_MINUTES}min)")

        print(f"[SIGNAL] {coin}: StochRSI K={k:.2f} — outside band (BUY: %K<={FUTURES_STOCH_RSI_BUY_LEVEL}, SELL: %K>={FUTURES_STOCH_RSI_SHORT_LEVEL}). No entry.")
        signal_details['reasons'].append(
            f"No threshold hit (K={k:.2f}, need %K <= {FUTURES_STOCH_RSI_BUY_LEVEL} for BUY or %K >= {FUTURES_STOCH_RSI_SHORT_LEVEL} for SELL)"
        )
        return "NO_TRADE", signal_details
    
    except Exception as e:
        print(f"[SIGNAL] Error generating signal for {coin}: {e}")
        signal_details['reasons'].append(f"Error: {e}")
        return "NO_TRADE", signal_details


# =====================================================
# CRITICAL: SYMBOL VALIDATION FUNCTIONS
# =====================================================

def validate_symbol_on_exchange(client, symbol):
    """
    Validate that symbol exists on Binance and is tradeable.
    Returns: (is_valid, min_notional, lot_size, error_msg, max_qty)
    """
    try:
        exchange_info = client.futures_exchange_info()
        
        for sym_info in exchange_info['symbols']:
            if sym_info['symbol'] == symbol:
                # Symbol found - check if it's tradeable
                if sym_info['status'] != 'TRADING':
                    return False, None, None, f"Symbol {symbol} status is {sym_info['status']} (not TRADING)", None
                
                # Extract required filters
                min_notional = None
                lot_size = None
                max_qty = None
                
                for f in sym_info['filters']:
                    if f['filterType'] == 'MIN_NOTIONAL':
                        min_notional = float(f.get('notional', 0))
                    elif f['filterType'] == 'LOT_SIZE':
                        lot_size = float(f.get('stepSize', 0))
                        raw_max = float(f.get('maxQty', 0))
                        max_qty = raw_max if raw_max > 0 else None
                
                if min_notional is None or lot_size is None:
                    return False, None, None, f"Symbol {symbol} missing required filters", None
                
                return True, min_notional, lot_size, None, max_qty
        
        return False, None, None, f"Symbol {symbol} not found on Binance Futures", None
        
    except Exception as e:
        print(f"[VALIDATE] Error validating {symbol}: {e}")
        return False, None, None, str(e), None


def validate_order_before_placement(client, symbol, quantity, current_price):
    """
    Comprehensive validation before placing order.
    Returns: (is_valid, error_msg)
    """
    try:
        # 1. Validate symbol exists
        is_valid, min_notional, lot_size, error_msg, max_qty = validate_symbol_on_exchange(client, symbol)
        if not is_valid:
            return False, f"Symbol validation failed: {error_msg}"
        
        # 2. Validate quantity
        if quantity <= 0:
            return False, f"Invalid quantity: {quantity}"
        
        # 3. Check maxQty (prevents "Quantity greater than max quantity" error)
        if max_qty and quantity > max_qty:
            return False, f"Quantity {quantity} exceeds exchange maxQty {max_qty} for {symbol}"
        
        # 4. Validate notional value
        notional_value = current_price * quantity
        if notional_value < min_notional:
            return False, f"Notional value ${notional_value:.2f} < minimum ${min_notional:.2f}"
        
        # 5. Validate lot size precision
        adjusted_qty = (quantity // lot_size) * lot_size
        if adjusted_qty <= 0:
            return False, f"Quantity {quantity} too small for lot size {lot_size}"
        
        # 6. Check account balance
        try:
            balance = get_wallet_balance_future(client)
            if balance < 2.0:
                return False, f"Insufficient balance: ${balance:.2f}"
        except:
            pass  # If we can't check balance, don't block the order
        
        return True, None
        
    except Exception as e:
        print(f"[VALIDATE] Validation error for {symbol}: {e}")
        return False, str(e)


# =====================================================
# SL / TP ORDER PLACEMENT
# =====================================================

def place_sl_tp_orders(client, symbol, position_type, tp_price, sl_price):
    """
    Place TAKE_PROFIT_MARKET (TP) and STOP_MARKET (SL) reduce-only orders on Binance
    Futures for an already-open position.  Both orders use closePosition=True so they
    close the full position regardless of the exact quantity on the exchange.

    Args:
        client:        Binance client
        symbol:        Trading symbol, e.g. 'BTCUSDT'
        position_type: 'LONG' or 'SHORT'
        tp_price:      Take-profit trigger price
        sl_price:      Stop-loss trigger price

    Returns:
        (tp_ok: bool, sl_ok: bool, tp_error: str | None, sl_error: str | None)
    """
    from utils.precision import adjust_price

    close_side = "SELL" if position_type == "LONG" else "BUY"
    adj_tp = adjust_price(symbol, tp_price, client)
    adj_sl = adjust_price(symbol, sl_price, client)

    tp_ok, sl_ok = False, False
    tp_error, sl_error = None, None

    try:
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=adj_tp,
            closePosition=True,
        )
        tp_ok = True
        print(f"[SL/TP] ✅ TP placed for {symbol} {position_type} at {adj_tp}")
    except Exception as e:
        tp_error = str(e)
        print(f"[SL/TP] ❌ TP placement failed for {symbol}: {tp_error}")

    try:
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="STOP_MARKET",
            stopPrice=adj_sl,
            closePosition=True,
        )
        sl_ok = True
        print(f"[SL/TP] ✅ SL placed for {symbol} {position_type} at {adj_sl}")
    except Exception as e:
        sl_error = str(e)
        print(f"[SL/TP] ❌ SL placement failed for {symbol}: {sl_error}")

    return tp_ok, sl_ok, tp_error, sl_error


# =====================================================
# RETRY LOGIC FOR ORDER PLACEMENT (Production Grade)
# =====================================================

async def place_order_with_retry(client, symbol, side, quantity, max_retries=3, backoff_seconds=2):
    """
    Place order with exponential backoff retry logic.
    
    Args:
        client: Binance client
        symbol: Trading symbol
        side: "BUY" or "SELL"
        quantity: Order quantity
        max_retries: Number of retry attempts
        backoff_seconds: Initial backoff duration (doubles after each retry)
    
    Returns:
        (order_dict, success: bool, error_msg: str)
    """
    attempt = 0
    last_error = None
    
    while attempt < max_retries:
        try:
            adjusted_quantity = adjust_quantity(symbol, quantity, client)
            if adjusted_quantity <= 0:
                return None, False, f"Adjusted quantity is invalid: {adjusted_quantity}"

            print(f"[ORDER-RETRY] 🔄 Attempting to place {side} order for {symbol} (Attempt {attempt + 1}/{max_retries})")
            
            order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=adjusted_quantity,
            )
            
            # Verify order was filled
            if order and order.get('status') in ['FILLED', 'NEW']:
                print(f"[ORDER-RETRY] ✅ {side} order placed successfully for {symbol}")
                return order, True, None
            else:
                last_error = f"Order status: {order.get('status', 'UNKNOWN')}"
                print(f"[ORDER-RETRY] ⚠️ Order not filled: {last_error}")
        
        except BinanceAPIException as e:
            last_error = f"Binance API Error {e.status_code}: {e.message}"
            
            # Don't retry on certain errors
            if e.status_code in [400, 403, 404]:  # Bad request, insufficient balance, etc.
                print(f"[ORDER-RETRY] ❌ Non-retryable error: {last_error}")
                return None, False, last_error
            
            print(f"[ORDER-RETRY] ⚠️ Attempt {attempt + 1} failed: {last_error}")
        
        except Exception as e:
            last_error = str(e)
            print(f"[ORDER-RETRY] ⚠️ Attempt {attempt + 1} failed: {last_error}")
        
        attempt += 1
        
        # Exponential backoff
        if attempt < max_retries:
            wait_time = backoff_seconds * (2 ** (attempt - 1))
            print(f"[ORDER-RETRY] ⏳ Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
    
    # All retries exhausted
    print(f"[ORDER-RETRY] ❌ Failed after {max_retries} attempts: {last_error}")
    return None, False, last_error
