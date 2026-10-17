"""Save sensor readings and fault events in CSV (spreadsheet) files."""
import csv
import os
import tempfile
import threading
from pathlib import Path


class DataLogger:
    """Create log files as needed and add one row for each new reading or event."""

    def __init__(self, reading_file, fault_file):
        self.reading_file = Path(reading_file)
        self.fault_file = Path(fault_file)
        # Archive readers copy bytes under this lock, then release it before
        # parsing. It must never be the monitor's live-state lock.
        self.file_lock = threading.RLock()

    def log_reading(self, timestamp, sensor, value, source="sensor", sample_id="", excluded=False):
        """Preserve old CSV rows while adding analysis metadata to the schema."""
        header = ["timestamp", "sensor", "value", "source", "sample_id", "excluded"]
        row = [timestamp.isoformat(timespec="milliseconds"), sensor, value, source, sample_id, excluded]
        with self.file_lock:
            self._ensure_reading_schema(header)
            with self.reading_file.open("a", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerow(row)

    def _append(self, path, header, row):
        with self.file_lock:
            self._ensure(path, header)
            with path.open("a", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerow(row)

    def _ensure_reading_schema(self, header):
        self._ensure(self.reading_file, header)
        with self.reading_file.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames == header:
                return
            if not {"timestamp", "sensor", "value"}.issubset(reader.fieldnames or []):
                raise ValueError("Unknown readings CSV schema; refusing to replace it")
            with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                    dir=self.reading_file.parent, delete=False) as target:
                tmp = target.name
                writer = csv.DictWriter(target, header, extrasaction="ignore")
                writer.writeheader()
                try:
                    for row in reader:
                        writer.writerow({**row, "source": row.get("source") or "unknown",
                                         "sample_id": row.get("sample_id") or "",
                                         "excluded": row.get("excluded") or False})
                    target.flush()
                    os.fsync(target.fileno())
                except BaseException:
                    os.unlink(tmp)
                    raise
        os.replace(tmp, self.reading_file)

    def log_sampling_event(self, file, timestamp, event, reason, resume_at=""):
        path = Path(file)
        self._append(path, ["timestamp", "event", "reason", "resume_at"],
                     [timestamp.isoformat(timespec="milliseconds"), event, reason, resume_at])

    def log_coverage(self, file, timestamp, available):
        path = Path(file)
        self._append(path, ["timestamp", "available"],
                     [timestamp.isoformat(timespec="milliseconds"), available])

    def log_fault_event(self, timestamp, event):
        """Add a fault change, such as RAISED, ACKNOWLEDGED or CLEARED."""
        row = [
            timestamp.isoformat(timespec="seconds"),
            event.get("event", ""),
            event.get("code", ""),
            event.get("category", ""),
            event.get("severity", ""),
            event.get("message", ""),
            event.get("action", ""),
        ]
        self._append(self.fault_file,
                     ["timestamp", "event", "code", "category", "severity", "message", "action"], row)

    @staticmethod
    def _ensure(path, header):
        # "a" appends rows. "w" creates the header for a new or empty file.
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8") as log_file:
                csv.writer(log_file).writerow(header)

    def log_bubble_event(self, file, timestamp, event):
        path = Path(file)
        keys = ("session", "count", "raw", "baseline", "drop", "threshold", "hold_seconds")
        self._append(path, ["timestamp", *keys],
                     [timestamp.isoformat(timespec="milliseconds"),
                      *(event.get(key, "") for key in keys)])
