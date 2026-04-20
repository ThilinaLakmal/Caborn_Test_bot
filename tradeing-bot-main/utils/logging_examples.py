"""
LOGGING SYSTEM USAGE GUIDE

This module demonstrates how to use the comprehensive logging system.
The logger automatically tracks all user activities and stores them in Firestore.
"""

from utils.logging_service import ActivityLogger

# ============================================================================
# EXAMPLES: HOW TO RETRIEVE LOGS FOR A USER
# ============================================================================

def get_user_logs_from_database(user_id):
    """Example: Get all logs for a specific user"""
    # Get last 100 logs
    all_logs = ActivityLogger.get_user_logs(user_id=user_id, limit=100)
    
    for log in all_logs:
        print(f"[{log['timestamp']}] {log['log_type']}: {log['message']}")
        if log.get('error_message'):
            print(f"  Error: {log['error_message']}")
    
    return all_logs


def get_user_error_logs(user_id):
    """Example: Get only error logs"""
    errors = ActivityLogger.get_error_logs(user_id=user_id, limit=50)
    
    for error in errors:
        print(f"[{error['timestamp']}] ERROR: {error['message']}")
        print(f"  Details: {error['error_message']}")
        print(f"  Context: {error['context']}")
    
    return errors


def get_user_trade_logs(user_id):
    """Example: Get all trade-related logs (OPEN/CLOSE)"""
    trades = ActivityLogger.get_trade_logs(user_id=user_id, limit=100)
    
    for trade in trades:
        context = trade.get('context', {})
        print(f"\n{trade['message']}")
        print(f"  Status Code: {trade['status_code']}")
        print(f"  Symbol: {context.get('symbol')}")
        print(f"  Entry: {context.get('entry_price')}")
        print(f"  P&L %: {context.get('profit_loss_pct')}")
    
    return trades


def get_api_errors_for_debugging(user_id):
    """Example: Get API errors for debugging failed trades"""
    api_errors = ActivityLogger.get_api_error_logs(user_id=user_id, limit=100)
    
    for error in api_errors:
        context = error.get('context', {})
        print(f"\nAPI Call: {context.get('api_name')} - {context.get('endpoint')}")
        print(f"  Status Code: {error['status_code']}")
        print(f"  Method: {context.get('method')}")
        print(f"  Error: {error['error_message']}")
        print(f"  Response Time: {context.get('response_time_ms')}ms")
    
    return api_errors


def get_logs_by_date_range(user_id, start_date, end_date):
    """Example: Get logs within a specific date range"""
    logs = ActivityLogger.get_user_logs_by_date_range(
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        log_type=None  # Can filter by type: LOG_TYPE_TRADE_OPEN, LOG_TYPE_ERROR, etc.
    )
    
    print(f"Found {len(logs)} logs between {start_date} and {end_date}")
    for log in logs:
        print(f"  {log['timestamp']}: {log['message']}")
    
    return logs


# ============================================================================
# DATABASE STRUCTURE - USER_ACTIVITY_LOGS COLLECTION
# ============================================================================
"""
Each log entry stored in Firestore has this structure:

{
    "user_id": "123456789",
    "timestamp": Timestamp("2026-03-17 15:30:45"),
    "log_type": "TRADE_OPEN" or "TRADE_CLOSE" or "REGISTRATION" or "ERROR" etc.,
    "status_code": 200 or 400 or 500 etc.,
    "message": "Human readable message",
    "error_message": "Detailed error description (if applicable)",
    "context": {
        # Additional context depending on log type
        # For TRADE_OPEN: symbol, entry_price, quantity, leverage, tp_price, sl_price, etc.
        # For TRADE_CLOSE: symbol, entry_price, exit_price, profit_loss, profit_loss_pct, etc.
        # For API_CALL: api_name, endpoint, method, response_time_ms, etc.
        # For ERROR: error_type, and any additional debugging info
    }
}

FIRESTORE QUERY EXAMPLES:
1. Get logs for specific user:
   db.collection("user_activity_logs").where("user_id", "==", "123456789")

2. Get errors for debugging:
   db.collection("user_activity_logs")
     .where("user_id", "==", "123456789")
     .where("log_type", "==", "ERROR")

3. Get trades opened in last 24 hours:
   db.collection("user_activity_logs")
     .where("user_id", "==", "123456789")
     .where("log_type", "==", "TRADE_OPEN")
     .where("timestamp", ">=", 24_hours_ago)

4. Get failed API calls:
   db.collection("user_activity_logs")
     .where("user_id", "==", "123456789")
     .where("log_type", "==", "API_CALL")
     .where("status_code", ">=", 400)
"""


# ============================================================================
# LOG TYPES AVAILABLE - Use these constants when logging
# ============================================================================
"""
ActivityLogger.LOG_TYPE_REGISTRATION        - User registration
ActivityLogger.LOG_TYPE_LOGIN              - User login
ActivityLogger.LOG_TYPE_TRADING            - General trading event
ActivityLogger.LOG_TYPE_TRADE_OPEN         - Trade opened
ActivityLogger.LOG_TYPE_TRADE_CLOSE        - Trade closed
ActivityLogger.LOG_TYPE_API_CALL           - External API call (Binance, MT5, MEXC)
ActivityLogger.LOG_TYPE_ERROR              - Error event
ActivityLogger.LOG_TYPE_SUCCESS            - Success event
ActivityLogger.LOG_TYPE_SETTINGS_UPDATE    - User settings changed
ActivityLogger.LOG_TYPE_USER_ACTION        - User action (button click, command, etc.)
ActivityLogger.LOG_TYPE_MARKET_ANALYSIS    - Market analysis result
ActivityLogger.LOG_TYPE_CRASH_DETECTION    - Crash detected
ActivityLogger.LOG_TYPE_POSITION_STATUS    - Current position status
"""


# ============================================================================
# STATUS CODES - HTTP-like codes with meanings
# ============================================================================
"""
ActivityLogger.STATUS_SUCCESS = 200         - Operation successful
ActivityLogger.STATUS_CREATED = 201         - Resource created
ActivityLogger.STATUS_BAD_REQUEST = 400     - Invalid request/parameters
ActivityLogger.STATUS_UNAUTHORIZED = 401    - Auth failed
ActivityLogger.STATUS_FORBIDDEN = 403       - No permission
ActivityLogger.STATUS_NOT_FOUND = 404       - Resource not found
ActivityLogger.STATUS_CONFLICT = 409        - Conflict (e.g., duplicate)
ActivityLogger.STATUS_ERROR = 500           - Internal server error
ActivityLogger.STATUS_API_ERROR = 502       - External API error
ActivityLogger.STATUS_TIMEOUT = 504         - Timeout
ActivityLogger.STATUS_UNKNOWN = 0           - Unknown error
"""


if __name__ == "__main__":
    # Example: Retrieve logs for a specific user
    user_id = 123456789  # Replace with actual user ID
    
    print("=" * 70)
    print("RETRIEVING LOGS FOR USER", user_id)
    print("=" * 70)
    
    # Get all logs
    print("\n📊 RECENT ACTIVITY (Last 20):")
    logs = ActivityLogger.get_user_logs(user_id=user_id, limit=20)
    for log in logs:
        print(f"  [{log['timestamp']}] {log['log_type']}: {log['message']}")
    
    # Get error logs
    print("\n⚠️ ERRORS (Last 10):")
    errors = ActivityLogger.get_error_logs(user_id=user_id, limit=10)
    for error in errors:
        print(f"  [{error['timestamp']}] {error['message']}")
        if error.get('error_message'):
            print(f"     Details: {error['error_message']}")
    
    # Get trade logs
    print("\n💰 TRADES:")
    trades = ActivityLogger.get_trade_logs(user_id=user_id, limit=10)
    for trade in trades:
        context = trade.get('context', {})
        print(f"  {trade['message']}")
        print(f"    Symbol: {context.get('symbol')}, Status: {trade['status_code']}")
    
    print("\n" + "=" * 70)
