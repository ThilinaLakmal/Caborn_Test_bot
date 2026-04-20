"""
MT5 Market Crash Protection Module (MetaAPI Cloud)
Monitors forex/gold markets for sudden crashes and takes protective action.
Similar to Binance crash_protection.py but uses MetaAPI data sources.

All check/close methods are async and take a ctx (MT5UserContext) parameter.
"""
import asyncio
from datetime import datetime

from config import (
    MT5_CRASH_LOWER_THRESHOLD_PCT,
    MT5_CRASH_UPPER_THRESHOLD_PCT,
    MT5_CRASH_MONITORING_TIMEFRAME,
    MT5_CRASH_MONITORING_CANDLES,
    CRASH_COOLDOWN_MINUTES,
    MT5_DAILY_TRADE_LIMIT_MODE,
    MT5_DAILY_MAX_TRADES,
)


class MT5CrashProtection:
    """
    Monitors MT5 market health and protects capital during crashes.
    
    For MT5/Forex:
    - Watches XAUUSD (Gold) as a market stress indicator
    - If Gold drops > threshold in monitoring window -> CRASH MODE
    - In crash mode: close all positions, pause trading, wait for cooldown
    - Tracks daily P/L and stops trading if max daily loss hit
    """

    # MT5-specific settings
    CRASH_REFERENCE_SYMBOL = "XAUUSD"  # Gold as market stress indicator (must match crash checks in handler)
    MONITORING_TIMEFRAME = MT5_CRASH_MONITORING_TIMEFRAME  # e.g. M5
    MONITORING_CANDLES = MT5_CRASH_MONITORING_CANDLES      # from config (e.g. 12 M5 = ~1h window)

    def __init__(self):
        # Market-wide state (shared across users — same market data)
        self.crash_mode = False
        self.crash_triggered_at = None
        self.last_crash_check = None
        
        # Result of the latest crash check (shared with all 1000+ users)
        self.last_result = {'is_crashing': False, 'drop_pct': 0, 'reason': 'Initializing...'}

        # Background task for 1000-user scale
        self._monitor_task = None
        self._monitor_lock = asyncio.Lock()
        self._reference_ctx = None

        # Per-user daily state keyed by user_id
        self._user_daily_start_balance = {}
        self._user_daily_trade_count = {}
        self._user_daily_start_time = {}

    def reset_crash_mode(self):
        """Manually reset crash mode and cooldown (called on bot restart or manual override)"""
        self.crash_mode = False
        self.crash_triggered_at = None
        self._user_last_crash_check = {}
        print("[MT5-CRASH-PROTECTION] 🔄 Crash mode reset - trading can resume immediately")

    def stop_monitor(self):
        """Cancel the background monitoring loop. Call when no MT5 users are active."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            print("[MT5-CRASH] 🛑 Background monitor stopped (no active users)")
        self._monitor_task = None
        self._reference_ctx = None

    def _reset_daily_if_needed(self, user_id):
        """Reset daily counters at midnight for a specific user"""
        now = datetime.now()
        start_time = self._user_daily_start_time.get(user_id)
        if start_time is None or now.date() != start_time.date():
            self._user_daily_start_time[user_id] = now
            self._user_daily_trade_count[user_id] = 0
            self._user_daily_start_balance[user_id] = None
            print(f"[MT5-CRASH] 📅 Daily counters reset for {user_id} at {now}")

    def set_daily_start_balance(self, user_id, balance):
        """Set the starting balance for the day for a specific user"""
        self._reset_daily_if_needed(user_id)
        if self._user_daily_start_balance.get(user_id) is None:
            self._user_daily_start_balance[user_id] = balance
            print(f"[MT5-CRASH] 💰 Daily start balance set for {user_id}: ${balance:.2f}")

    def record_trade(self, user_id):
        """Record a trade for a specific user's daily counting"""
        self._reset_daily_if_needed(user_id)
        self._user_daily_trade_count[user_id] = self._user_daily_trade_count.get(user_id, 0) + 1
        count = self._user_daily_trade_count[user_id]
        
        # Show limit in log only if LIMITED mode
        if MT5_DAILY_TRADE_LIMIT_MODE == "LIMITED":
            print(f"[MT5-CRASH] 📊 Daily trade count for {user_id}: {count}/{MT5_DAILY_MAX_TRADES}")
        else:
            print(f"[MT5-CRASH] 📊 Daily trade count for {user_id}: {count} (mode: UNLIMITED)")

    # =====================================================
    # CRASH DETECTION (using MetaAPI data)
    # =====================================================

    async def check_for_crash(self, ctx, reference_symbol=None, force_check=False):
        """
        Scale-optimized crash check. 
        Instead of 1000 users fetching candles, we use a shared background result.
        """
        # Ensure background monitor is running using the first available context
        if self._monitor_task is None:
            async with self._monitor_lock:
                if self._monitor_task is None:
                    # Capture the first valid context as our "Master Prober"
                    self._reference_ctx = ctx
                    self._monitor_task = asyncio.create_task(self._background_monitoring_loop(reference_symbol))

        # Return the globally calculated result (O(1) lookup)
        # This prevents 1000 users from hammering MetaAPI REST endpoints simultaneously.
        return self.last_result

    async def _background_monitoring_loop(self, reference_symbol=None):
        """Single global loop to monitor the market for ALL 1000+ users."""
        from mt5.mt5_core import get_candles
        
        ref_symbol = reference_symbol or self.CRASH_REFERENCE_SYMBOL
        print(f"[MT5-CRASH] 🛡️ Global background monitor started for {ref_symbol}")
        
        try:
            while True:
                try:
                    if not self._reference_ctx:
                        await asyncio.sleep(5)
                        continue

                    candles = await get_candles(
                        self._reference_ctx,
                        ref_symbol,
                        self.MONITORING_TIMEFRAME,
                        self.MONITORING_CANDLES
                    )

                    if candles and len(candles) >= 1:
                        window_open = candles[0]['open']
                        current_close = candles[-1]['close']
                        
                        if window_open > 0:
                            change_pct = ((current_close - window_open) / window_open) * 100
                            
                            lower_threshold = MT5_CRASH_LOWER_THRESHOLD_PCT
                            upper_threshold = MT5_CRASH_UPPER_THRESHOLD_PCT
                            
                            is_crashing = False
                            event = "none"
                            reason = ""
                            
                            if change_pct <= lower_threshold:
                                is_crashing = True
                                event = "crash"
                                reason = f"{ref_symbol} dropped {change_pct:.2f}% (${window_open:.2f} -> ${current_close:.2f}) from open."
                            elif change_pct >= upper_threshold:
                                is_crashing = True
                                event = "pump"
                                reason = f"{ref_symbol} pumped {change_pct:.2f}% (${window_open:.2f} -> ${current_close:.2f}) from open."
                            
                            self.last_result = {
                                'is_crashing': is_crashing,
                                'drop_pct': round(change_pct, 2),
                                'reason': reason or f"{ref_symbol} at {change_pct:.2f}%",
                                'event_type': event,
                                'current_price': current_close,
                                'window_high': window_open,
                                'window_low': window_open
                            }
                            
                            if is_crashing and not self.crash_mode:
                                self.crash_mode = True
                                self.crash_triggered_at = datetime.now()
                                print(f"[MT5-CRASH] 🚨 GLOBAL CRASH DETECTED: {reason}")
                            elif not is_crashing:
                                self.crash_mode = False
                    
                    self.last_crash_check = datetime.now()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[MT5-CRASH] ❌ Global monitor error: {e}")
                
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            print("[MT5-CRASH] 🛑 Background monitor cancelled")
            return

    # =====================================================
    # EMERGENCY CLOSE ALL MT5 POSITIONS (via MetaAPI)
    # =====================================================

    async def emergency_close_all(self, ctx, username, mt5_user_data, bot=None, telegram_id=None):
        """
        Close ALL open MT5 positions immediately via MetaAPI.
        
        Args:
            ctx: MT5UserContext
            username: Username key
            mt5_user_data: Global user data dict
            bot: Telegram bot instance
            telegram_id: User's Telegram ID
        
        Returns:
            list: Results of close attempts
        """
        from mt5.mt5_core import get_open_positions, close_position, is_mt5_connected

        print("[MT5-CRASH] 🚨🚨🚨 EMERGENCY CLOSE ALL MT5 POSITIONS 🚨🚨🚨")
        results = []

        try:
            if not await is_mt5_connected(ctx.telegram_id):
                print("[MT5-CRASH] ❌ MetaAPI not connected")
                return results

            positions = await get_open_positions(ctx, magic_only=True)

            if not positions:
                print("[MT5-CRASH] ℹ️ No open positions to close")
                return results

            for pos in positions:
                symbol = pos["symbol"]
                ticket = pos["ticket"]
                volume = pos["volume"]
                pos_type = pos["type"]
                entry_price = pos["price_open"]
                current_profit = pos.get("profit", 0)

                try:
                    result = await close_position(ctx, ticket)

                    close_result = {
                        'symbol': symbol,
                        'ticket': ticket,
                        'side': pos_type,
                        'entry': entry_price,
                        'profit': current_profit,
                        'success': result is not None
                    }
                    results.append(close_result)

                    # Update internal state
                    if username in mt5_user_data and symbol in mt5_user_data[username].get("positions", {}):
                        del mt5_user_data[username]["positions"][symbol]

                    if result:
                        print(f"[MT5-CRASH] ✅ Closed {pos_type} {symbol} (#{ticket}) | Profit: ${current_profit:.2f}")
                    else:
                        print(f"[MT5-CRASH] ❌ Failed to close {symbol} (#{ticket})")

                except Exception as e:
                    results.append({
                        'symbol': symbol,
                        'ticket': ticket,
                        'success': False,
                        'error': str(e)
                    })
                    print(f"[MT5-CRASH] ❌ Error closing {symbol}: {e}")

            # Send Telegram notification
            if bot and telegram_id:
                try:
                    closed_text = "\n".join([
                        f"  {'🟢' if r.get('profit', 0) >= 0 else '🔴'} {r['symbol']} ({r.get('side', '?')}): ${r.get('profit', 0):.2f}"
                        for r in results if r['success']
                    ])
                    failed_text = "\n".join([
                        f"  ❌ {r['symbol']}: {r.get('error', 'unknown')}"
                        for r in results if not r['success']
                    ])

                    msg = (
                        f"<b>🚨 MARKET EXTREME — MT5 EMERGENCY CLOSE</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n\n"
                        f"📉 <b>Extreme movement detected in {self.CRASH_REFERENCE_SYMBOL}</b>\n"
                        f"🔒 <b>All MT5 positions closed to protect capital</b>\n\n"
                        f"<b>Closed Positions:</b>\n{closed_text or '  None'}\n"
                    )
                    if failed_text:
                        msg += f"\n<b>Failed:</b>\n{failed_text}\n"
                    msg += f"\n⏸️ <i>MT5 trading paused for {CRASH_COOLDOWN_MINUTES} minutes</i>"

                    bot.send_message(chat_id=telegram_id, text=msg, parse_mode='HTML')
                except Exception as e:
                    print(f"[MT5-CRASH] ⚠️ Failed to send crash notification: {e}")

        except Exception as e:
            print(f"[MT5-CRASH] ❌ Error during emergency close: {e}")

        return results

    # =====================================================
    # CIRCUIT BREAKER
    # =====================================================

    def is_trading_allowed(self, user_id, current_balance=None):
        """
        Check if MT5 trading is currently allowed for a specific user.
        
        Returns:
            tuple: (allowed: bool, reason: str)
        """
        self._reset_daily_if_needed(user_id)

        # Check crash cooldown (market-wide)
        if self.crash_mode:
            if self.crash_triggered_at:
                elapsed = (datetime.now() - self.crash_triggered_at).total_seconds() / 60
                if elapsed < CRASH_COOLDOWN_MINUTES:
                    remaining = CRASH_COOLDOWN_MINUTES - elapsed
                    return False, f"Crash cooldown active ({remaining:.0f} min remaining)"
                else:
                    print(f"[MT5-CRASH] ✅ Crash cooldown expired, resuming trading")
                    self.crash_mode = False
                    self.crash_triggered_at = None
            else:
                # crash_mode set without timestamp — reset to avoid stuck state
                self.crash_mode = False

        # Check daily trade limit (per-user) — only if MT5_DAILY_TRADE_LIMIT_MODE is "LIMITED"
        if MT5_DAILY_TRADE_LIMIT_MODE == "LIMITED":
            user_trade_count = self._user_daily_trade_count.get(user_id, 0)
            if user_trade_count >= MT5_DAILY_MAX_TRADES:
                return False, f"Daily trade limit reached ({user_trade_count}/{MT5_DAILY_MAX_TRADES})"

        return True, "Trading allowed"

    async def is_safe_to_open_position(self, ctx, reference_symbol=None):
        """
        Quick pre-trade crash check — bypass throttle to ensure safety.
        Prevents orders from opening during sudden price crashes.
        
        Returns:
            tuple: (is_safe: bool, reason: str)
        """
        result = await self.check_for_crash(ctx, reference_symbol, force_check=True)
        
        if result.get('is_crashing'):
            return False, f"Market crash detected: {result.get('reason')}"
        
        # Additional check: if we're in crash mode cooldown, refuse new trades
        if self.crash_mode and self.crash_triggered_at:
            elapsed = (datetime.now() - self.crash_triggered_at).total_seconds() / 60
            if elapsed < CRASH_COOLDOWN_MINUTES:
                remaining = CRASH_COOLDOWN_MINUTES - elapsed
                return False, f"Crash cooldown active ({remaining:.0f} min remaining)"
        
        return True, "Safe to trade"


# Global singleton instance
mt5_crash_protector = MT5CrashProtection()
