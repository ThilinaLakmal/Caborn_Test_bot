"""
MEXC Futures Client Factory
Creates MEXC ccxt clients for futures (swap) trading.
CCXT is used instead of official SDK due to better compatibility and maintenance.
Implements rate limiting to prevent API exhaustion with 75+ concurrent users.
"""
import ccxt
from utils.rate_limiter import rate_limiter


def get_mexc_client(api_key: str, api_secret: str, cache_key: str = None) -> ccxt.mexc:
    """
    Create MEXC futures client using CCXT.
    Includes rate limiting to prevent hitting MEXC API limits.
    
    Args:
        api_key: MEXC API Key
        api_secret: MEXC API Secret  
        cache_key: Ignored (kept for backwards compatibility)

    Returns:
        ccxt.mexc: MEXC exchange instance configured for futures trading
    """
    # Apply rate limiting BEFORE creating client
    rate_limiter.mexc_before_request(api_key[:8])  # Use key prefix as user_id
    
    client = ccxt.mexc({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {
            "defaultType": "swap",  # Trade perpetual futures (swap contracts)
        },
        "enableRateLimit": True,
    })

    # Note: MEXC does not support sandbox mode in CCXT
    # Both dev and prod use the live API with real credentials

    return client
