"""
MEXC Futures Trading Module
Uses CCXT for API calls to MEXC exchange.
All trading is perpetual (swap) contracts quoted in USDT.
"""
import time
import numpy as np


# ─────────────────────────────────────────────────────────
# Symbol conversion helpers
# ─────────────────────────────────────────────────────────

def to_ccxt_symbol(symbol: str) -> str:
    """Convert 'BTCUSDT' -> 'BTC/USDT:USDT' (ccxt swap format)."""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    return symbol


def to_raw_symbol(ccxt_symbol: str) -> str:
    """Convert 'BTC/USDT:USDT' -> 'BTCUSDT'."""
    base = ccxt_symbol.split("/")[0]
    return f"{base}USDT"


# ─────────────────────────────────────────────────────────
# Balance - Using CCXT fetch_balance()
# ─────────────────────────────────────────────────────────

def get_mexc_wallet_balance(client) -> float:
    """
    Return the USDT balance from the MEXC account.
    Uses CCXT's fetch_balance() for swap/futures.
    """
    try:
        bal = client.fetch_balance({"type": "swap"})
        usdt = bal.get("USDT", {})
        # 'total' = equity, 'free' = available margin
        total = usdt.get("total") or usdt.get("free") or 0.0
        return float(total)
    except Exception as e:
        print(f"[MEXC] Error fetching wallet balance: {e}")
        return 0.0


def get_real_active_mexc_count(client) -> int:
    """
    Return the number of open MEXC futures positions.
    Uses CCXT's fetch_positions().
    """
    try:
        positions = client.fetch_positions()
        return sum(
            1 for p in positions
            if float(p.get("contracts") or p.get("contractSize") or 0) != 0
            or float(p.get("info", {}).get("vol", 0)) != 0
        )
    except Exception as e:
        print(f"[MEXC] Error fetching active positions: {e}")
        return 0


# ─────────────────────────────────────────────────────────
# Price Data - Using CCXT fetch_ohlcv()
# ─────────────────────────────────────────────────────────

def get_mexc_recent_prices(symbol: str, interval: str = "1m", lookback: int = 100, client=None) -> np.ndarray:
    """
    Fetch recent closing prices for a MEXC futures symbol.
    Uses CCXT's fetch_ohlcv() method.

    Args:
        symbol: Symbol like 'BTCUSDT'
        interval: OHLCV timeframe ('1m', '5m', '15m', etc.)
        lookback: Number of candles to fetch
        client: CCXT MEXC client

    Returns:
        np.ndarray of closing prices
    """
    try:
        ccxt_sym = to_ccxt_symbol(symbol)
        ohlcv = client.fetch_ohlcv(ccxt_sym, interval, limit=lookback)
        closing_prices = [candle[4] for candle in ohlcv]  # index 4 = close
        return np.array(closing_prices).reshape(-1, 1)
    except Exception as e:
        print(f"[MEXC] Error fetching prices for {symbol}: {e}")
        return None


def _fetch_klines(symbol: str, interval: str, limit: int, client) -> list:
    """Internal helper: fetch OHLCV candles using CCXT."""
    ccxt_sym = to_ccxt_symbol(symbol)
    return client.fetch_ohlcv(ccxt_sym, interval, limit=limit)


# ─────────────────────────────────────────────────────────
# Support / Resistance
# ─────────────────────────────────────────────────────────

def find_mexc_support_level(symbol: str, client) -> float | None:
    """Compute support using the 10th-percentile low of recent 4-h candles."""
    try:
        klines = _fetch_klines(symbol, "4h", 24, client)   # ~4 days
        lows = sorted([c[3] for c in klines])              # index 3 = low
        idx = max(1, int(len(lows) * 0.1))
        support = lows[idx]
        print(f"[MEXC-SUPPORT] {symbol}: support={support:.6f}")
        return support
    except Exception as e:
        print(f"[MEXC-SUPPORT] Error for {symbol}: {e}")
        try:
            ticker = client.fetch_ticker(to_ccxt_symbol(symbol))
            return float(ticker["last"]) * 0.98
        except Exception:
            return None


def find_mexc_resistance_level(symbol: str, client) -> float | None:
    """Compute resistance using the 90th-percentile high of recent 4-h candles."""
    try:
        klines = _fetch_klines(symbol, "4h", 24, client)
        highs = sorted([c[2] for c in klines])             # index 2 = high
        idx = min(len(highs) - 2, int(len(highs) * 0.9))
        resistance = highs[idx]
        print(f"[MEXC-RESISTANCE] {symbol}: resistance={resistance:.6f}")
        return resistance
    except Exception as e:
        print(f"[MEXC-RESISTANCE] Error for {symbol}: {e}")
        try:
            ticker = client.fetch_ticker(to_ccxt_symbol(symbol))
            return float(ticker["last"]) * 1.02
        except Exception:
            return None


# ─────────────────────────────────────────────────────────
# Technical Indicators
# ─────────────────────────────────────────────────────────

def _compute_rsi_series(close_prices: list, period: int = 14) -> list:
    if len(close_prices) < period + 1:
        return [50.0] * len(close_prices)

    rsi_values: list = [None] * period
    gains = [max(close_prices[i] - close_prices[i - 1], 0) for i in range(1, period + 1)]
    losses = [max(close_prices[i - 1] - close_prices[i], 0) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    for i in range(period + 1, len(close_prices)):
        change = close_prices[i] - close_prices[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_values


def _calculate_mexc_stoch_rsi(symbol: str, client,
                               rsi_period: int = 14, stoch_period: int = 14,
                               k_smooth: int = 3, d_smooth: int = 3,
                               interval: str = "15m") -> dict:
    """Stochastic RSI calculation using MEXC OHLCV data."""
    try:
        # Wilder's RSI needs many bars to converge (EMA-style warm-up).
        # With too few bars the initial seed dominates and values diverge from charts.
        # 500 bars (~5 days of 15m data) gives full convergence.
        needed = 500
        klines = _fetch_klines(symbol, interval, needed, client)
        close_prices = [c[4] for c in klines]

        if len(close_prices) < rsi_period + stoch_period:
            return {"k": 50.0, "d": 50.0, "rsi": 50.0, "crossover": "NONE"}

        rsi_series = _compute_rsi_series(close_prices, rsi_period)
        valid_rsi = [r for r in rsi_series if r is not None]

        if len(valid_rsi) < stoch_period:
            return {"k": 50.0, "d": 50.0, "rsi": valid_rsi[-1] if valid_rsi else 50.0, "crossover": "NONE"}

        stoch_raw = []
        for i in range(stoch_period - 1, len(valid_rsi)):
            window = valid_rsi[i - stoch_period + 1: i + 1]
            lo, hi = min(window), max(window)
            stoch_raw.append(((valid_rsi[i] - lo) / (hi - lo) * 100) if hi != lo else 50.0)

        if len(stoch_raw) < k_smooth:
            return {"k": 50.0, "d": 50.0, "rsi": valid_rsi[-1], "crossover": "NONE"}

        k_values = [
            sum(stoch_raw[i - k_smooth + 1: i + 1]) / k_smooth
            for i in range(k_smooth - 1, len(stoch_raw))
        ]

        if len(k_values) < d_smooth:
            return {"k": k_values[-1] if k_values else 50.0, "d": 50.0, "rsi": valid_rsi[-1], "crossover": "NONE"}

        d_values = [
            sum(k_values[i - d_smooth + 1: i + 1]) / d_smooth
            for i in range(d_smooth - 1, len(k_values))
        ]

        k, d = k_values[-1], d_values[-1]
        crossover = "NONE"
        if len(k_values) >= 2 and len(d_values) >= 2:
            pk, pd = k_values[-2], d_values[-2]
            if pk <= pd and k > d:
                crossover = "BULLISH"
            elif pk >= pd and k < d:
                crossover = "BEARISH"

        return {"k": round(k, 2), "d": round(d, 2), "rsi": round(valid_rsi[-1], 2), "crossover": crossover}

    except Exception as e:
        print(f"[MEXC-STOCHRSI] Error for {symbol}: {e}")
        return {"k": 50.0, "d": 50.0, "rsi": 50.0, "crossover": "NONE"}


def _get_mexc_trend(symbol: str, client, short_period: int = 20, long_period: int = 50) -> tuple:
    """EMA crossover trend for MEXC."""
    try:
        klines = _fetch_klines(symbol, "15m", long_period + 10, client)
        closes = [c[4] for c in klines]
        if len(closes) < long_period:
            return "SIDEWAYS", 0, 0
        short_ma = sum(closes[-short_period:]) / short_period
        long_ma = sum(closes[-long_period:]) / long_period
        diff_pct = (short_ma - long_ma) / long_ma * 100
        if diff_pct > 0.5:
            return "UPTREND", short_ma, long_ma
        elif diff_pct < -0.5:
            return "DOWNTREND", short_ma, long_ma
        return "SIDEWAYS", short_ma, long_ma
    except Exception as e:
        print(f"[MEXC-TREND] Error for {symbol}: {e}")
        return "SIDEWAYS", 0, 0


def _detect_mexc_momentum(symbol: str, client) -> str:
    """Candlestick momentum shift detection for MEXC."""
    try:
        klines = _fetch_klines(symbol, "15m", 5, client)
        if len(klines) < 3:
            return "NO_SIGNAL"
        candles = [{"open": c[1], "high": c[2], "low": c[3], "close": c[4]} for c in klines[-3:]]
        last, prev = candles[-1], candles[-2]
        last_green = last["close"] > last["open"]
        prev_red = prev["close"] < prev["open"]
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        if last_green and prev_red and last["close"] > prev["close"]:
            if rng > 0 and body / rng > 0.4:
                return "BULLISH_REVERSAL"
        last_red = last["close"] < last["open"]
        prev_green = prev["close"] > prev["open"]
        if last_red and prev_green and last["close"] < prev["close"]:
            if rng > 0 and body / rng > 0.4:
                return "BEARISH_REVERSAL"
        return "NO_SIGNAL"
    except Exception as e:
        print(f"[MEXC-MOMENTUM] Error for {symbol}: {e}")
        return "NO_SIGNAL"


def _check_mexc_volume(symbol: str, client) -> tuple:
    """Volume-above-average check for MEXC."""
    try:
        klines = _fetch_klines(symbol, "15m", 20, client)
        vols = [c[5] for c in klines]
        if len(vols) < 10:
            return False, 1.0
        avg = sum(vols[:-1]) / (len(vols) - 1)
        cur = vols[-1]
        ratio = cur / avg if avg else 1.0
        return ratio >= 0.8, ratio
    except Exception as e:
        print(f"[MEXC-VOLUME] Error for {symbol}: {e}")
        return False, 1.0


# ─────────────────────────────────────────────────────────
# Signal Generation
# ─────────────────────────────────────────────────────────

def get_mexc_trade_signal(symbol: str, client,
                           current_price: float = None,
                           support: float = None,
                           resistance: float = None) -> tuple:
    """
    Advanced trade signal for MEXC futures with threshold-based entry (MT5-inspired).

    Entry logic mirrors MT5:
    - StochRSI %K <= MEXC_STOCH_RSI_BUY_LEVEL  -> LONG  (oversold threshold, +4 to score)
    - StochRSI %K >= MEXC_STOCH_RSI_SHORT_LEVEL -> SHORT (overbought threshold, +4 to score)
    - Combined scoring (StochRSI zone, RSI, trend, momentum, S/R, volume) logged alongside
    - Cooldown window prevents re-entry within MEXC_THRESHOLD_MONITORING_WINDOW_MINUTES

    Returns:
        (signal: str, details: dict)  — signal is "LONG", "SHORT", or "NO_TRADE"
    """
    details = {
        "signal": "NO_TRADE",
        "stoch_rsi_k": None, "stoch_rsi_d": None, "rsi": None, "crossover": None,
        "trend": None, "momentum": None, "volume_confirmed": False,
        "reasons": [],
    }

    try:
        from config import (
            MEXC_STOCH_RSI_BUY_LEVEL,
            MEXC_STOCH_RSI_SHORT_LEVEL,
            MEXC_THRESHOLD_MONITORING_WINDOW_MINUTES,
        )
        from datetime import datetime

        # 1. Stochastic RSI
        stoch = _calculate_mexc_stoch_rsi(symbol, client)
        details.update({
            "stoch_rsi_k": stoch["k"], "stoch_rsi_d": stoch["d"],
            "rsi": stoch["rsi"], "crossover": stoch["crossover"],
        })
        k, d, rsi, crossover = stoch["k"], stoch["d"], stoch["rsi"], stoch["crossover"]

        # 2. Trend
        trend, short_ma, long_ma = _get_mexc_trend(symbol, client)
        details["trend"] = trend

        # 3. Momentum
        momentum = _detect_mexc_momentum(symbol, client)
        details["momentum"] = momentum

        # 4. Volume
        vol_ok, vol_ratio = _check_mexc_volume(symbol, client)
        details["volume_confirmed"] = vol_ok
        details["volume_ratio"] = vol_ratio

        # Price position in S/R range
        if current_price and support and resistance and resistance > support:
            price_position = (current_price - support) / (resistance - support)
            price_position = max(0.0, min(1.0, price_position))
        else:
            price_position = 0.5
        details["price_position"] = price_position

        # ═══════════════════════════════════════════════════════════════════
        # THRESHOLD TRIGGERS + SCORING (MT5-inspired)
        # ═══════════════════════════════════════════════════════════════════
        strong_buy_trigger = round(float(k), 2) <= round(float(MEXC_STOCH_RSI_BUY_LEVEL), 2)
        strong_sell_trigger = round(float(k), 2) >= round(float(MEXC_STOCH_RSI_SHORT_LEVEL), 2)

        # ---------- LONG score ----------
        long_score = 0.0

        if strong_buy_trigger:
            long_score += 4
            details['reasons'].append(f"StochRSI buy trigger hit (%K={k:.2f} <= {MEXC_STOCH_RSI_BUY_LEVEL})")
        elif k < 20:
            long_score += 2
            details['reasons'].append(f"StochRSI oversold (%K={k:.1f})")
        elif k < 35:
            long_score += 1
            details['reasons'].append(f"StochRSI low (%K={k:.1f})")

        if crossover == 'BULLISH':
            long_score += 2
            details['reasons'].append(f"StochRSI bullish crossover (%K={k:.1f} > %D={d:.1f})")

        if rsi < 45:
            long_score += 1
            details['reasons'].append(f"RSI supports LONG ({rsi:.1f})")

        if trend == "UPTREND":
            long_score += 1.5
            details['reasons'].append("Uptrend confirmed (MA crossover)")
        elif trend == "SIDEWAYS":
            long_score += 0.5

        if momentum == "BULLISH_REVERSAL":
            long_score += 1.5
            details['reasons'].append("Bullish reversal pattern")

        if price_position < 0.35:
            long_score += 1
            details['reasons'].append(f"Price near support ({price_position*100:.0f}%)")
        elif price_position < 0.5:
            long_score += 0.5

        if vol_ok and vol_ratio > 1.2:
            long_score += 1
            details['reasons'].append(f"Strong volume ({vol_ratio:.1f}x avg)")

        # ---------- SHORT score ----------
        short_score = 0.0

        if strong_sell_trigger:
            short_score += 4
            details['reasons'].append(f"StochRSI short trigger hit (%K={k:.2f} >= {MEXC_STOCH_RSI_SHORT_LEVEL})")
        elif k > 80:
            short_score += 2
            details['reasons'].append(f"StochRSI overbought (%K={k:.1f})")
        elif k > 65:
            short_score += 1
            details['reasons'].append(f"StochRSI high (%K={k:.1f})")

        if crossover == 'BEARISH':
            short_score += 2
            details['reasons'].append(f"StochRSI bearish crossover (%K={k:.1f} < %D={d:.1f})")

        if rsi > 55:
            short_score += 1
            details['reasons'].append(f"RSI supports SHORT ({rsi:.1f})")

        if trend == "DOWNTREND":
            short_score += 1.5
            details['reasons'].append("Downtrend confirmed (MA crossover)")
        elif trend == "SIDEWAYS":
            short_score += 0.5

        if momentum == "BEARISH_REVERSAL":
            short_score += 1.5
            details['reasons'].append("Bearish reversal pattern")

        if price_position > 0.65:
            short_score += 1
            details['reasons'].append(f"Price near resistance ({price_position*100:.0f}%)")
        elif price_position > 0.5:
            short_score += 0.5

        if vol_ok and vol_ratio > 1.2:
            short_score += 1

        details['long_score'] = long_score
        details['short_score'] = short_score

        # ═══════════════════════════════════════════════════════════════════
        # THRESHOLD-BASED ENTRY DECISION
        # ═══════════════════════════════════════════════════════════════════
        if not hasattr(get_mexc_trade_signal, 'last_threshold_time'):
            get_mexc_trade_signal.last_threshold_time = {}

        now = datetime.now()
        time_since_last = None
        if symbol in get_mexc_trade_signal.last_threshold_time:
            time_since_last = (now - get_mexc_trade_signal.last_threshold_time[symbol]).total_seconds() / 60

        allow_threshold = (time_since_last is None or time_since_last >= MEXC_THRESHOLD_MONITORING_WINDOW_MINUTES)

        if strong_buy_trigger:
            if allow_threshold:
                get_mexc_trade_signal.last_threshold_time[symbol] = now
                details['signal'] = 'LONG'
                print(f"[MEXC-SIGNAL] ⚡ {symbol}: THRESHOLD BUY triggered (K={k:.2f}, score={long_score:.1f})")
                return details['signal'], details
            else:
                print(f"[MEXC-SIGNAL] ⏳ {symbol}: K={k:.2f} touching BUY level but in cooldown ({time_since_last:.1f}min/{MEXC_THRESHOLD_MONITORING_WINDOW_MINUTES}min)")
        elif strong_sell_trigger:
            if allow_threshold:
                get_mexc_trade_signal.last_threshold_time[symbol] = now
                details['signal'] = 'SHORT'
                print(f"[MEXC-SIGNAL] ⚡ {symbol}: THRESHOLD SELL triggered (K={k:.2f}, score={short_score:.1f})")
                return details['signal'], details
            else:
                print(f"[MEXC-SIGNAL] ⏳ {symbol}: K={k:.2f} touching SELL level but in cooldown ({time_since_last:.1f}min/{MEXC_THRESHOLD_MONITORING_WINDOW_MINUTES}min)")

        print(f"[MEXC-SIGNAL] {symbol}: StochRSI K={k:.2f} — outside band (BUY: %K<={MEXC_STOCH_RSI_BUY_LEVEL}, SELL: %K>={MEXC_STOCH_RSI_SHORT_LEVEL}). No entry.")
        details["reasons"].append(
            f"No threshold hit (K={k:.2f}, need %K <= {MEXC_STOCH_RSI_BUY_LEVEL} for BUY or %K >= {MEXC_STOCH_RSI_SHORT_LEVEL} for SELL)"
        )
        return details["signal"], details

    except Exception as e:
        print(f"[MEXC-SIGNAL] Error for {symbol}: {e}")
        return "NO_TRADE", details

# ─────────────────────────────────────────────────────────
# Order Execution - Using CCXT
# ─────────────────────────────────────────────────────────

def _adjust_mexc_amount(symbol: str, amount: float, client) -> float:
    """Round amount to MEXC's required precision for the symbol."""
    try:
        ccxt_sym = to_ccxt_symbol(symbol)
        market = client.market(ccxt_sym)
        precision = market.get("precision", {}).get("amount", 6)
        # Round to significant figures
        factor = 10 ** int(precision) if isinstance(precision, int) else 10 ** 6
        return int(amount * factor) / factor
    except Exception as e:
        print(f"[MEXC-PRECISION] Warning for {symbol}: {e}. Using raw amount.")
        return round(amount, 6)


def _calculate_mexc_quantity(wallet_balance: float, current_price: float,
                              leverage: int, wallet_pct: float) -> float:
    """Return quantity (base coin) from wallet percentage + leverage."""
    if current_price <= 0:
        return 0.0
    notional = wallet_balance * (wallet_pct / 100) * leverage
    return notional / current_price


def place_mexc_market_order(symbol: str, side: str, quantity: float,
                             leverage: int, client,
                             margin_type: str = "isolated") -> dict | None:
    """
    Place a MEXC futures market order using CCXT.

    Returns Order dict on success, None if order fails.
    """
    try:
        ccxt_sym = to_ccxt_symbol(symbol)

        # 1. Set margin mode to ISOLATED
        try:
            client.set_margin_mode(margin_type.lower(), ccxt_sym)
            print(f"[MEXC-MARGIN] ✅ {symbol} margin set to {margin_type}")
        except Exception as m_err:
            err_str = str(m_err)
            if "No need to change margin type" in err_str or "already" in err_str.lower():
                pass  # already ISOLATED
            else:
                print(f"[MEXC-MARGIN] ❌ {symbol}: {m_err}")

        # 2. Set leverage
        try:
            client.set_leverage(leverage, ccxt_sym)
            print(f"[MEXC-LEVERAGE] ✅ {symbol} leverage set to {leverage}x")
        except Exception as lev_err:
            print(f"[MEXC-LEVERAGE] ❌ {symbol}: {lev_err}")

        # 3. Place the order
        quantity = _adjust_mexc_amount(symbol, quantity, client)
        if quantity <= 0:
            print(f"[MEXC-ORDER] Quantity too small for {symbol}")
            return None

        order = client.create_order(
            symbol=ccxt_sym,
            type="market",
            side=side,
            amount=quantity,
            params={"positionSide": "BOTH"},
        )
        print(f"[MEXC-ORDER] ✅ {side.upper()} {quantity} {symbol} — order: {order.get('id')}")
        return order

    except Exception as e:
        print(f"[MEXC-ORDER] ❌ Error placing {side} order for {symbol}: {e}")
        return None


def long_mexc_trade(symbol: str, quantity: float, leverage: int, client) -> dict | None:
    """Open a LONG position on MEXC futures."""
    order = place_mexc_market_order(symbol, "buy", quantity, leverage, client)
    if order:
        print(f"[MEXC] ✅ Opened LONG for {symbol}, qty={quantity}")
    else:
        print(f"[MEXC] ❌ Failed to open LONG for {symbol}")
    return order


def short_mexc_trade(symbol: str, quantity: float, leverage: int, client) -> dict | None:
    """Open a SHORT position on MEXC futures."""
    order = place_mexc_market_order(symbol, "sell", quantity, leverage, client)
    if order:
        print(f"[MEXC] ✅ Opened SHORT for {symbol}, qty={quantity}")
    else:
        print(f"[MEXC] ❌ Failed to open SHORT for {symbol}")
    return order


def close_mexc_position(symbol: str, quantity: float, client, position_type: str = "LONG") -> dict | None:
    """
    Close a MEXC futures position using CCXT.

    Args:
        symbol: e.g. 'XRPUSDT'
        quantity: amount to close
        client: CCXT MEXC client
        position_type: 'LONG' or 'SHORT'
    """
    try:
        ccxt_sym = to_ccxt_symbol(symbol)
        close_side = "sell" if position_type == "LONG" else "buy"
        quantity = _adjust_mexc_amount(symbol, quantity, client)

        order = client.create_order(
            symbol=ccxt_sym,
            type="market",
            side=close_side,
            amount=quantity,
            params={"reduceOnly": True, "positionSide": "BOTH"},
        )
        print(f"[MEXC] ✅ Closed {position_type} position for {symbol}: {order.get('id')}")
        return order
    except Exception as e:
        print(f"[MEXC] ❌ Error closing {position_type} for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────
# Position Information - Using CCXT
# ─────────────────────────────────────────────────────────

def get_mexc_open_positions(client) -> list:
    """Return a list of open MEXC perpetual futures positions."""
    try:
        positions = client.fetch_positions()
        return [
            p for p in positions
            if abs(float(p.get("contracts") or p.get("info", {}).get("vol", 0))) > 0
        ]
    except Exception as e:
        print(f"[MEXC] Error fetching positions: {e}")
        return []


def get_mexc_position_for_symbol(symbol: str, client) -> dict | None:
    """Return the open position for a specific raw symbol (e.g. 'XRPUSDT'), or None."""
    try:
        ccxt_sym = to_ccxt_symbol(symbol)
        positions = client.fetch_positions([ccxt_sym])
        for p in positions:
            if abs(float(p.get("contracts") or p.get("info", {}).get("vol", 0))) > 0:
                return p
        return None
    except Exception as e:
        print(f"[MEXC] Error fetching position for {symbol}: {e}")
        return None


def get_mexc_current_price(symbol: str, client) -> float:
    """Fetch last traded price for a MEXC futures symbol."""
    try:
        ticker = client.fetch_ticker(to_ccxt_symbol(symbol))
        return float(ticker.get("last") or ticker.get("close") or 0)
    except Exception as e:
        print(f"[MEXC] Error fetching price for {symbol}: {e}")
        return 0.0


def get_mexc_detailed_status(client) -> dict:
    """
    Build a status dict compatible with the trading status display.
    Uses CCXT methods.
    """
    try:
        bal = client.fetch_balance({"type": "swap"})
        usdt = bal.get("USDT", {})
        total_balance = float(usdt.get("total") or 0)
        free_balance = float(usdt.get("free") or 0)

        positions = get_mexc_open_positions(client)
        total_unrealized = 0.0
        pos_details = []

        for p in positions:
            info = p.get("info", {})
            symbol_raw = to_raw_symbol(p.get("symbol", ""))
            side = "LONG" if float(p.get("contracts", 0)) > 0 else "SHORT"
            entry = float(p.get("entryPrice") or info.get("openPrice") or 0)
            current = float(p.get("markPrice") or info.get("markPrice") or 0)
            qty = abs(float(p.get("contracts") or info.get("vol") or 0))
            pnl = float(p.get("unrealizedPnl") or info.get("unrealisedPnl") or 0)
            leverage = int(float(p.get("leverage") or info.get("leverage") or 1))

            total_unrealized += pnl
            pnl_pct = (pnl / (entry * qty / leverage) * 100) if entry and qty and leverage else 0

            pos_details.append({
                "symbol": symbol_raw,
                "side": side,
                "entry_price": entry,
                "mark_price": current,
                "current_price": current,
                "size": qty,
                "leverage": leverage,
                "unrealized_pnl": pnl,
                "pnl_percentage": round(pnl_pct, 2),
                "liquidation_price": float(p.get("liquidationPrice") or info.get("liquidatePrice") or 0),
                "profit": pnl,
            })

        pnl_pct_total = (total_unrealized / total_balance * 100) if total_balance else 0

        return {
            "status": "Running",
            "trading_mode": "MEXC Futures",
            "balance": total_balance,
            "margin_balance": total_balance,
            "available_balance": free_balance,
            "total_initial_margin": 0,
            "total_maint_margin": 0,
            "unrealized_pnl": total_unrealized,
            "pnl_percentage": round(pnl_pct_total, 2),
            "active_trades": len(positions),
            "positions": pos_details,
        }
    except Exception as e:
        print(f"[MEXC-STATUS] Error: {e}")
        return {}

