import asyncio
import threading

# Global event loop for background tasks
loop = asyncio.new_event_loop()

def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def start_background_loop():
    """Start the global asyncio event loop in a separate thread"""
    t = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
    t.start()
    return loop
