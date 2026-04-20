"""
MT5 Forex Signal Analysis (MetaAPI Cloud)
Stochastic RSI + Trend + Momentum + Volume — adapted for MT5 forex
data via MetaAPI.

All functions are async and take a ctx (MT5UserContext) parameter
to support multi-user trading.
"""
import numpy as np

from mt5.mt5_core import (
    get_candles,
    get_close_prices,
)
from mt5.mt5_config import (
    MIN_SIGNAL_SCORE,
    SIGNAL_TIMEFRAME,
    STOCH_RSI_BUY_LEVEL,
    STOCH_RSI_SHORT_LEVEL,
    STOCH_K_SMOOTH,
    STOCH_D_SMOOTH,
    TREND_TIMEFRAME,
    STOCH_RSI_PERIOD,
    RSI_PERIOD,
)


# =====================================================
# RSI (Wilder's smoothing)
# =====================================================
def _compute_rsi_series(close_prices, period=14):
    """Compute full RSI series from close prices using Wilder's smoothing."""
    if len(close_prices) < period + 1:
        return [50.0] * len(close_prices)

    rsi_values = [None] * period

    gains, losses = [], []
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


# =====================================================
# STOCHASTIC RSI
# =====================================================
async def calculate_stochastic_rsi(
    ctx, symbol, timeframe=SIGNAL_TIMEFRAME,
    rsi_period=RSI_PERIOD, stoch_period=STOCH_RSI_PERIOD,
    k_smooth=STOCH_K_SMOOTH, d_smooth=STOCH_D_SMOOTH,
):
    """
    Stochastic RSI using Wilder RSI as the source series.
    Matches chart settings like `Stoch RSI 14 14 3 3`.

    Returns:
        dict: {k, d, rsi, crossover}
    """
    # WHY 2000 candles?
    # Wilder's smoothing RSI is an EMA-style calculation. Its initial average is
    # seeded from only the first `rsi_period` bars, so every subsequent bar is a
    # weighted blend of that warm-up seed and the real data.  With too few bars
    # the seed dominates and RSI never converges to the chart value.
    # TradingView effectively uses thousands of historical bars. To match it:
    #   - We need at least ~10-20x rsi_period bars just to converge the RSI.
    #   - Then stoch_period + k_smooth + d_smooth bars on top of that.
    #   - 2000 M5 bars = ~7 days of data — more than enough for full convergence.
    candles_needed = 2000
    close_prices = await get_close_prices(ctx, symbol, timeframe, candles_needed)
    print(
        f"[STOCHRSI] {symbol} | RSI={rsi_period} Stoch={stoch_period} "
        f"K={k_smooth} D={d_smooth} | Requested={candles_needed} | Received={len(close_prices) if close_prices else 0}"
    )

    if close_prices is None or len(close_prices) < rsi_period + stoch_period:
        return {"k": 50.0, "d": 50.0, "rsi": 50.0, "crossover": "NONE"}

    rsi_series = _compute_rsi_series(close_prices, rsi_period)
    valid_rsi = [r for r in rsi_series if r is not None]
    if len(valid_rsi) < stoch_period:
        return {"k": 50.0, "d": 50.0, "rsi": valid_rsi[-1] if valid_rsi else 50.0, "crossover": "NONE"}

    stoch_rsi_raw = []
    for i in range(stoch_period - 1, len(valid_rsi)):
        window = valid_rsi[i - stoch_period + 1: i + 1]
        rsi_min = min(window)
        rsi_max = max(window)
        if rsi_max - rsi_min == 0:
            stoch_rsi_raw.append(50.0)
        else:
            stoch_rsi_raw.append(((valid_rsi[i] - rsi_min) / (rsi_max - rsi_min)) * 100.0)

    if len(stoch_rsi_raw) < k_smooth:
        return {"k": 50.0, "d": 50.0, "rsi": valid_rsi[-1], "crossover": "NONE"}

    k_values = []
    for i in range(k_smooth - 1, len(stoch_rsi_raw)):
        k_val = sum(stoch_rsi_raw[i - k_smooth + 1: i + 1]) / k_smooth
        k_values.append(k_val)

    if len(k_values) < d_smooth:
        return {"k": k_values[-1] if k_values else 50.0, "d": 50.0, "rsi": valid_rsi[-1], "crossover": "NONE"}

    d_values = []
    for i in range(d_smooth - 1, len(k_values)):
        d_val = sum(k_values[i - d_smooth + 1: i + 1]) / d_smooth
        d_values.append(d_val)

    current_k = k_values[-1]
    current_d = d_values[-1]

    # Crossover detection
    crossover = "NONE"
    if len(k_values) >= 2 and len(d_values) >= 2:
        prev_k = k_values[-2]
        prev_d = d_values[-2]
        if prev_k <= prev_d and current_k > current_d:
            crossover = "BULLISH"
        elif prev_k >= prev_d and current_k < current_d:
            crossover = "BEARISH"

    return {
        "k": round(current_k, 2),
        "d": round(current_d, 2),
        "rsi": round(valid_rsi[-1], 2),
        "crossover": crossover,
    }


# =====================================================
# TREND DETECTION (EMA)
# =====================================================
async def get_trend_with_ma(ctx, symbol, timeframe=TREND_TIMEFRAME, short_period=20, long_period=50):
    """
    Determine trend using SMA crossover.

    Args:
        ctx: MT5UserContext

    Returns:
        tuple: (trend_str, short_ma, long_ma)
    """
    close_prices = await get_close_prices(ctx, symbol, timeframe, long_period + 10)

    if close_prices is None or len(close_prices) < long_period:
        return "SIDEWAYS", 0, 0

    short_ma = sum(close_prices[-short_period:]) / short_period
    long_ma = sum(close_prices[-long_period:]) / long_period
    diff_pct = ((short_ma - long_ma) / long_ma) * 100

    if diff_pct > 0.05:       # Tighter threshold for forex (smaller moves)
        return "UPTREND", short_ma, long_ma
    elif diff_pct < -0.05:
        return "DOWNTREND", short_ma, long_ma
    else:
        return "SIDEWAYS", short_ma, long_ma


# =====================================================
# MOMENTUM (Candlestick reversal)
# =====================================================
async def detect_momentum_shift(ctx, symbol, timeframe=SIGNAL_TIMEFRAME):
    """Detect bullish/bearish reversal from last 3 candles."""
    candles = await get_candles(ctx, symbol, timeframe, 5)
    if candles is None or len(candles) < 3:
        return "NO_SIGNAL"

    c = candles[-3:]
    last, prev = c[-1], c[-2]

    last_green = last["close"] > last["open"]
    prev_red = prev["close"] < prev["open"]

    # Bullish reversal
    if last_green and prev_red and last["close"] > prev["close"]:
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        if rng > 0 and body / rng > 0.4:
            return "BULLISH_REVERSAL"

    # Bearish reversal
    last_red = last["close"] < last["open"]
    prev_green = prev["close"] > prev["open"]

    if last_red and prev_green and last["close"] < prev["close"]:
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        if rng > 0 and body / rng > 0.4:
            return "BEARISH_REVERSAL"

    return "NO_SIGNAL"


# =====================================================
# VOLUME CONFIRMATION
# =====================================================
async def get_volume_confirmation(ctx, symbol, timeframe=SIGNAL_TIMEFRAME):
    """Check if current volume is above average."""
    candles = await get_candles(ctx, symbol, timeframe, 20)
    if candles is None or len(candles) < 10:
        return False, 1.0

    volumes = [c["volume"] for c in candles]
    avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 1
    current_vol = volumes[-1]

    if avg_vol == 0:
        return False, 1.0

    ratio = current_vol / avg_vol
    return ratio >= 0.8, round(ratio, 2)


# =====================================================
# ENGULFING PATTERN
# =====================================================
async def detect_engulfing(ctx, symbol, timeframe="D1", count=5):
    """Detect bullish/bearish engulfing pattern."""
    candles = await get_candles(ctx, symbol, timeframe, count)
    if candles is None or len(candles) < 2:
        return "neutral"

    prev = candles[-2]
    curr = candles[-1]

    # Bullish engulfing
    if (prev["close"] < prev["open"] and curr["close"] > curr["open"]
            and curr["close"] > prev["open"] and curr["open"] < prev["close"]):
        return "bullish"

    # Bearish engulfing
    if (prev["close"] > prev["open"] and curr["close"] < curr["open"]
            and curr["open"] > prev["close"] and curr["close"] < prev["open"]):
        return "bearish"

    return "neutral"


# =====================================================
# COMBINED SIGNAL (StochRSI + Trend + Momentum + Volume)
# =====================================================
async def get_trade_signal_mt5(ctx, symbol, current_bid, support, resistance):
    """
    Advanced trade signal for MT5 Forex via MetaAPI.
    Same scoring system as the Binance futures bot.

    Args:
        ctx: MT5UserContext

    Returns:
        str: "BUY", "SELL", or "NO_TRADE"
        dict: signal details with reasons
    """
    details = {
        "signal": "NO_TRADE",
        "stoch_rsi_k": None,
        "stoch_rsi_d": None,
        "rsi": None,
        "crossover": None,
        "trend": None,
        "momentum": None,
        "volume_confirmed": False,
        "reasons": [],
    }

    try:
        # 1. Stochastic RSI
        srsi = await calculate_stochastic_rsi(ctx, symbol)
        k, d, rsi, crossover = srsi["k"], srsi["d"], srsi["rsi"], srsi["crossover"]
        details.update({"stoch_rsi_k": k, "stoch_rsi_d": d, "rsi": rsi, "crossover": crossover})

        # Guard: if k/d both == 50.0 exactly, MetaAPI likely timed out — skip this cycle
        if k == 50.0 and d == 50.0:
            print(f"[MT5-SIGNAL] {symbol}: StochRSI returned default 50/50 — MetaAPI data unavailable, skipping")
            details["reasons"].append("StochRSI data unavailable (MetaAPI timeout) — skipped")
            return "NO_TRADE", details

        # 2. Trend
        trend, short_ma, long_ma = await get_trend_with_ma(ctx, symbol)
        details["trend"] = trend

        # 3. Momentum
        momentum = await detect_momentum_shift(ctx, symbol)
        details["momentum"] = momentum

        # 4. Volume
        vol_ok, vol_ratio = await get_volume_confirmation(ctx, symbol)
        details["volume_confirmed"] = vol_ok
        details["volume_ratio"] = vol_ratio

        # Price position in S/R range
        if support and resistance and resistance > support:
            price_position = (current_bid - support) / (resistance - support)
        else:
            price_position = 0.5
        details["price_position"] = price_position

        # ---------- BUY score ----------
        buy_score = 0.0

        strong_buy_trigger = round(float(k), 2) <= round(float(STOCH_RSI_BUY_LEVEL), 2)
        strong_sell_trigger = round(float(k), 2) >= round(float(STOCH_RSI_SHORT_LEVEL), 2)

        if strong_buy_trigger:
            buy_score += 4
            details["reasons"].append(
                f"StochRSI buy trigger hit (%K={k:.2f} <= {STOCH_RSI_BUY_LEVEL:.2f})"
            )
        elif k < 20:
            buy_score += 2
            details["reasons"].append(f"StochRSI oversold (%K={k:.1f})")
        elif k < 35:
            buy_score += 1
            details["reasons"].append(f"StochRSI low (%K={k:.1f})")

        if crossover == "BULLISH":
            buy_score += 2
            details["reasons"].append(f"StochRSI bullish crossover (%K={k:.1f} > %D={d:.1f})")

        if rsi < 45:
            buy_score += 1
            details["reasons"].append(f"RSI supports BUY ({rsi:.1f})")

        if trend == "UPTREND":
            buy_score += 1.5
            details["reasons"].append("Uptrend (MA crossover)")
        elif trend == "SIDEWAYS":
            buy_score += 0.5

        if momentum == "BULLISH_REVERSAL":
            buy_score += 1.5
            details["reasons"].append("Bullish reversal candle")

        if price_position < 0.35:
            buy_score += 1
            details["reasons"].append(f"Price near support ({price_position*100:.0f}%)")
        elif price_position < 0.5:
            buy_score += 0.5

        if vol_ok and vol_ratio > 1.2:
            buy_score += 1
            details["reasons"].append(f"Strong volume ({vol_ratio:.1f}x)")

        # ---------- SELL score ----------
        sell_score = 0.0

        if strong_sell_trigger:
            sell_score += 4
            details["reasons"].append(
                f"StochRSI short trigger hit (%K={k:.2f} >= {STOCH_RSI_SHORT_LEVEL:.2f})"
            )
        elif k > 80:
            sell_score += 2
            details["reasons"].append(f"StochRSI overbought (%K={k:.1f})")
        elif k > 65:
            sell_score += 1
            details["reasons"].append(f"StochRSI high (%K={k:.1f})")

        if crossover == "BEARISH":
            sell_score += 2
            details["reasons"].append(f"StochRSI bearish crossover (%K={k:.1f} < %D={d:.1f})")

        if rsi > 55:
            sell_score += 1
            details["reasons"].append(f"RSI supports SELL ({rsi:.1f})")

        if trend == "DOWNTREND":
            sell_score += 1.5
            details["reasons"].append("Downtrend (MA crossover)")
        elif trend == "SIDEWAYS":
            sell_score += 0.5

        if momentum == "BEARISH_REVERSAL":
            sell_score += 1.5
            details["reasons"].append("Bearish reversal candle")

        if price_position > 0.65:
            sell_score += 1
            details["reasons"].append(f"Price near resistance ({price_position*100:.0f}%)")
        elif price_position > 0.5:
            sell_score += 0.5

        if vol_ok and vol_ratio > 1.2:
            sell_score += 1

        # ---------- Final Score for Reporting ----------
        details["buy_score"] = buy_score
        details["sell_score"] = sell_score
        details["score"] = max(buy_score, sell_score)

        # ---------- Decision: threshold entries (BUY if %K <= buy level, SELL if %K >= short level) ----------
        # ---------- Decision: band trigger + score gate ----------
        if strong_buy_trigger and buy_score >= MIN_SIGNAL_SCORE:
            details["signal"] = "BUY"
        elif strong_sell_trigger and sell_score >= MIN_SIGNAL_SCORE:
            details["signal"] = "SELL"
        elif buy_score >= MIN_SIGNAL_SCORE + 2 and buy_score > sell_score:
            details["signal"] = "BUY"
            details["reasons"].append(f"Score-based BUY (score={buy_score:.1f})")
        elif sell_score >= MIN_SIGNAL_SCORE + 2 and sell_score > buy_score:
            details["signal"] = "SELL"
            details["reasons"].append(f"Score-based SELL (score={sell_score:.1f})")
        else:
            details["reasons"].append(
                f"No entry: K={k:.2f} (need <={STOCH_RSI_BUY_LEVEL} or >={STOCH_RSI_SHORT_LEVEL}), buy_score={buy_score:.1f}, sell_score={sell_score:.1f}"
            )

        return details["signal"], details

    except Exception as e:
        print(f"[MT5-SIGNAL] Error for {symbol}: {e}")
        details["reasons"].append(f"Error: {e}")
        return "NO_TRADE", details
