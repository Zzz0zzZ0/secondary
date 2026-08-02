import os
import signal
import threading

from .delivery_service import reap_expired_leases


stop_event = threading.Event()


def stop(_signum, _frame):
    stop_event.set()


def main():
    interval = int(os.getenv("OUTBOX_REAPER_INTERVAL_SECONDS", "30"))
    if interval < 5 or interval > 3600:
        raise RuntimeError("OUTBOX_REAPER_INTERVAL_SECONDS must be between 5 and 3600")
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stop_event.is_set():
        expired = reap_expired_leases()
        if expired:
            print(f"Marked {expired} expired delivery lease(s) as unknown", flush=True)
        stop_event.wait(interval)


if __name__ == "__main__":
    main()
