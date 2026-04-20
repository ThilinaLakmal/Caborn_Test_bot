import math
from decimal import Decimal, ROUND_DOWN

def get_quantity_precision(symbol, client):
    """
    Get the quantity precision (step size) for a symbol.
    """
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        return step_size
        return None
    except Exception as e:
        print(f"Error getting quantity precision for {symbol}: {e}")
        return None

def round_step_size(quantity, step_size):
    """
    Floor quantity to the exchange step size so we never overshoot
    Binance's allowed precision or the live position size.
    """
    if step_size is None:
        return quantity

    quantity_decimal = Decimal(str(quantity))
    step_decimal = Decimal(str(step_size))

    if step_decimal <= 0:
        return float(quantity_decimal)

    steps = (quantity_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN)
    adjusted = steps * step_decimal
    return float(adjusted)

def adjust_quantity(symbol, quantity, client):
    """
    Adjust quantity to be valid for the symbol.
    """
    step_size = get_quantity_precision(symbol, client)
    if step_size:
        return round_step_size(quantity, step_size)
    return quantity


def get_price_precision(symbol, client):
    """Get the tick size (minimum price step) for a futures symbol."""
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        return float(f['tickSize'])
        return None
    except Exception as e:
        print(f"Error getting price precision for {symbol}: {e}")
        return None


def adjust_price(symbol, price, client):
    """Round a price to the symbol's tick size (floors to valid exchange precision)."""
    tick_size = get_price_precision(symbol, client)
    if tick_size:
        return round_step_size(price, tick_size)
    return round(price, 6)
