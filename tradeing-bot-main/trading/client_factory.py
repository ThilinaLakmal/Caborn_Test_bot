"""
Centralized Binance Client Factory
Creates fresh Binance clients without caching to ensure multi-user concurrency safety.
Implements rate limiting to prevent API exhaustion with 75+ concurrent users.
"""
from binance.client import Client
from config import APP_MODE as _INITIAL_MODE
from utils.rate_limiter import rate_limiter

APP_MODE = _INITIAL_MODE


def get_binance_client(api_key: str, api_secret: str, cache_key: str = None) -> Client:
    """
    Create fresh Binance client based on APP_MODE.
    No caching - each call creates new client for concurrency safety.
    Includes rate limiting to prevent hitting Binance API limits.
    
    Args:
        api_key: Binance API key
        api_secret: Binance API secret
        cache_key: Ignored (kept for backwards compatibility)
    
    Returns:
        Client: Fresh Binance Client instance with rate limiting
    """
    # Apply rate limiting BEFORE creating client
    rate_limiter.binance_before_request(api_key[:8])  # Use key prefix as user_id
    
    if APP_MODE == "PROD":
        return Client(api_key, api_secret)
    else:
        return Client(api_key, api_secret, testnet=True)


def get_app_mode():
    """Get current application mode"""
    return APP_MODE


def set_app_mode(mode: str):
    """Set application mode (DEV or PROD)"""
    global APP_MODE
    if mode not in ["DEV", "PROD"]:
        raise ValueError("Mode must be 'DEV' or 'PROD'")
    APP_MODE = mode
