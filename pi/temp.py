#!/usr/bin/env python3
"""Linux w1_therm DS18B20 publisher. Run as python -m pi.temp."""
import glob
import logging
import math
from pathlib import Path
import signal
import time

# Support both python -m pi.NAME and python pi/NAME.py.
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from mqtt_common import connect_and_start, make_client, send, stop_requested

log = logging.getLogger("temperature-hardware")


class TemperatureHardwareClient:
    def __init__(self):
        self.sensor_path = None
        self.client = make_client("biodigester-temperature-hardware")

    def find_sensor(self):
        sensors = sorted(glob.glob(config.TEMP_SENSOR_GLOB))
        if len(sensors) != 1:
            raise RuntimeError("Expected exactly one DS18B20; set TEMP_SENSOR_GLOB to its exact path if multiple are connected")
        self.sensor_path = Path(sensors[0])
        return self.sensor_path

    def read_temperature(self):
        if self.sensor_path is None:
            self.find_sensor()
        # Linux's temperature attribute performs a checked conversion; CRC errors
        # surface as read errors. Never republish the previous value on failure.
        raw = (Path(self.sensor_path) / "temperature").read_text().strip()
        value = float(raw) / 1000.0
        if not math.isfinite(value) or not -55 <= value <= 125 or value in (-127, 85):
            raise ValueError(f"Invalid/uninitialised DS18B20 temperature: {value}")
        return value

    def publish_temperature(self, value):
        send(self.client, config.TOPIC_TEMPERATURE, {"temperature": round(value, 3)},
             retain=config.TEMP_MQTT_RETAIN)

    def run(self):
        try:
            connect_and_start(self.client, "temperature-hardware")
            while True:
                try:
                    self.publish_temperature(self.read_temperature())
                except (OSError, RuntimeError, ValueError):
                    log.exception("Temperature unavailable; retrying")
                    self.sensor_path = None
                time.sleep(config.TEMP_PUBLISH_INTERVAL_S)
        finally:
            self.client.disconnect()
            self.client.loop_stop()


def main():
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, stop_requested)
    try:
        TemperatureHardwareClient().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
