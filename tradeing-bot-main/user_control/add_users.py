import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# Initialize Firebase
cred = credentials.Certificate("firebase/test_dev_firebase.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

# Connect to Firestore
db = firestore.client()


def is_api_key_in_use(api_key):
    """
    Check if the given API key is already associated with any user.
    Returns (is_in_use, telegram_id) tuple.
    """
    users_ref = db.collection("users")
    users = users_ref.stream()
    
    for user in users:
        user_data = user.to_dict()
        if user_data.get("api_key") == api_key:
            return True, user_data.get("telegram_id", "Unknown")
    
    return False, None


def is_api_secret_in_use(api_secret):
    """
    Check if the given API secret is already associated with any user.
    Returns (is_in_use, telegram_id) tuple.
    """
    users_ref = db.collection("users")
    users = users_ref.stream()
    
    for user in users:
        user_data = user.to_dict()
        if user_data.get("api_secret") == api_secret:
            return True, user_data.get("telegram_id", "Unknown")
    
    return False, None


def is_mexc_api_key_in_use(api_key):
    """Check if a MEXC API key is already registered. Returns (in_use, telegram_id)."""
    for user in db.collection("users").stream():
        data = user.to_dict()
        if data.get("mexc_api_key") == api_key:
            return True, data.get("telegram_id", "Unknown")
    return False, None


def is_mexc_api_secret_in_use(api_secret):
    """Check if a MEXC API secret is already registered. Returns (in_use, telegram_id)."""
    for user in db.collection("users").stream():
        data = user.to_dict()
        if data.get("mexc_api_secret") == api_secret:
            return True, data.get("telegram_id", "Unknown")
    return False, None


def add_user_mexc(telegram_id, mexc_api_key, mexc_api_secret,
                  status="pending", language="en", name=None):
    """Save a MEXC-only user to the database."""
    from datetime import datetime

    user_data = {
        "telegram_id": int(telegram_id),
        "mexc_api_key": mexc_api_key,
        "mexc_api_secret": mexc_api_secret,
        "status": status,
        "language": language,
        "platform": "mexc",
        "registered_at": datetime.now().isoformat(),
        "approved_by": None,
        "approved_at": None,
    }
    if name:
        user_data["name"] = name

    db.collection("users").document(str(telegram_id)).set(user_data)
    print(f"MEXC user '{telegram_id}' ({name or 'No name'}) saved with status: {status}")


def add_user_all_platforms(telegram_id, api_key, api_secret,
                            mt5_login, mt5_password, mt5_server,
                            mexc_api_key, mexc_api_secret,
                            status="pending", language="en", name=None,
                            metaapi_account_id=None):
    """Save a user registered for ALL three platforms (Binance + MT5 + MEXC)."""
    from datetime import datetime
    from mt5.mt5_config import MT5_SERVER as DEFAULT_SERVER

    user_data = {
        "telegram_id": int(telegram_id),
        # Binance
        "api_key": api_key,
        "api_secret": api_secret,
        # MT5
        "mt5_login": int(mt5_login),
        "mt5_password": mt5_password,
        "mt5_server": mt5_server if mt5_server else DEFAULT_SERVER,
        # MEXC
        "mexc_api_key": mexc_api_key,
        "mexc_api_secret": mexc_api_secret,
        # Meta
        "status": status,
        "language": language,
        "platform": "all",
        "registered_at": datetime.now().isoformat(),
        "approved_by": None,
        "approved_at": None,
    }
    if name:
        user_data["name"] = name
    if metaapi_account_id:
        user_data["metaapi_account_id"] = metaapi_account_id

    db.collection("users").document(str(telegram_id)).set(user_data)
    print(f"ALL-platform user '{telegram_id}' ({name or 'No name'}) saved.")


def load_MEXC_credentials(telegram_id):
    """
    Load MEXC API credentials for a user by Telegram ID.
    Returns (mexc_api_key, mexc_api_secret, user_id, user_data) or (None, None, None, None).
    """
    try:
        if isinstance(telegram_id, str) and telegram_id.startswith("user_"):
            telegram_id = int(telegram_id.replace("user_", ""))
        user_data, user_id = get_user_by_telegram_id(telegram_id)
        if user_data and user_data.get("status") == "active":
            platform = user_data.get("platform", "")
            if platform in ("mexc", "all"):
                return (
                    user_data.get("mexc_api_key"),
                    user_data.get("mexc_api_secret"),
                    user_id,
                    user_data,
                )
        return None, None, None, None
    except Exception as e:
        print(f"Error loading MEXC credentials for {telegram_id}: {e}")
        return None, None, None, None


def are_api_credentials_in_use(api_key, api_secret):
    """
    Check if both API key and secret are in use (separately or together).
    Returns (key_in_use, secret_in_use, key_user_id, secret_user_id) tuple.
    """
    key_in_use, key_user_id = is_api_key_in_use(api_key)
    secret_in_use, secret_user_id = is_api_secret_in_use(api_secret)
    
    return key_in_use, secret_in_use, key_user_id, secret_user_id


def add_user(telegram_id, api_key, api_secret, status="pending", language="en", name=None, platform="binance"):
    """
    Save Binance user to database after validation.
    This function assumes API credentials have been validated.
    New users are pending by default, requiring admin approval.
    Uses telegram_id as document ID for direct lookups.
    """
    from datetime import datetime
    
    user_data = {
        "telegram_id": int(telegram_id),
        "api_key": api_key,
        "api_secret": api_secret,
        "status": status,  # "pending", "active", or "rejected"
        "language": language,  # "en" or "si"
        "platform": platform,  # "binance" or "mt5"
        "registered_at": datetime.now().isoformat(),
        "approved_by": None,
        "approved_at": None,
    }
    
    if name:
        user_data["name"] = name
    
    doc_ref = db.collection("users").document(str(telegram_id))
    doc_ref.set(user_data)
    print(f"User '{telegram_id}' ({name or 'No name'}) saved successfully with platform: {platform}, status: {status}")


def add_user_mt5(telegram_id, mt5_login, mt5_password, mt5_server=None, status="pending", language="en", name=None, metaapi_account_id=None):
    """
    Save MT5 user to database after validation.
    MT5 users have different credentials than Binance users.
    Uses telegram_id as document ID for direct lookups.
    metaapi_account_id is the MetaAPI cloud account ID for this user.
    """
    from datetime import datetime
    from mt5.mt5_config import MT5_SERVER as DEFAULT_SERVER
    
    user_data = {
        "telegram_id": int(telegram_id),
        "mt5_login": int(mt5_login),
        "mt5_password": mt5_password,
        "mt5_server": mt5_server if mt5_server else DEFAULT_SERVER,  # Use provided or default
        "status": status,  # "pending", "active", or "rejected"
        "language": language,  # "en" or "si"
        "platform": "mt5",
        "registered_at": datetime.now().isoformat(),
        "approved_by": None,
        "approved_at": None,
    }
    
    if name:
        user_data["name"] = name
    
    if metaapi_account_id:
        user_data["metaapi_account_id"] = metaapi_account_id
    
    doc_ref = db.collection("users").document(str(telegram_id))
    doc_ref.set(user_data)
    print(f"MT5 User '{telegram_id}' ({name or 'No name'}) saved successfully with login: {mt5_login}")


def update_user_metaapi_account_id(telegram_id, metaapi_account_id):
    """Update the metaapi_account_id for an existing user"""
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc_ref.update({"metaapi_account_id": metaapi_account_id})
        print(f"MetaAPI account ID updated for user {telegram_id}")
        return True
    except Exception as e:
        print(f"Error updating MetaAPI account ID: {e}")
        return False


def is_mt5_login_in_use(mt5_login):
    """
    Check if the given MT5 login is already associated with any user.
    Returns (is_in_use, telegram_id) tuple.
    """
    users_ref = db.collection("users")
    users = users_ref.stream()
    
    for user in users:
        user_data = user.to_dict()
        if user_data.get("mt5_login") == int(mt5_login):
            return True, user_data.get("telegram_id", "Unknown")
    
    return False, None


def view_users_from_db():
    """Display all users from the database"""
    users_ref = db.collection("users")
    users = users_ref.stream()

    user_details = ""
    for user in users:
        user_data = user.to_dict()
        telegram_id = user_data.get("telegram_id", "Unknown")
        platform = user_data.get("platform", "binance")
        status = user_data.get("status", "Unknown")
        language = user_data.get("language", "en")
        registered_at = user_data.get("registered_at", "Unknown")
        
        if platform == "binance":
            api_key = user_data.get("api_key", "No API Key")
            user_details += f"Telegram ID: {telegram_id}\nPlatform: Binance\nAPI Key: {api_key[:10]}...\nStatus: {status}\nLanguage: {language}\nRegistered: {registered_at}\n\n"
        else:
            mt5_login = user_data.get("mt5_login", "No Login")
            user_details += f"Telegram ID: {telegram_id}\nPlatform: MT5\nMT5 Login: {mt5_login}\nStatus: {status}\nLanguage: {language}\nRegistered: {registered_at}\n\n"

    return user_details


def delete_user(telegram_id):
    """Delete a user from the database"""
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc_ref.delete()
        print(f"User '{telegram_id}' deleted successfully.")
        return True
    except Exception as e:
        print(f"Error deleting user: {e}")
        return False


def load_API_KEYS(user_identifier):
    """
    Load API keys from database (legacy function for compatibility)
    Now redirects to telegram_id-based loading
    """
    # Extract telegram_id if it has "user_" prefix
    if isinstance(user_identifier, str) and user_identifier.startswith("user_"):
        telegram_id = int(user_identifier.replace("user_", ""))
        return load_API_KEYS_by_telegram_id(telegram_id)
    
    return None, None, None, None


def load_API_KEYS_by_telegram_id(telegram_id):
    """Load API keys for a specific telegram_id"""
    try:
        # Extract numeric telegram_id if it has "user_" prefix
        if isinstance(telegram_id, str) and telegram_id.startswith("user_"):
            telegram_id = int(telegram_id.replace("user_", ""))
        
        user_data, user_id = get_user_by_telegram_id(telegram_id)
        
        if user_data and user_data.get("status") == "active":
            return user_data.get("api_key"), user_data.get("api_secret"), user_id, user_data
        
        return None, None, None, None
    except Exception as e:
        print(f"Error loading API keys for telegram_id {telegram_id}: {e}")
        return None, None, None, None


def is_user_exists(telegram_id):
    """Check if a user exists in the database using telegram_id"""
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc = doc_ref.get()
        return doc.exists
    except Exception as e:
        print(f"Error checking if user exists: {e}")
        return False


def get_user_by_telegram_id(telegram_id):
    """Get user data by Telegram ID using direct document lookup"""
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc = doc_ref.get()
        
        if doc.exists:
            user_data = doc.to_dict()
            return user_data, str(telegram_id)
        else:
            return None, None
    except Exception as e:
        print(f"Error getting user by telegram_id: {e}")
        return None, None


def user_has_api_keys(telegram_id):
    """Check if user has Binance API keys stored and is approved"""
    user_data, user_id = get_user_by_telegram_id(telegram_id)

    if user_data:
        platform = user_data.get("platform", "binance")
        is_approved = user_data.get("status") == "active"

        if platform in ("binance", "all"):
            has_api_key = bool(user_data.get("api_key"))
            has_api_secret = bool(user_data.get("api_secret"))
            return has_api_key and has_api_secret and is_approved, user_data, user_id
        elif platform == "mt5":
            has_mt5_login = bool(user_data.get("mt5_login"))
            has_mt5_password = bool(user_data.get("mt5_password"))
            return has_mt5_login and has_mt5_password and is_approved, user_data, user_id
        elif platform == "mexc":
            has_key = bool(user_data.get("mexc_api_key"))
            has_secret = bool(user_data.get("mexc_api_secret"))
            return has_key and has_secret and is_approved, user_data, user_id

    return False, None, None


def get_user_platform(telegram_id):
    """Get the trading platform for a user (binance or mt5)"""
    user_data, _ = get_user_by_telegram_id(telegram_id)
    
    if user_data:
        return user_data.get("platform", "binance")
    return None


def user_has_mt5_credentials(telegram_id):
    """Check if user has MT5 credentials stored and is approved.
    Also checks for metaapi_account_id which is required for trading."""
    user_data, user_id = get_user_by_telegram_id(telegram_id)

    if user_data:
        has_mt5_login = bool(user_data.get("mt5_login"))
        has_mt5_password = bool(user_data.get("mt5_password"))
        has_metaapi = bool(user_data.get("metaapi_account_id"))
        is_approved = user_data.get("status") == "active"
        platform_ok = user_data.get("platform") in ("mt5", "all")

        return has_mt5_login and has_mt5_password and has_metaapi and is_approved and platform_ok, user_data, user_id

    return False, None, None


def load_MT5_credentials(telegram_id):
    """Load MT5 credentials for a specific telegram_id.
    Returns (mt5_login, mt5_password, mt5_server, user_id, user_data) tuple.
    user_data dict includes metaapi_account_id if present.
    """
    try:
        if isinstance(telegram_id, str) and telegram_id.startswith("user_"):
            telegram_id = int(telegram_id.replace("user_", ""))

        user_data, user_id = get_user_by_telegram_id(telegram_id)

        if user_data and user_data.get("status") == "active":
            platform = user_data.get("platform", "")
            if platform in ("mt5", "all"):
                return (
                    user_data.get("mt5_login"),
                    user_data.get("mt5_password"),
                    user_data.get("mt5_server"),
                    user_id,
                    user_data
                )

        return None, None, None, None, None
    except Exception as e:
        print(f"Error loading MT5 credentials for telegram_id {telegram_id}: {e}")
        return None, None, None, None, None


def get_user_status(telegram_id):
    """Get user status: pending, active, or rejected"""
    doc_ref = db.collection("users").document(str(telegram_id))
    doc = doc_ref.get()
    
    if doc.exists:
        return doc.to_dict().get("status", "pending")
    return None


def get_user_language(telegram_id):
    """Get user's preferred language"""
    doc_ref = db.collection("users").document(str(telegram_id))
    doc = doc_ref.get()
    
    if doc.exists:
        return doc.to_dict().get("language", "en")
    return "en"


def update_user_language(telegram_id, language):
    """Update user's language preference"""
    doc_ref = db.collection("users").document(str(telegram_id))
    doc_ref.update({"language": language})
    print(f"Language updated to {language} for user {telegram_id}")


def approve_user(telegram_id, approved_by_admin):
    """Approve a user and set status to active"""
    from datetime import datetime
    
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc_ref.update({
            "status": "active",
            "approved_by": approved_by_admin,
            "approved_at": datetime.now().isoformat()
        })
        
        user_data = doc_ref.get().to_dict()
        username = str(telegram_id)  # Use telegram_id as username
        return True, f"User {username} approved successfully", username
    except Exception as e:
        return False, f"Error approving user: {str(e)}", str(telegram_id)


def reject_user(telegram_id):
    """Reject a user registration"""
    try:
        doc_ref = db.collection("users").document(str(telegram_id))
        doc_ref.update({"status": "rejected"})
        
        username = str(telegram_id)  # Use telegram_id as username
        print(f"User {telegram_id} has been rejected.")
        return True, f"User {username} rejected successfully", username
    except Exception as e:
        return False, f"Error rejecting user: {str(e)}", str(telegram_id)


def get_user_by_api_credentials(api_key, api_secret):
    """Find user by their API credentials"""
    users_ref = db.collection("users")
    users = users_ref.stream()
    
    for user in users:
        user_data = user.to_dict()
        if (user_data.get("api_key") == api_key and 
            user_data.get("api_secret") == api_secret):
            return {
                "telegram_id": user_data.get("telegram_id"),
                "status": user_data.get("status"),
                "language": user_data.get("language")
            }
    return None


def update_user_telegram_id(old_identifier, telegram_id):
    """Update user with telegram_id (legacy support)"""
    # This function is mainly for migration purposes
    # In the new system, telegram_id is the document ID
    pass


def get_username_by_telegram_id(telegram_id):
    """Get username by telegram_id (for compatibility)"""
    # In new system, telegram_id IS the identifier
    return str(telegram_id)


def get_pending_users():
    """Get all users with pending status"""
    users_ref = db.collection("users")
    users = users_ref.where(filter=FieldFilter("status", "==", "pending")).stream()
    
    pending_list = []
    for user in users:
        user_data = user.to_dict()
        telegram_id = user_data.get("telegram_id")
        platform = user_data.get("platform", "binance")
        
        user_info = {
            "telegram_id": telegram_id,
            "name": user_data.get("name", "N/A"),
            "username": str(telegram_id),  # Use telegram_id as username for display
            "platform": platform,
            "registered_at": user_data.get("registered_at", ""),
            "language": user_data.get("language", "en")
        }
        
        # Add platform-specific credential info
        if platform == "binance":
            user_info["api_key"] = user_data.get("api_key", "")[:10] + "..."
        else:
            user_info["mt5_login"] = user_data.get("mt5_login", "N/A")
        
        pending_list.append(user_info)
    
    return pending_list


def set_user_language(telegram_id, language):
    """Set user language (alias for update_user_language)"""
    return update_user_language(telegram_id, language)


def get_user_telegram_id(username):
    """Legacy function - returns username as telegram_id"""
    # In new system, username IS the telegram_id
    return username


def set_user_telegram_id(username, telegram_id):
    """Legacy function - not needed in new system"""
    # In new system, telegram_id is the document ID
    pass


def get_user_by_username(username):
    """Legacy function - redirects to get_user_by_telegram_id"""
    return get_user_by_telegram_id(username)


def is_admin(telegram_id):
    """Check if a user is an admin (BOT_CREATOR)"""
    from config import BOT_CREATOR_ID
    return int(telegram_id) == int(BOT_CREATOR_ID)