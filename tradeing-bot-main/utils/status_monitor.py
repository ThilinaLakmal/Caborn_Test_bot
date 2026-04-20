"""
Bot status monitoring and reporting system
Sends periodic status updates to admin
"""
import threading
import time
from datetime import datetime
from config import BOT_CREATOR_ID, STATUS_CHECK_INTERVAL_MINUTES, BOT_NAME
from user_control.add_users import get_pending_users, db


def get_bot_statistics():
    """Gather bot statistics from database"""
    try:
        users_ref = db.collection("users")
        all_users = list(users_ref.stream())
        
        total_users = len(all_users)
        active_users = sum(1 for user in all_users if user.to_dict().get("status") == "active")
        pending_users = sum(1 for user in all_users if user.to_dict().get("status") == "pending")
        rejected_users = sum(1 for user in all_users if user.to_dict().get("status") == "rejected")
        
        return {
            "total": total_users,
            "active": active_users,
            "pending": pending_users,
            "rejected": rejected_users
        }
    except Exception as e:
        return {
            "error": str(e)
        }


def generate_status_message(stats):
    """Generate formatted status message"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Format interval display
    if STATUS_CHECK_INTERVAL_MINUTES >= 60:
        interval_display = f"{STATUS_CHECK_INTERVAL_MINUTES // 60} hour(s)"
    else:
        interval_display = f"{STATUS_CHECK_INTERVAL_MINUTES} minute(s)"
    
    if "error" in stats:
        message = f"""
🔴 <b>BOT STATUS REPORT</b>

⏰ <b>Time:</b> {current_time}
🤖 <b>Bot:</b> {BOT_NAME}
━━━━━━━━━━━━━━━━━━━━━━

❌ <b>Status:</b> Error
⚠️ <b>Error:</b> {stats['error']}
"""
    else:
        message = f"""
✅ <b>BOT STATUS REPORT</b>

⏰ <b>Time:</b> {current_time}
🤖 <b>Bot:</b> {BOT_NAME}
━━━━━━━━━━━━━━━━━━━━━━

📊 <b>Status:</b> Running ✅
👥 <b>Total Users:</b> {stats['total']}
✅ <b>Active:</b> {stats['active']}
⏳ <b>Pending:</b> {stats['pending']}
❌ <b>Rejected:</b> {stats['rejected']}

━━━━━━━━━━━━━━━━━━━━━━
🔄 Next report in {interval_display}
"""
    
    return message


def send_status_report(bot):
    """Send status report to admin"""
    try:
        stats = get_bot_statistics()
        message = generate_status_message(stats)
        bot.send_message(BOT_CREATOR_ID, message, parse_mode='HTML')
    except Exception as e:
        print(f"Error sending status report: {e}")


def status_monitor_thread(bot):
    """Background thread for status monitoring"""
    # Wait 5 seconds before first status check (let bot initialize)
    time.sleep(5)
    
    # Send initial status
    send_status_report(bot)
    
    # Convert minutes to seconds
    interval_seconds = STATUS_CHECK_INTERVAL_MINUTES * 60
    
    while True:
        time.sleep(interval_seconds)
        send_status_report(bot)


def start_status_monitor(bot):
    """Start the status monitoring system"""
    monitor = threading.Thread(target=status_monitor_thread, args=(bot,), daemon=True)
    monitor.start()
    print(f"✅ Status monitor started - Reports every {STATUS_CHECK_INTERVAL_MINUTES} minute(s)")
