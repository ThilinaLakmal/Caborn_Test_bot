"""
Comprehensive Logging Service for User Activity Tracking
Stores all user activities, API calls, errors, and trading events in Firestore
"""
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import threading

# Initialize Firebase if not already initialized
cred = credentials.Certificate("firebase/test_dev_firebase.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Thread-safe operation
_log_lock = threading.Lock()


class ActivityLogger:
    """
    Logs user activities to Firestore in a separate 'user_activity_logs' collection.
    Stores: user_id, timestamp, log_type, status_code, message, context
    """

    # Log types
    LOG_TYPE_REGISTRATION = "REGISTRATION"
    LOG_TYPE_LOGIN = "LOGIN"
    LOG_TYPE_TRADING = "TRADING"
    LOG_TYPE_TRADE_OPEN = "TRADE_OPEN"
    LOG_TYPE_TRADE_CLOSE = "TRADE_CLOSE"
    LOG_TYPE_API_CALL = "API_CALL"
    LOG_TYPE_ERROR = "ERROR"
    LOG_TYPE_SUCCESS = "SUCCESS"
    LOG_TYPE_SETTINGS_UPDATE = "SETTINGS_UPDATE"
    LOG_TYPE_USER_ACTION = "USER_ACTION"
    LOG_TYPE_MARKET_ANALYSIS = "MARKET_ANALYSIS"
    LOG_TYPE_CRASH_DETECTION = "CRASH_DETECTION"
    LOG_TYPE_POSITION_STATUS = "POSITION_STATUS"

    # Status codes
    STATUS_SUCCESS = 200
    STATUS_CREATED = 201
    STATUS_BAD_REQUEST = 400
    STATUS_UNAUTHORIZED = 401
    STATUS_FORBIDDEN = 403
    STATUS_NOT_FOUND = 404
    STATUS_CONFLICT = 409
    STATUS_ERROR = 500
    STATUS_API_ERROR = 502
    STATUS_TIMEOUT = 504
    STATUS_UNKNOWN = 0

    @staticmethod
    def get_db():
        """Get Firestore database instance"""
        return db

    @staticmethod
    def _get_timestamp():
        """Get current timestamp in UTC"""
        return datetime.now(timezone.utc)

    @staticmethod
    def log(
        user_id: str,
        log_type: str,
        message: str,
        status_code: int = STATUS_SUCCESS,
        error_message: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Log an activity to Firestore
        
        Args:
            user_id: Telegram user ID
            log_type: Type of log (from LOG_TYPE_* constants)
            message: Main log message
            status_code: HTTP-like status code
            error_message: Detailed error message (if applicable)
            context: Additional context data (dict)
        
        Returns:
            True if logging successful, False otherwise
        """
        try:
            with _log_lock:
                log_entry = {
                    "user_id": str(user_id),
                    "timestamp": ActivityLogger._get_timestamp(),
                    "log_type": log_type,
                    "status_code": status_code,
                    "message": message,
                    "error_message": error_message or "",
                    "context": context or {},
                }
                
                # Add to user_activity_logs collection
                db.collection("user_activity_logs").add(log_entry)
                return True
        except Exception as e:
            print(f"[LOGGING ERROR] Failed to log activity for user {user_id}: {str(e)}")
            return False

    @staticmethod
    def log_registration(
        user_id: str,
        platform: str,
        status: str = "SUCCESS",
        error_msg: Optional[str] = None,
        api_keys_present: bool = False,
    ) -> bool:
        """Log user registration event"""
        context = {
            "platform": platform,
            "status": status,
            "api_keys_present": api_keys_present,
        }
        
        message = f"Registration for {platform} - {status}"
        status_code = (
            ActivityLogger.STATUS_SUCCESS if status == "SUCCESS" 
            else ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_REGISTRATION,
            message=message,
            status_code=status_code,
            error_message=error_msg,
            context=context,
        )

    @staticmethod
    def log_login(user_id: str, platform: str, status: str = "SUCCESS") -> bool:
        """Log user login event"""
        context = {"platform": platform}
        message = f"Login to {platform} - {status}"
        status_code = (
            ActivityLogger.STATUS_SUCCESS if status == "SUCCESS" 
            else ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_LOGIN,
            message=message,
            status_code=status_code,
            context=context,
        )

    @staticmethod
    def log_trade_open(
        user_id: str,
        exchange: str,
        symbol: str,
        entry_price: float,
        quantity: float,
        leverage: int,
        tp_price: float,
        sl_price: float,
        order_id: Optional[str] = None,
        error_msg: Optional[str] = None,
        status: str = "SUCCESS",
        api_status_code: Optional[int] = None,
    ) -> bool:
        """Log trade open event"""
        context = {
            "exchange": exchange,
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": quantity,
            "leverage": leverage,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "order_id": order_id or "",
            "api_status_code": api_status_code or 0,
        }
        
        message = f"Trade OPEN: {symbol} @ {entry_price} ({status})"
        status_code = (
            api_status_code or ActivityLogger.STATUS_SUCCESS 
            if status == "SUCCESS" 
            else api_status_code or ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_TRADE_OPEN,
            message=message,
            status_code=status_code,
            error_message=error_msg,
            context=context,
        )

    @staticmethod
    def log_trade_close(
        user_id: str,
        exchange: str,
        symbol: str,
        entry_price: float,
        exit_price: float,
        profit_loss: float,
        profit_loss_pct: float,
        order_id: Optional[str] = None,
        close_reason: str = "MANUAL",
        error_msg: Optional[str] = None,
        status: str = "SUCCESS",
        api_status_code: Optional[int] = None,
    ) -> bool:
        """Log trade close event"""
        context = {
            "exchange": exchange,
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "profit_loss": profit_loss,
            "profit_loss_pct": profit_loss_pct,
            "close_reason": close_reason,
            "order_id": order_id or "",
            "api_status_code": api_status_code or 0,
        }
        
        pnl_sign = "+" if profit_loss >= 0 else ""
        message = f"Trade CLOSE: {symbol} P&L {pnl_sign}{profit_loss_pct:.2f}% ({status})"
        status_code = (
            api_status_code or ActivityLogger.STATUS_SUCCESS 
            if status == "SUCCESS" 
            else api_status_code or ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_TRADE_CLOSE,
            message=message,
            status_code=status_code,
            error_message=error_msg,
            context=context,
        )

    @staticmethod
    def log_api_call(
        user_id: str,
        api_name: str,
        endpoint: str,
        method: str,
        status_code: int,
        response_time_ms: float = 0,
        error_msg: Optional[str] = None,
        request_data: Optional[Dict] = None,
    ) -> bool:
        """Log API call to external service (Binance, MT5, MEXC, etc.)"""
        context = {
            "api_name": api_name,
            "endpoint": endpoint,
            "method": method,
            "response_time_ms": response_time_ms,
            "request_data": request_data or {},
        }
        
        status_text = "SUCCESS" if 200 <= status_code < 300 else "FAILED"
        message = f"API Call {status_text}: {api_name} {method} {endpoint}"
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_API_CALL,
            message=message,
            status_code=status_code,
            error_message=error_msg,
            context=context,
        )

    @staticmethod
    def log_error(
        user_id: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None,
        status_code: int = STATUS_ERROR,
    ) -> bool:
        """Log an error event"""
        if context is None:
            context = {}
        context["error_type"] = error_type
        
        message = f"ERROR: {error_type}"
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_ERROR,
            message=message,
            status_code=status_code,
            error_message=error_message,
            context=context,
        )

    @staticmethod
    def log_settings_update(
        user_id: str,
        setting_name: str,
        old_value: Any,
        new_value: Any,
        status: str = "SUCCESS",
    ) -> bool:
        """Log user settings update"""
        context = {
            "setting_name": setting_name,
            "old_value": str(old_value),
            "new_value": str(new_value),
        }
        
        message = f"Settings Update: {setting_name}"
        status_code = (
            ActivityLogger.STATUS_SUCCESS if status == "SUCCESS" 
            else ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_SETTINGS_UPDATE,
            message=message,
            status_code=status_code,
            context=context,
        )

    @staticmethod
    def log_user_action(
        user_id: str,
        action: str,
        description: str,
        status: str = "SUCCESS",
    ) -> bool:
        """Log user action (button clicks, command executions, etc.)"""
        context = {"action": action}
        
        message = f"User {action}: {description}"
        status_code = (
            ActivityLogger.STATUS_SUCCESS if status == "SUCCESS" 
            else ActivityLogger.STATUS_ERROR
        )
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_USER_ACTION,
            message=message,
            status_code=status_code,
            context=context,
        )

    @staticmethod
    def log_market_analysis(
        user_id: str,
        exchange: str,
        symbol: str,
        signal_score: float,
        analysis_result: str,
        additional_data: Optional[Dict] = None,
    ) -> bool:
        """Log market analysis result"""
        context = {
            "exchange": exchange,
            "symbol": symbol,
            "signal_score": signal_score,
            "analysis_result": analysis_result,
            "additional_data": additional_data or {},
        }
        
        message = f"Market Analysis: {symbol} - Score: {signal_score:.2f}"
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_MARKET_ANALYSIS,
            message=message,
            status_code=ActivityLogger.STATUS_SUCCESS,
            context=context,
        )

    @staticmethod
    def log_crash_detection(
        user_id: str,
        crash_type: str,
        description: str,
        severity: str = "HIGH",
    ) -> bool:
        """Log crash detection event"""
        context = {
            "crash_type": crash_type,
            "severity": severity,
        }
        
        message = f"CRASH DETECTED: {crash_type}"
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_CRASH_DETECTION,
            message=message,
            status_code=ActivityLogger.STATUS_ERROR,
            error_message=description,
            context=context,
        )

    @staticmethod
    def log_position_status(
        user_id: str,
        exchange: str,
        symbol: str,
        position_data: Dict[str, Any],
    ) -> bool:
        """Log current position status"""
        message = f"Position Status: {symbol} on {exchange}"
        
        return ActivityLogger.log(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_POSITION_STATUS,
            message=message,
            status_code=ActivityLogger.STATUS_SUCCESS,
            context={
                "exchange": exchange,
                "symbol": symbol,
                "position_data": position_data,
            },
        )

    @staticmethod
    def get_user_logs(
        user_id: str,
        log_type: Optional[str] = None,
        limit: int = 100,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> list:
        """
        Retrieve logs for a specific user
        
        Args:
            user_id: Telegram user ID
            log_type: Filter by log type (optional)
            limit: Maximum number of logs to return
            date_from: Filter logs from this date (optional)
            date_to: Filter logs until this date (optional)
        
        Returns:
            List of log documents
        """
        try:
            query = db.collection("user_activity_logs").where("user_id", "==", str(user_id))
            
            if log_type:
                query = query.where("log_type", "==", log_type)
            
            if date_from:
                query = query.where("timestamp", ">=", date_from)
            
            if date_to:
                query = query.where("timestamp", "<=", date_to)
            
            # Order by timestamp descending and limit
            query = query.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
            
            docs = query.stream()
            logs = []
            for doc in docs:
                log_data = doc.to_dict()
                log_data["id"] = doc.id
                logs.append(log_data)
            
            return logs
        except Exception as e:
            print(f"[LOGGING ERROR] Failed to retrieve logs for user {user_id}: {str(e)}")
            return []

    @staticmethod
    def get_user_logs_by_date_range(
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        log_type: Optional[str] = None,
    ) -> list:
        """Get logs for a user within a date range"""
        return ActivityLogger.get_user_logs(
            user_id=user_id,
            log_type=log_type,
            limit=1000,
            date_from=start_date,
            date_to=end_date,
        )

    @staticmethod
    def get_error_logs(user_id: str, limit: int = 50) -> list:
        """Get only error logs for a user"""
        return ActivityLogger.get_user_logs(
            user_id=user_id,
            log_type=ActivityLogger.LOG_TYPE_ERROR,
            limit=limit,
        )

    @staticmethod
    def get_trade_logs(user_id: str, limit: int = 100) -> list:
        """Get all trade-related logs for a user"""
        try:
            query = db.collection("user_activity_logs").where("user_id", "==", str(user_id))
            # Filter for trade open/close logs
            query = query.where("log_type", "in", [
                ActivityLogger.LOG_TYPE_TRADE_OPEN,
                ActivityLogger.LOG_TYPE_TRADE_CLOSE,
            ])
            query = query.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
            
            docs = query.stream()
            logs = []
            for doc in docs:
                log_data = doc.to_dict()
                log_data["id"] = doc.id
                logs.append(log_data)
            
            return logs
        except Exception as e:
            print(f"[LOGGING ERROR] Failed to retrieve trade logs for user {user_id}: {str(e)}")
            return []

    @staticmethod
    def get_api_error_logs(user_id: str, limit: int = 100) -> list:
        """Get API call errors for debugging purposes"""
        try:
            query = db.collection("user_activity_logs").where("user_id", "==", str(user_id))
            query = query.where("log_type", "==", ActivityLogger.LOG_TYPE_API_CALL)
            # Filter for failed API calls (status code >= 400)
            query = query.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
            
            docs = query.stream()
            logs = []
            for doc in docs:
                log_data = doc.to_dict()
                if log_data.get("status_code", 0) >= 400:
                    log_data["id"] = doc.id
                    logs.append(log_data)
            
            return logs
        except Exception as e:
            print(f"[LOGGING ERROR] Failed to retrieve API error logs for user {user_id}: {str(e)}")
            return []


# Create a singleton instance for easy import
logger = ActivityLogger()
