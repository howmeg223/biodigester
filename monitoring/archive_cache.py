"""Share parsed archives between browser requests without locking live state."""
from pathlib import Path
import threading
import time


class ArchiveCache:
    def __init__(self, refresh_seconds=10, clock=time.monotonic):
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}

    def read(self, path, columns, loader, refresh=False):
        key = (Path(path).resolve(), tuple(columns))
        # Only one request parses a changed archive. This lock is deliberately
        # unrelated to MonitoringService.lock and is never acquired by MQTT.
        with self.lock:
            now = self.clock()
            entry = self.entries.get(key)
            if entry and not refresh and now - entry[0] < self.refresh_seconds:
                return entry[2]
            try:
                stat = key[0].stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                signature = None
            if entry and signature == entry[1]:
                self.entries[key] = (now, signature, entry[2])
                return entry[2]
            data = loader(key[0], columns)
            self.entries[key] = (now, signature, data)
            return data
