"""
Rate Limiting Module
Implements per-user and per-exchange rate throttling to prevent API limit exhaustion.

With 75 users, parallel requests hit limits quickly:
- MetaAPI: 1000 cpu credits/sec total (÷75 users = ~13 credits/sec per user)
- Binance: 1200 requests/min for futures (÷75 users = ~16 requests/min per user)
- MEXC: 1000 requests/min (÷75 users = ~13 requests/min per user)
"""

import time
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta


class RateLimiter:
    """
    Token bucket rate limiter - allows burst but maintains average rate.
    """
    def __init__(self, max_requests, time_window_seconds):
        """
        Args:
            max_requests: Maximum requests allowed in time_window_seconds
            time_window_seconds: Time window for rate calculation
        """
        self.max_requests = max_requests
        self.time_window_seconds = time_window_seconds
        self.timestamps = []
    
    def allow_request(self):
        """Check if request is allowed without blocking"""
        now = time.time()
        
        # Remove old timestamps outside the window
        self.timestamps = [ts for ts in self.timestamps if now - ts < self.time_window_seconds]
        
        # Check if we can make a request
        if len(self.timestamps) < self.max_requests:
            self.timestamps.append(now)
            return True
        return False
    
    def wait_if_needed(self):
        """Block until a request can be made"""
        while not self.allow_request():
            # Calculate wait time: time until oldest request falls out of window
            if self.timestamps:
                waittime = self.time_window_seconds - (time.time() - self.timestamps[0]) + 0.1
                if waittime > 0:
                    time.sleep(min(waittime, 1.0))  # Sleep max 1 second per check
            else:
                time.sleep(0.1)


class AsyncRateLimiter:
    """
    Async token bucket rate limiter for async/await code.
    """
    def __init__(self, max_requests, time_window_seconds):
        self.max_requests = max_requests
        self.time_window_seconds = time_window_seconds
        self.timestamps = []
        self.lock = asyncio.Lock()
    
    async def allow_request(self):
        """Check if request is allowed without blocking"""
        async with self.lock:
            now = time.time()
            
            # Remove old timestamps outside the window
            self.timestamps = [ts for ts in self.timestamps if now - ts < self.time_window_seconds]
            
            # Check if we can make a request
            if len(self.timestamps) < self.max_requests:
                self.timestamps.append(now)
                return True
            return False
    
    async def wait_if_needed(self):
        """Async sleep until a request can be made"""
        while not await self.allow_request():
            # Calculate wait time
            async with self.lock:
                if self.timestamps:
                    now = time.time()
                    waittime = self.time_window_seconds - (now - self.timestamps[0]) + 0.1
                    if waittime > 0:
                        await asyncio.sleep(min(waittime, 1.0))
                else:
                    await asyncio.sleep(0.1)


class ExchangeRateLimiter:
    """
    Per-exchange and per-user rate limiting to prevent API exhaustion.
    """
    
    def __init__(self):
        # Per-exchange rate limiters (shared across users)
        # MetaAPI: 1000 credits/sec per app (but we have 75 users, so ~13/sec per user)
        self.metaapi_limiter = AsyncRateLimiter(max_requests=1000, time_window_seconds=1)
        
        # Binance: 1200 requests/min per IP
        # With 75 concurrent users, use conservative limit: 1 request per 6 seconds per user
        self.binance_limiters = defaultdict(
            lambda: RateLimiter(max_requests=10, time_window_seconds=1)  # ~10 reqs/sec per user
        )
        
        # MEXC: 1000 requests/min per IP 
        # Conservative: 1 request per 6 seconds per user
        self.mexc_limiters = defaultdict(
            lambda: RateLimiter(max_requests=10, time_window_seconds=1)  # ~10 reqs/sec per user
        )
        
        # Track request counts for logging
        self.request_counts = defaultdict(lambda: defaultdict(int))
        self.last_log_time = datetime.now()
    
    # ─────────────────────────────────────────────────────────
    # METAAPI RATE LIMITING
    # ─────────────────────────────────────────────────────────
    
    async def metaapi_before_request(self):
        """Call before making a MetaAPI request"""
        await self.metaapi_limiter.wait_if_needed()
        self.request_counts['metaapi']['total'] += 1
    
    # ─────────────────────────────────────────────────────────
    # BINANCE RATE LIMITING
    # ─────────────────────────────────────────────────────────
    
    def binance_before_request(self, user_id):
        """Call before making a Binance API request"""
        limiter = self.binance_limiters[user_id]
        limiter.wait_if_needed()
        self.request_counts['binance'][user_id] += 1
    
    # ─────────────────────────────────────────────────────────
    # MEXC RATE LIMITING
    # ─────────────────────────────────────────────────────────
    
    def mexc_before_request(self, user_id):
        """Call before making an MEXC API request"""
        limiter = self.mexc_limiters[user_id]
        limiter.wait_if_needed()
        self.request_counts['mexc'][user_id] += 1
    
    # ─────────────────────────────────────────────────────────
    # LOGGING & STATS
    # ─────────────────────────────────────────────────────────
    
    def print_stats(self):
        """Print rate limiting stats for debugging"""
        now = datetime.now()
        elapsed = (now - self.last_log_time).total_seconds()
        
        if elapsed > 60:  # Print every 60 seconds
            print("\n[RATE-LIMIT] 📊 API Request Stats (last minute):")
            print(f"  MetaAPI: {self.request_counts['metaapi']['total']} requests")
            print(f"  Binance: {len(self.request_counts['binance'])} active users")
            print(f"  MEXC:    {len(self.request_counts['mexc'])} active users")
            
            self.last_log_time = now
            self.request_counts.clear()


# Global rate limiter instance
rate_limiter = ExchangeRateLimiter()


# ═════════════════════════════════════════════════════════════════
# DECORATORS FOR EASY INTEGRATION
# ═════════════════════════════════════════════════════════════════

def with_binance_ratelimit(func):
    """Decorator to add Binance rate limiting to a function"""
    def wrapper(user_api_key=None, *args, **kwargs):
        if user_api_key:
            rate_limiter.binance_before_request(user_api_key[:8])  # Use key prefix as user_id
        return func(*args, **kwargs)
    return wrapper


def with_mexc_ratelimit(func):
    """Decorator to add MEXC rate limiting to a function"""
    def wrapper(user_api_key=None, *args, **kwargs):
        if user_api_key:
            rate_limiter.mexc_before_request(user_api_key[:8])  # Use key prefix as user_id
        return func(*args, **kwargs)
    return wrapper
