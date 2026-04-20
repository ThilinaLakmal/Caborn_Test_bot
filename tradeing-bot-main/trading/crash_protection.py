"""
Market Crash Protection Module
Detects sudden market crashes (e.g., BTC 90000 -> 60000) and takes protective action:
  1. Emergency close all open positions
  2. Pause trading (circuit breaker)
  3. Daily max loss limit enforcement
  4. Volatility-based position size reduction
"""
from datetime import datetime

from config import (
    CRASH_REFERENCE_SYMBOL,
    CRASH_LOWER_THRESHOLD_PCT,
    CRASH_UPPER_THRESHOLD_PCT,
    CRASH_MONITORING_WINDOW,
    CRASH_MONITORING_CANDLES,
    CRASH_CLOSE_ALL_POSITIONS,
    CRASH_PAUSE_TRADING,
    CRASH_COOLDOWN_MINUTES,
    FUTURES_DAILY_TRADE_LIMIT_MODE,
    FUTURES_DAILY_MAX_TRADES,
    HIGH_VOLATILITY_THRESHOLD,
    HIGH_VOLATILITY_SIZE_REDUCTION,
    FUTURES_WALLET_PERCENTAGE,
)


class CrashProtection:
    """
    Monitors market health and protects capital during crashes.
    
    Strategy:
    - Watches BTC (or reference coin) for sudden drops as a market health proxy
    - If BTC drops > threshold in monitoring window -> CRASH MODE
    - In crash mode: close all positions, pause trading, wait for cooldown
    - Tracks daily P/L and stops trading if max daily loss hit
    - Reduces position size during high volatility periods
    """

    def __init__(self):
        # Market-wide state (shared across users — same exchange data)
        self.crash_mode = False
        self.crash_triggered_at = None

        # Per-user throttle so every user independently runs a live crash
        # check and receives their own notification when crash is detected.
        self._user_last_crash_check = {}  # user_id -> datetime
        self.last_crash_check = None

        # Per-user daily state keyed by user_id
        self._user_daily_start_balance = {}
        self._user_daily_trade_count = {}
        self._user_daily_start_time = {}

    def reset_crash_mode(self):
        """Manually reset crash mode and cooldown (called on bot restart or manual override)"""
        self.crash_mode = False
        self.crash_triggered_at = None
        self._user_last_crash_check = {}
        self.last_crash_check = None
        print("[CRASH-PROTECTION] 🔄 Crash mode reset - trading can resume immediately")

    def _reset_daily_if_needed(self, user_id):
        """Reset daily counters at midnight for a specific user"""
        now = datetime.now()
        start_time = self._user_daily_start_time.get(user_id)
        if start_time is None or now.date() != start_time.date():
            self._user_daily_start_time[user_id] = now
            self._user_daily_trade_count[user_id] = 0
            self._user_daily_start_balance[user_id] = None
            print(f"[CRASH-PROTECTION] 📅 Daily counters reset for {user_id} at {now}")

    def set_daily_start_balance(self, user_id, balance):
        """Set the starting balance for the day for a specific user"""
        self._reset_daily_if_needed(user_id)
        if self._user_daily_start_balance.get(user_id) is None:
            self._user_daily_start_balance[user_id] = balance
            print(f"[CRASH-PROTECTION] 💰 Daily start balance set for {user_id}: ${balance:.2f}")

    def record_trade(self, user_id):
        """Record a trade for a specific user's daily trade counting"""
        self._reset_daily_if_needed(user_id)
        self._user_daily_trade_count[user_id] = self._user_daily_trade_count.get(user_id, 0) + 1
        count = self._user_daily_trade_count[user_id]
        
        # Show limit in log only if LIMITED mode
        if FUTURES_DAILY_TRADE_LIMIT_MODE == "LIMITED":
            print(f"[CRASH-PROTECTION] 📊 Daily trade count for {user_id}: {count}/{FUTURES_DAILY_MAX_TRADES}")
        else:
            print(f"[CRASH-PROTECTION] 📊 Daily trade count for {user_id}: {count} (mode: UNLIMITED)")

    # =====================================================
    # CRASH DETECTION
    # =====================================================

    def check_for_crash(self, client, threshold_override=None, upper_threshold_override=None):
        """
        Check if market is crashing by monitoring reference coin (BTC).
        
        Detects crashes when price change is below lower threshold OR above upper threshold.
        Safe zone: -3% to +3% (or configured bounds).
        
        Returns:
            dict: {
                'is_crashing': bool,
                'drop_pct': float,
                'current_price': float,
                'window_high': float,
                'reason': str
            }
        """
        try:
            # Per-user throttle (30s): every user independently runs a live
            # check so all 90 users receive their own crash notification.
            now = datetime.now()
            last_check = getattr(self, 'last_crash_check', None)
            if last_check and (now - last_check).total_seconds() < 30:
                return {'is_crashing': self.crash_mode, 'drop_pct': 0, 'reason': 'Throttled',
                        'event_type': 'none', 'current_price': 0, 'window_high': 0, 'window_low': 0}
            self.last_crash_check = now

            # Fetch candles for the reference symbol
            klines = client.futures_klines(
                symbol=CRASH_REFERENCE_SYMBOL,
                interval=CRASH_MONITORING_WINDOW,
                limit=CRASH_MONITORING_CANDLES
            )

            if not klines:
                return {'is_crashing': False, 'drop_pct': 0, 'reason': 'Insufficient data'}

            # Get highest and lowest prices in the monitoring window
            highs = [float(k[2]) for k in klines]  # kline[2] = high
            lows = [float(k[3]) for k in klines]   # kline[3] = low
            current_close = float(klines[-1][4])   # Latest close
            window_high = max(highs)
            window_low = min(lows)

            if window_high == 0 or window_low == 0:
                return {'is_crashing': False, 'drop_pct': 0, 'reason': 'Invalid prices'}

            # Use dual thresholds: detect crash if drop below lower OR spike above upper
            lower_threshold = threshold_override if threshold_override is not None else CRASH_LOWER_THRESHOLD_PCT
            upper_threshold = upper_threshold_override if upper_threshold_override is not None else CRASH_UPPER_THRESHOLD_PCT

            # Calculate percentage change from peak/bottom to current price
            drop_pct = ((current_close - window_high) / window_high) * 100
            pump_pct = ((current_close - window_low) / window_low) * 100

            is_crashing = False
            trigger_pct = 0.0
            event_type = "none"
            reason = ""

            if drop_pct <= lower_threshold:
                is_crashing = True
                trigger_pct = drop_pct
                event_type = "crash"
                reason = (
                    f"{CRASH_REFERENCE_SYMBOL} dropped {drop_pct:.1f}% "
                    f"(${window_high:.0f} -> ${current_close:.0f}) "
                    f"in last {CRASH_MONITORING_CANDLES}x{CRASH_MONITORING_WINDOW}"
                )
            elif pump_pct >= upper_threshold:
                is_crashing = True
                trigger_pct = pump_pct
                event_type = "pump"
                reason = (
                    f"{CRASH_REFERENCE_SYMBOL} pumped {pump_pct:.1f}% "
                    f"(${window_low:.0f} -> ${current_close:.0f}) "
                    f"in last {CRASH_MONITORING_CANDLES}x{CRASH_MONITORING_WINDOW}"
                )

            print(f"[CRASH-PROTECTION] Evaluated drop: {drop_pct:.2f}%, pump: {pump_pct:.2f}% | Safe Zone: {lower_threshold}% to {upper_threshold}%")

            result = {
                'is_crashing': is_crashing,
                'drop_pct': round(trigger_pct, 2),
                'current_price': current_close,
                'window_high': window_high,
                'window_low': window_low,
                'event_type': event_type,
                'reason': reason
            }

            if result['is_crashing']:
                print(f"[CRASH-PROTECTION] 🚨 MARKET EXTREME DETECTED: {result['reason']}")

                if not self.crash_mode:
                    self.crash_mode = True
                    self.crash_triggered_at = datetime.now()

            return result

        except Exception as e:
            print(f"[CRASH-PROTECTION] ❌ Error checking for crash: {e}")
            return {'is_crashing': False, 'drop_pct': 0, 'reason': f'Error: {e}'}

    # =====================================================
    # EMERGENCY CLOSE ALL POSITIONS
    # =====================================================

    def emergency_close_all(self, client, user_data, username, bot=None, telegram_id=None):
        """
        Close ALL open futures positions immediately.
        Called when crash is detected.
        
        Returns:
            list: Results of close attempts
        """
        if not CRASH_CLOSE_ALL_POSITIONS:
            print("[CRASH-PROTECTION] ⚠️ Emergency close disabled in config")
            return []

        print("[CRASH-PROTECTION] 🚨🚨🚨 EMERGENCY CLOSE ALL POSITIONS 🚨🚨🚨")
        results = []

        try:
            positions = client.futures_position_information()
            open_positions = [p for p in positions if float(p["positionAmt"]) != 0]

            if not open_positions:
                print("[CRASH-PROTECTION] ℹ️ No open positions to close")
                return results

            for pos in open_positions:
                symbol = pos["symbol"]
                position_amt = float(pos["positionAmt"])
                entry_price = float(pos["entryPrice"])
                side = "SELL" if position_amt > 0 else "BUY"  # Opposite side to close
                qty = abs(position_amt)

                try:
                    order = client.futures_create_order(
                        symbol=symbol,
                        side=side,
                        type="MARKET",
                        quantity=qty,
                    )

                    # Get close price
                    ticker = client.futures_symbol_ticker(symbol=symbol)
                    close_price = float(ticker['price'])

                    # Calculate P/L
                    if position_amt > 0:  # Was LONG
                        pnl_pct = ((close_price - entry_price) / entry_price) * 100
                    else:  # Was SHORT
                        pnl_pct = ((entry_price - close_price) / entry_price) * 100

                    result = {
                        'symbol': symbol,
                        'side': 'LONG' if position_amt > 0 else 'SHORT',
                        'entry': entry_price,
                        'exit': close_price,
                        'pnl_pct': pnl_pct,
                        'success': True
                    }
                    results.append(result)

                    # Update internal state
                    if username in user_data and symbol in user_data[username].get("trades", {}):
                        user_data[username]["trades"][symbol]["holding_position"] = False
                        user_data[username]["trades"][symbol]["trade_completed"] = True
                        if symbol in user_data[username].get("active_coins", []):
                            user_data[username]["active_coins"].remove(symbol)

                    print(f"[CRASH-PROTECTION] ✅ Closed {result['side']} {symbol} | P/L: {pnl_pct:.2f}%")

                except Exception as e:
                    results.append({
                        'symbol': symbol,
                        'success': False,
                        'error': str(e)
                    })
                    print(f"[CRASH-PROTECTION] ❌ Failed to close {symbol}: {e}")

            # Send Telegram notification
            if bot and telegram_id:
                try:
                    closed_text = "\n".join([
                        f"  {'🟢' if r.get('pnl_pct', 0) >= 0 else '🔴'} {r['symbol']} ({r.get('side', '?')}): {r.get('pnl_pct', 0):.2f}%"
                        for r in results if r['success']
                    ])
                    failed_text = "\n".join([
                        f"  ❌ {r['symbol']}: {r.get('error', 'unknown')}"
                        for r in results if not r['success']
                    ])

                    msg = (
                        f"<b>🚨 MARKET EXTREME — EMERGENCY CLOSE</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n\n"
                        f"📉 <b>Extreme movement detected in {CRASH_REFERENCE_SYMBOL}</b>\n"
                        f"🔒 <b>All positions closed to protect capital</b>\n\n"
                        f"<b>Closed Positions:</b>\n{closed_text or '  None'}\n"
                    )
                    if failed_text:
                        msg += f"\n<b>Failed:</b>\n{failed_text}\n"
                    msg += f"\n⏸️ <i>Trading paused for {CRASH_COOLDOWN_MINUTES // 60} hours</i>"

                    bot.send_message(chat_id=telegram_id, text=msg, parse_mode='HTML')
                except Exception as e:
                    print(f"[CRASH-PROTECTION] ⚠️ Failed to send crash notification: {e}")

        except Exception as e:
            print(f"[CRASH-PROTECTION] ❌ Error during emergency close: {e}")

        return results

    # =====================================================
    # CIRCUIT BREAKER - TRADING PAUSE
    # =====================================================

    def is_trading_allowed(self, user_id, current_balance=None):
        """
        Check if trading is currently allowed for a specific user.
        
        Blocks trading if:
        1. Crash mode is active and cooldown hasn't expired (market-wide)
        2. Daily max loss limit reached (per-user)
        3. Daily max trades reached (per-user)
        
        Returns:
            tuple: (allowed: bool, reason: str)
        """
        self._reset_daily_if_needed(user_id)

        # Check crash cooldown (market-wide)
        if self.crash_mode and CRASH_PAUSE_TRADING:
            if self.crash_triggered_at:
                elapsed = (datetime.now() - self.crash_triggered_at).total_seconds() / 60
                if elapsed < CRASH_COOLDOWN_MINUTES:
                    remaining = CRASH_COOLDOWN_MINUTES - elapsed
                    remaining_h = int(remaining // 60)
                    remaining_m = int(remaining % 60)
                    return False, f"Crash cooldown active ({remaining_h}h {remaining_m}m remaining)"
                else:
                    print(f"[CRASH-PROTECTION] ✅ Crash cooldown expired, resuming trading")
                    self.crash_mode = False
                    self.crash_triggered_at = None

        # Check daily trade limit (per-user) — only if FUTURES_DAILY_TRADE_LIMIT_MODE is "LIMITED"
        if FUTURES_DAILY_TRADE_LIMIT_MODE == "LIMITED":
            user_trade_count = self._user_daily_trade_count.get(user_id, 0)
            if user_trade_count >= FUTURES_DAILY_MAX_TRADES:
                return False, f"Daily trade limit reached ({user_trade_count}/{FUTURES_DAILY_MAX_TRADES})"

        return True, "Trading allowed"

    # =====================================================
    # VOLATILITY-BASED POSITION SIZING
    # =====================================================

    def get_adjusted_position_percentage(self, client):
        """
        Get position size percentage, reduced during high volatility.
        
        Measures volatility by looking at the range of recent BTC candles.
        If volatility is high, reduce position size to limit exposure.
        
        Returns:
            float: Adjusted wallet percentage for position sizing (e.g., 10 or 5)
        """
        try:
            klines = client.futures_klines(
                symbol=CRASH_REFERENCE_SYMBOL,
                interval="1h",
                limit=24  # Last 24 hours
            )

            if not klines or len(klines) < 12:
                return FUTURES_WALLET_PERCENTAGE  # Default if not enough data

            # Calculate average hourly volatility (high-low range as % of close)
            volatilities = []
            for k in klines:
                high = float(k[2])
                low = float(k[3])
                close = float(k[4])
                if close > 0:
                    vol = ((high - low) / close) * 100
                    volatilities.append(vol)

            avg_volatility = sum(volatilities) / len(volatilities) if volatilities else 0

            if avg_volatility >= HIGH_VOLATILITY_THRESHOLD:
                reduced = FUTURES_WALLET_PERCENTAGE * (1 - HIGH_VOLATILITY_SIZE_REDUCTION / 100)
                print(
                    f"[CRASH-PROTECTION] ⚠️ High volatility detected ({avg_volatility:.2f}%) "
                    f"— Position size reduced: {FUTURES_WALLET_PERCENTAGE}% -> {reduced:.1f}%"
                )
                return reduced
            else:
                return FUTURES_WALLET_PERCENTAGE

        except Exception as e:
            print(f"[CRASH-PROTECTION] ❌ Error checking volatility: {e}")
            return FUTURES_WALLET_PERCENTAGE

    def get_status_report(self, user_id, current_balance=None):
        """Get a formatted status report of crash protection state for a user"""
        self._reset_daily_if_needed(user_id)

        status = "🚨 CRASH MODE" if self.crash_mode else "✅ Normal"
        daily_loss = "N/A"
        user_start_balance = self._user_daily_start_balance.get(user_id)
        user_trade_count = self._user_daily_trade_count.get(user_id, 0)

        if current_balance and user_start_balance:
            loss_pct = ((user_start_balance - current_balance) / user_start_balance) * 100
            daily_loss = f"{loss_pct:.1f}%"

        start_bal_str = f"${user_start_balance:.2f}" if user_start_balance else "$0.00"
        
        # Show trade limit based on mode
        if FUTURES_DAILY_TRADE_LIMIT_MODE == "LIMITED":
            trades_str = f"{user_trade_count}/{FUTURES_DAILY_MAX_TRADES}"
        else:
            trades_str = f"{user_trade_count} (UNLIMITED)"

        return (
            f"<b>🛡️ Crash Protection Status</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Status:</b> {status}\n"
            f"📉 <b>Daily P/L:</b> {daily_loss}\n"
            f"🔢 <b>Trades Today:</b> {trades_str}\n"
            f"💰 <b>Start Balance:</b> {start_bal_str}\n"
        )


# Global singleton instance
crash_protector = CrashProtection()
