# Deployment Guide - DEV vs PROD Mode

## 🎯 Overview

The bot now supports two modes:
- **DEV Mode** (Default): Uses Binance Testnet with fake money
- **PROD Mode**: Uses Live Binance with real money

## 🔧 How to Switch Modes

### Option 1: Edit config.py (Recommended)

Open `config.py` and change:

```python
# For TESTNET (fake money)
APP_MODE = "DEV"

# For LIVE trading (real money) 
APP_MODE = "PROD"
```

### Option 2: Environment Variable

Set environment variable before running:

**Windows PowerShell:**
```powershell
$env:APP_MODE="PROD"
uv run main.py
```

**Linux/Mac:**
```bash
export APP_MODE=PROD
python main.py
```

## 🟢 DEV Mode (Testnet)

### Setup:
1. Create account at: https://testnet.binancefuture.com
2. Generate testnet API keys
3. Add keys through bot registration
4. Set `APP_MODE = "DEV"` in config.py

### Features:
- ✅ Uses fake USDT (usually 10,000+ USDT)
- ✅ Safe for testing strategies
- ✅ No real money risk
- ✅ Same functionality as live
- ✅ Connects to: `testnet.binancefuture.com`

## 🔴 PROD Mode (Live Trading)

### Setup:
1. Create account at: https://www.binance.com
2. Generate LIVE API keys
3. **Enable Futures Trading** on your account
4. Fund your Futures wallet with USDT
5. Set `APP_MODE = "PROD"` in config.py

### Security Recommendations:
- ⚠️ **DISABLE WITHDRAWAL** permission on API keys
- ⚠️ **USE IP WHITELIST** to restrict API access
- ⚠️ Enable only "Enable Futures" permission
- ⚠️ Start with small amounts
- ⚠️ Monitor closely for first few trades

### Features:
- 🔴 Uses REAL money
- 🔴 Real profits and losses
- 🔴 Connects to: `api.binance.com`

## 📋 Client Factory

The bot now uses a centralized client factory (`trading/client_factory.py`):

```python
from trading.client_factory import get_binance_client, get_app_mode

# Get client (automatically uses correct mode)
client = get_binance_client(api_key, api_secret)

# Check current mode
current_mode = get_app_mode()  # Returns "DEV" or "PROD"
```

### Benefits:
- ✅ Single point of configuration
- ✅ Automatic mode detection
- ✅ Client caching for performance
- ✅ Easy to switch between DEV/PROD
- ✅ No hardcoded testnet=True everywhere

## 🚀 Startup Messages

When you run the bot, you'll see:

**DEV Mode:**
```
============================================================
🟢 BOT MODE: DEV - TESTNET (Fake Money) 🟢
============================================================
🤖 Bot started and polling...
```

**PROD Mode:**
```
============================================================
🔴 BOT MODE: PROD - LIVE TRADING (Real Money) 🔴
============================================================
🤖 Bot started and polling...
```

## ⚠️ IMPORTANT WARNINGS

1. **Always test in DEV mode first!**
2. **Never share your API keys**
3. **Start with small amounts in PROD**
4. **Monitor your first trades**
5. **Understand the risks of futures trading**

## 📊 Testing Checklist

Before switching to PROD:

- [ ] Test all trading signals in DEV mode
- [ ] Verify TP/SL work correctly
- [ ] Check position management
- [ ] Test stop trading function
- [ ] Monitor for at least 24 hours in DEV
- [ ] Understand your strategy performance
- [ ] Set appropriate risk limits
- [ ] Have emergency stop plan

## 🛠️ Troubleshooting

**Issue: "Unable to fetch balance"**
- Check API keys are correct
- Verify API keys have correct permissions
- Ensure you're in the right mode (DEV/PROD)
- Check if Futures wallet has funds (PROD mode)

**Issue: "Connection timeout"**
- Check internet connection
- Verify API keys match the mode
- Binance may be under maintenance

**Issue: "Client creation failed"**
- Ensure APP_MODE is "DEV" or "PROD" (case sensitive)
- Check client_factory.py is properly imported

## 📞 Support

If you encounter issues:
1. Check logs for error messages
2. Verify your API configuration
3. Test in DEV mode first
4. Contact admin via Telegram

---

**Remember: Trading involves risk. Never trade more than you can afford to lose!**
