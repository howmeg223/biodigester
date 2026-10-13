"""Save sensor readings and fault events in CSV (spreadsheet) files."""
import csv
from pathlib import Path


class DataLogger:
    """Create log files as needed and add one row for each new reading or event."""

    def __init__(self, reading_file, fault_file):
        self.reading_file = Path(reading_file)
        self.fault_file = Path(fault_file)

    def log_reading(self, timestamp, sensor, value):
        """Add a sensor reading, using a readable date and time."""
        self._ensure(self.reading_file, ["timestamp", "sensor", "value"])
        row = [timestamp.isoformat(timespec="seconds"), sensor, value]
        with self.reading_file.open("a", newline="", encoding="utf-8") as log_file:
            writer = csv.writer(log_file)
            writer.writerow(row)

    def log_fault_event(self, timestamp, event):
        """Add a fault change, such as RAISED, ACKNOWLEDGED or CLEARED."""
        self._ensure(self.fault_file,
                     ["timestamp", "event", "code", "category", "severity", "message", "action"])
        row = [
            timestamp.isoformat(timespec="seconds"),
            event.get("event", ""),
            event.get("code", ""),
            event.get("category", ""),
            event.get("severity", ""),
            event.get("message", ""),
            event.get("action", ""),
        ]
        with self.fault_file.open("a", newline="", encoding="utf-8") as log_file:
            writer = csv.writer(log_file)
            writer.writerow(row)

    @staticmethod
    def _ensure(path, header):
        # "a" appends rows. "w" creates the header for a new or empty file.
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8") as log_file:
                csv.writer(log_file).writerow(header)
