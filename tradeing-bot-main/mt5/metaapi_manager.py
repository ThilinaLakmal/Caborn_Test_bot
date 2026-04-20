"""
MetaAPI Connection Manager
Manages cloud-based MT5 connections for multiple users simultaneously.
Each user gets their own MetaAPI RPC connection, enabling parallel trading
without requiring a local MetaTrader 5 terminal.

Architecture:
    - One global MetaApi client (authenticated with admin token)
    - Per-user MetatraderAccount + RpcMetaApiConnection
    - Account provisioning during user registration
    - Historical candle data via MetaAPI REST endpoint (Cached globally)
    - REST-based balance and position checks for 1000+ user scalability
"""
import asyncio
import aiohttp
from typing import Any
from datetime import datetime
from metaapi_cloud_sdk import MetaApi

from config import (
    METAAPI_TOKEN, MT5_DEPLETION_THRESHOLD, MT5_RECOVERY_THRESHOLD, 
    MT5_DEPLETION_ALERT_COOLDOWN_HOURS
)

# =====================================================
# GLOBAL STATE
# =====================================================

_meta_api = None
_meta_api_lock = None          # created lazily

_user_connections = {}         # telegram_id -> RpcMetaApiConnection
_user_accounts = {}            # telegram_id -> MetatraderAccount
_last_connection_errors = {}      # telegram_id -> user-friendly string shown to user
_last_connection_raw_errors = {}  # telegram_id -> raw error string shown to admin
_admin_action_required = set()    # telegram_ids where admin must act (e.g. top-up billing)
_user_balance_state = {}       # telegram_id -> last_known_balance

_user_depletion_alert_sent = {}
_user_recovery_alert_sent = {}
_user_depletion_alert_timestamp = {}

_user_connection_locks = {}    # telegram_id -> asyncio.Lock
_ws_initialized = set()        # telegram_id -> bool
_global_connect_semaphore = None

_global_market_data_cache = {} # (symbol, timeframe) -> {"data": [...], "timestamp": datetime}
_global_market_data_locks = {}

_historical_candles_cache = {}
_historical_candles_locks = {}
_user_balance_cache = {}       # metaapi_account_id -> (balance, timestamp)

_shared_session = None

# =====================================================
# HELPERS
# =====================================================

def _get_user_lock(telegram_id):
    if telegram_id not in _user_connection_locks:
        _user_connection_locks[telegram_id] = asyncio.Lock()
    return _user_connection_locks[telegram_id]

async def _get_meta_api_lock():
    global _meta_api_lock
    if _meta_api_lock is None:
        _meta_api_lock = asyncio.Lock()
    return _meta_api_lock

async def _get_global_connect_semaphore():
    global _global_connect_semaphore
    if _global_connect_semaphore is None:
        _global_connect_semaphore = asyncio.Semaphore(30)
    return _global_connect_semaphore

def _get_market_data_lock(symbol, timeframe):
    key = (symbol, timeframe)
    if key not in _global_market_data_locks:
        _global_market_data_locks[key] = asyncio.Lock()
    return _global_market_data_locks[key]

async def get_meta_api():
    global _meta_api
    if _meta_api is None:
        lock = await _get_meta_api_lock()
        async with lock:
            if _meta_api is None:
                _meta_api = MetaApi(token=METAAPI_TOKEN)
                print("[METAAPI] ✅ MetaApi client initialized")
    return _meta_api

# =====================================================
# CONNECTION MANAGEMENT (Refactored for 1000 Users)
# =====================================================

async def get_user_connection(telegram_id, metaapi_account_id, bot=None, must_connect=False):
    """
    Get MetaAPI connection for a user.
    Lazy-connects the websocket only if must_connect=True (Trading Mode).
    Default (must_connect=False) returns the connection object for REST operations.
    """
    # 1. Warm Cache Path
    if telegram_id in _user_connections:
        conn = _user_connections[telegram_id]
        if not must_connect:
            return conn
        if telegram_id in _ws_initialized:
            return conn

    # 2. Cold / Reconnect Path (Locked per user)
    user_lock = _get_user_lock(telegram_id)
    async with user_lock:
        # Re-check after lock
        if telegram_id in _user_connections:
            conn = _user_connections[telegram_id]
            if not must_connect:
                return conn
            if telegram_id in _ws_initialized:
                return conn
            
            # Upgrade needed: destroy cold connection wrapper so it can be re-established properly
            try: await conn.close()
            except: pass
            _user_connections.pop(telegram_id, None)

        try:
            api = await get_meta_api()
            
            if telegram_id in _user_accounts:
                account = _user_accounts[telegram_id]
            else:
                account = await api.metatrader_account_api.get_account(metaapi_account_id)
                _user_accounts[telegram_id] = account

            if account.state != 'DEPLOYED':
                print(f"[METAAPI] 🔧 Deploying account {metaapi_account_id} for user {telegram_id}...")
                await account.deploy()
                await account.wait_deployed()

            # Ensure Broker Connectivity
            await account.reload()
            if account.connection_status != "CONNECTED":
                print(f"[METAAPI] ⏳ [{telegram_id}] Waiting for MT5 broker connection (state: {account.connection_status})...")
                for i in range(1, 4):
                    try:
                        print(f"[METAAPI] ⏳ [{telegram_id}] Broker wait attempt {i}/3...")
                        await asyncio.wait_for(account.wait_connected(), timeout=30)
                        print(f"[METAAPI] ✅ [{telegram_id}] MT5 Broker CONNECTED")
                        break
                    except Exception as e:
                        if i == 3:
                            print(f"[METAAPI] ❌ [{telegram_id}] MT5 Broker NOT connected after retries")
                            raise Exception(f"Broker connection failed: {e}")
                        await asyncio.sleep(10)

            # Phase 2: RPC Connection
            connection = account.get_rpc_connection()
            _user_connections[telegram_id] = connection

            # Phase 3: Websocket Handshake (Throttled globally)
            if must_connect:
                sem = await _get_global_connect_semaphore()
                async with sem:
                    print(f"[METAAPI] 🛡️ Global Throttle: Connecting WS for {telegram_id}...")
                    for i in range(3):
                        try:
                            # Use rate limiter if available
                            try:
                                from utils.rate_limiter import rate_limiter
                                await rate_limiter.metaapi_before_request()
                            except ImportError: pass
                            
                            await asyncio.wait_for(connection.connect(), timeout=30)
                            print(f"[METAAPI] ✅ Websocket connected for {telegram_id}")
                            _ws_initialized.add(telegram_id)
                            break
                        except Exception as ws_err:
                            print(f"[METAAPI] ⚠️ WS attempt {i+1} fail: {ws_err}")
                            if i < 2: await asyncio.sleep(5)

            # Initialize balance tracking (REST)
            if telegram_id not in _user_balance_state:
                _user_balance_state[telegram_id] = await get_user_balance_rest(telegram_id, metaapi_account_id)

            return connection

        except Exception as e:
            error_str = str(e).lower()
            raw_error = str(e)
            print(f"[METAAPI] ❌ Connection Failed for {telegram_id}: {e}")

            # Store raw error for admin
            _last_connection_raw_errors[telegram_id] = raw_error
            _admin_action_required.discard(telegram_id)  # reset first

            # Map user-friendly messages
            if "timeout" in error_str:
                _last_connection_errors[telegram_id] = "❌ Connection Timed Out\nPlease wait 1 minute and try again."
            elif "authentication" in error_str or "wrong password" in error_str:
                _last_connection_errors[telegram_id] = "❌ MT5 Connection Failed\nYour MT5 login or password is incorrect."
            elif "not found" in error_str:
                _last_connection_errors[telegram_id] = "❌ MT5 Connection Failed\nYour MT5 account was not found."
            elif "broker" in error_str:
                _last_connection_errors[telegram_id] = "❌ MT5 Broker Error\nMetaAPI cannot reach your broker. Check server name."
            elif "top up" in error_str:
                # MetaAPI billing issue — admin must top up MetaAPI subscription
                _last_connection_errors[telegram_id] = "⚠️ Service temporarily unavailable. Please contact admin."
                _admin_action_required.add(telegram_id)
            else:
                _last_connection_errors[telegram_id] = f"❌ MT5 Connection Failed\n\nDetail: {error_str}"

            return None

def get_last_connection_error(telegram_id):
    """User-friendly error string."""
    return _last_connection_errors.get(telegram_id)

def get_last_connection_raw_error(telegram_id):
    """Raw error string for admin logging."""
    return _last_connection_raw_errors.get(telegram_id)

def is_admin_action_required(telegram_id):
    """True if the error requires admin action (e.g. MetaAPI billing top-up)."""
    return telegram_id in _admin_action_required

async def is_user_connected(telegram_id):
    if telegram_id in _user_accounts:
        acc = _user_accounts[telegram_id]
        return acc.connection_status == 'CONNECTED'
    return False

async def disconnect_user(telegram_id):
    if telegram_id in _user_connections:
        try:
            conn = _user_connections[telegram_id]
            await conn.close()
        except: pass
        finally:
            _user_connections.pop(telegram_id, None)
            _ws_initialized.discard(telegram_id)
    
    _user_accounts.pop(telegram_id, None)
    _user_connection_locks.pop(telegram_id, None)
    _user_balance_state.pop(telegram_id, None)
    _last_connection_errors.pop(telegram_id, None)
    _last_connection_raw_errors.pop(telegram_id, None)
    _admin_action_required.discard(telegram_id)
    print(f"[METAAPI] ✅ Cleared resources for user {telegram_id}")

# =====================================================
# SCALABLE REST QUERIES (1000+ Users)
# =====================================================

async def get_current_price_shared(telegram_id, metaapi_account_id, symbol):
    """
    Get current bid/ask prices with a 5-second global cache.
    Reduces RPC/REST load by sharing market data across all 1000+ users.
    Tries market-data API first, then falls back to client API.
    """
    key = symbol
    lock = _get_market_data_lock(symbol, "price")
    
    async with lock:
        # Check cache
        if symbol in _global_market_data_cache:
            data, ts = _global_market_data_cache[symbol]
            if (datetime.now() - ts).total_seconds() < 5:
                return data["bid"], data["ask"]

        # Fetch fresh (REST) — try regional first, then global
        global _shared_session
        if _shared_session is None or _shared_session.closed:
            _shared_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            region = 'vint-hill'
            if telegram_id in _user_accounts:
                region = getattr(_user_accounts[telegram_id], 'region', 'vint-hill')

            urls = [
                (
                    f"https://mt-market-data-client-api-v1.{region}.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/symbols/{symbol}/current-price"
                ),
                (
                    f"https://mt-market-data-client-api-v1.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/symbols/{symbol}/current-price"
                ),
                (
                    f"https://mt-client-api-v1.{region}.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/symbols/{symbol}/current-price"
                ),
                (
                    f"https://mt-client-api-v1.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/symbols/{symbol}/current-price"
                ),
            ]
            headers = {'auth-token': METAAPI_TOKEN}

            for url in urls:
                try:
                    async with _shared_session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            tick = await resp.json()
                            bid = float(tick.get('bid'))
                            ask = float(tick.get('ask'))
                            _global_market_data_cache[symbol] = ({"bid": bid, "ask": ask}, datetime.now())
                            return bid, ask
                except Exception:
                    continue

            return None, None
        except Exception as e:
            print(f"[METAAPI] ⚠️ REST price exception for {symbol}: {e}")
            return None, None

async def get_symbol_info_shared(telegram_id, metaapi_account_id, symbol):
    """
    Get symbol details (point, digits, etc.) via REST — no WebSocket needed.
    Permanently cached since symbol specs never change.
    Tries market-data API first, then falls back to client API (some brokers
    only expose specs on one of the two).
    """
    cache_key = f"info_{symbol}"
    if cache_key in _global_market_data_cache:
        return _global_market_data_cache[cache_key][0]

    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    try:
        region = 'vint-hill'
        if telegram_id in _user_accounts:
            region = getattr(_user_accounts[telegram_id], 'region', 'vint-hill')

        urls = [
            (
                f"https://mt-market-data-client-api-v1.{region}.agiliumtrade.ai"
                f"/users/current/accounts/{metaapi_account_id}"
                f"/symbols/{symbol}/specification"
            ),
            (
                f"https://mt-market-data-client-api-v1.agiliumtrade.ai"
                f"/users/current/accounts/{metaapi_account_id}"
                f"/symbols/{symbol}/specification"
            ),
            (
                f"https://mt-client-api-v1.{region}.agiliumtrade.ai"
                f"/users/current/accounts/{metaapi_account_id}"
                f"/symbols/{symbol}/specification"
            ),
            (
                f"https://mt-client-api-v1.agiliumtrade.ai"
                f"/users/current/accounts/{metaapi_account_id}"
                f"/symbols/{symbol}/specification"
            ),
        ]
        headers = {'auth-token': METAAPI_TOKEN}

        got_404_from_market_data = False
        for url in urls:
            try:
                async with _shared_session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        processed = {
                            "symbol": symbol,
                            "digits": int(info.get('digits', 5)),
                            "point": float(info.get('point', 0.00001)),
                            "min_lot": float(info.get('minVolume', 0.01)),
                            "max_lot": float(info.get('maxVolume', 100.0)),
                            "lot_step": float(info.get('volumeStep', 0.01)),
                        }
                        _global_market_data_cache[cache_key] = (processed, datetime.now())
                        return processed
                    elif resp.status == 404:
                        got_404_from_market_data = True
            except Exception:
                continue

        return None
    except Exception as e:
        print(f"[METAAPI] ⚠️ REST symbol info error for {symbol}: {e}")
        return None

async def get_user_balance_rest(telegram_id, metaapi_account_id, ttl=30):
    """
    REST-based balance fetch. No websocket required.
    Robustly handles region-specific and global API endpoints.
    """
    now = datetime.now()
    # Cache Check (Normalize ID to string)
    tid_str = str(telegram_id)
    if metaapi_account_id in _user_balance_cache:
        val, ts = _user_balance_cache[metaapi_account_id]
        if (now - ts).total_seconds() < ttl:
            return val

    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    last_val = _user_balance_cache.get(metaapi_account_id, (0.0, None))[0]

    try:
        region = 'vint-hill'
        if telegram_id in _user_accounts:
            region = getattr(_user_accounts[telegram_id], 'region', 'vint-hill')

        # Try regional first, then global if it fails
        urls = [
            f"https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{metaapi_account_id}/account-information",
            f"https://mt-client-api-v1.agiliumtrade.ai/users/current/accounts/{metaapi_account_id}/account-information"
        ]
        
        headers = {'auth-token': METAAPI_TOKEN}

        for url in urls:
            try:
                async with _shared_session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        balance = float(info.get('balance', 0.0))
                        _user_balance_cache[metaapi_account_id] = (balance, now)
                        return balance
                    elif resp.status == 401:
                        print(f"[METAAPI] ❌ REST Balance 401 Unauthorized for {tid_str}. Check Token.")
                        break # No point retrying URLs for Auth error
            except Exception:
                continue

        # If all REST attempts failed, return last known or 0
        return last_val
    except Exception as e:
        print(f"[METAAPI] ⚠️ REST Balance exception for {tid_str}: {e}")
        return last_val

async def get_user_positions_rest(telegram_id, metaapi_account_id):
    """
    REST-based position fetch. No websocket required.
    Tries regional and global URLs.
    """
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    try:
        tid_str = str(telegram_id)
        region = 'vint-hill'
        if telegram_id in _user_accounts:
            region = getattr(_user_accounts[telegram_id], 'region', 'vint-hill')

        urls = [
            f"https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{metaapi_account_id}/positions",
            f"https://mt-client-api-v1.agiliumtrade.ai/users/current/accounts/{metaapi_account_id}/positions"
        ]
        headers = {'auth-token': METAAPI_TOKEN}

        for url in urls:
            try:
                async with _shared_session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception:
                continue
        # If all REST attempts failed, return None to signify failure (distinguish from empty list)
        return None
    except Exception as e:
        print(f"[METAAPI] ⚠️ REST Positions exception for {tid_str}: {e}")
        return None

# =====================================================
# SHARED MARKET DATA (GLOBAL CACHE)
# =====================================================

async def get_historical_candles_shared(telegram_id, metaapi_account_id, symbol, timeframe, count=100):
    """
    Globally shared historical candles via REST.
    Prevents redundant API calls by locking per symbol/timeframe.
    """
    key = (symbol, timeframe)
    if key not in _historical_candles_locks:
        _historical_candles_locks[key] = asyncio.Lock()
    
    lock = _historical_candles_locks[key]
    async with lock:
        cached = _historical_candles_cache.get(key, [])
        last_fetch = getattr(lock, 'last_fetch_time', datetime.min)
        
        # Valid for 10 seconds to slash load
        if cached and (datetime.now() - last_fetch).total_seconds() < 10:
            return cached[-count:] if len(cached) > count else cached

        global _shared_session
        if _shared_session is None or _shared_session.closed:
            _shared_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

        try:
            region = 'vint-hill'
            if telegram_id in _user_accounts:
                region = getattr(_user_accounts[telegram_id], 'region', 'vint-hill')

            urls = [
                (
                    f"https://mt-market-data-client-api-v1.{region}.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/historical-market-data/symbols/{symbol}"
                    f"/timeframes/{timeframe}/candles?limit={count}"
                ),
                (
                    f"https://mt-market-data-client-api-v1.agiliumtrade.ai"
                    f"/users/current/accounts/{metaapi_account_id}"
                    f"/historical-market-data/symbols/{symbol}"
                    f"/timeframes/{timeframe}/candles?limit={count}"
                ),
            ]
            headers = {'auth-token': METAAPI_TOKEN}

            for url in urls:
                try:
                    async with _shared_session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data:
                                _historical_candles_cache[key] = data
                                lock.last_fetch_time = datetime.now()
                                return data
                            return None
                        else:
                            text = await resp.text()
                            print(f"[METAAPI] ⚠️ REST candles failed for {symbol} (Status {resp.status}): {text[:100]}")
                except Exception:
                    continue

            return None
        except Exception as e:
            print(f"[METAAPI] ⚠️ REST candles exception for {symbol}: {e}")
            return None

# =====================================================
# PROVISIONING (Used for registration)
# =====================================================

async def provision_account(telegram_id, mt5_login, mt5_password, mt5_server, name=None, bot=None):
    """Standard account provisioning logic."""
    try:
        api = await get_meta_api()
        account_name = name or f'tgbot_user_{telegram_id}'
        
        # Check if already exists
        accounts = await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination({
            'type': 'cloud',
            'query': str(mt5_login)
        })
        
        account = None
        for acc in accounts:
            if str(acc.login) == str(mt5_login) and acc.server == mt5_server:
                account = acc
                break
        
        if not account:
            account = await api.metatrader_account_api.create_account({
                'name': account_name,
                'type': 'cloud',
                'login': str(mt5_login),
                'password': mt5_password,
                'server': mt5_server,
                'platform': 'mt5',
                'magic': 0,
            })

        if account.state != 'DEPLOYED':
            await account.deploy()
            await asyncio.wait_for(account.wait_deployed(), timeout=300)
            
        print(f"[METAAPI] ✅ Provisioned user {telegram_id}: {account.id}")
        return account.id
        
    except Exception as e:
        print(f"[METAAPI] ❌ Provisioning failed: {e}")
        return None
