"""Keep recent fault events and print new faults in the terminal.

Today notifications are visible in the dashboard and terminal. This class is the
extension point for email/SMS/push integrations later, without moving detection
out of the controller.
"""
from collections import deque


class NotificationManager:
    """Store a limited history, with the newest event first."""

    def __init__(self, max_events=50):
        # A deque is a list-like collection. When full, it drops the oldest event.
        self._events = deque(maxlen=max_events)

    def process(self, event, timestamp):
        """Save an event without changing the original dictionary."""
        saved_event = dict(event)
        saved_event["received_at"] = timestamp
        self._events.appendleft(saved_event)

        # Clearing or acknowledging a fault is recorded without printing an alert.
        if event.get("event") in ("RAISED", "REASSERTED"):
            severity = event.get("severity", "FAULT")
            code = event.get("code")
            message = event.get("message", "")
            action = event.get("action", "")
            print(f"[monitor] {severity} {code}: {message} Action: {action}")

    def recent(self):
        """Return a list so the dashboard can select the events to display."""
        return list(self._events)
