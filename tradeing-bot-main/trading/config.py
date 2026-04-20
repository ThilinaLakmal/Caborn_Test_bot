"""
Trading configuration — re-exports from root config.py for backward compatibility.
All settings are now centralized in the root config.py file.
"""
from config import (
    TRADE_SYMBOL,
    TRADE_QUANTITY,
    LOOKBACK,
    TAKE_PROFIT_MULTIPLIER,
    STOP_LOSS_MULTIPLIER,
    MIN_GAP,
    TRADE_MODE_LIST as trade_mode_list,
    FUTURES_SYMBOLS_LIST as symbols_list,
    SPOT_SYMBOLS_LIST as symbols_list_spot,
)
