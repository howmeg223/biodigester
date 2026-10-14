#!/usr/bin/env python3

import argparse
import json
import logging
import math
import signal
import uuid
import time

# Support both python -m pi.NAME and python pi/NAME.py.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from pi.bubble_detection import BubbleDetector
from mqtt_common import connect_and_start, make_client, send, stop_requested

log = logging.getLogger("pressure-hardware")


class PressureHardwareClient:
    def __init__(self, mode=None):
        self.client = make_client("biodigester-pressure-hardware")
        self.gpio = None
        self.mode = mode or config.PRESSURE_MODE
        if self.mode not in ("pressure", "bubble", "both"):
            raise ValueError("Invalid pressure mode")
        self.detector = BubbleDetector()
        self.session = uuid.uuid4().hex
        self.latest_status = {"online": True, "ready": False, "mode": self.mode,
                              "calibrated": self.calibration_enabled()}
        self.client.will_set(config.TOPIC_PRESSURE_STATUS,
            json.dumps({"online": False}), qos=1, retain=True)
        self.client.on_connect = self._on_connect

    @staticmethod
    def _lgpio():
        try:
            import lgpio
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "lgpio is required on the Raspberry Pi running pressure.py. "
                "Install the Raspberry Pi lgpio package before starting this client."
            ) from exc
        return lgpio

    def setup_gpio(self):
        log.info("Opening /dev/gpiochip%d", config.PRESSURE_GPIO_CHIP)
        self.gpio = self._lgpio().gpiochip_open(config.PRESSURE_GPIO_CHIP)
        self._lgpio().gpio_claim_output(self.gpio, config.PRESSURE_SCK_PIN, 0)
        self._lgpio().gpio_claim_input(self.gpio, config.PRESSURE_OUT_PIN)
        self._lgpio().gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)
        log.info(
            "HX710B GPIO ready: SCK=GPIO%d OUT=GPIO%d",
            config.PRESSURE_SCK_PIN,
            config.PRESSURE_OUT_PIN,
        )

    def wait_ready(self):
        deadline = time.monotonic() + config.PRESSURE_READ_TIMEOUT
        while self._lgpio().gpio_read(self.gpio, config.PRESSURE_OUT_PIN):
            if time.monotonic() >= deadline:
                raise TimeoutError("HX710B did not become ready")
            time.sleep(0.00025)

    def clock_high(self):
        self._lgpio().gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 1)

    def clock_low(self):
        self._lgpio().gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)

    def read_raw(self):
        """
        Read one 24-bit signed HX710B conversion.

        24 clocks:
            retrieve ADC value

        +3 additional clocks:
            total = 27 clocks

        27 clocks tells HX710B:
            next conversion = differential pressure input
            data rate = 40 Hz
        """
        self.clock_low()
        self.wait_ready()
        value = 0
        # Sample DOUT after lowering SCK, following the datasheet's reference
        # routine. This keeps the high pulse to two GPIO writes. A Linux
        # scheduling delay must never silently become a corrupted ADC sample.
        for _ in range(24):
            value = value << 1 | self._pulse(read_bit=True)
        for _ in range(config.PRESSURE_EXTRA_CLOCKS):
            self._pulse(read_bit=False)
        # Interpret the ADC's 24-bit two's-complement value.
        if value & 0x800000:
            value -= 1 << 24
        return value

    def _pulse(self, read_bit):
        lgpio = self._lgpio()
        start = time.perf_counter_ns()
        lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 1)
        try:
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)
        finally:
            # Measure conservatively across both GPIO calls.
            elapsed = time.perf_counter_ns() - start
        if elapsed > 50000:  # datasheet maximum SCK high time = 50 microseconds
            # Force a known reset rather than continue a partial transfer.
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 1)
            time.sleep(0.0001)
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)
            raise OSError("HX710B clock delayed by scheduling; conversion discarded and ADC reset")
        return lgpio.gpio_read(self.gpio, config.PRESSURE_OUT_PIN) if read_bit else 0

    def calibration_enabled(self):
        values = (config.PRESSURE_ZERO_RAW, config.PRESSURE_CAL_RAW, config.PRESSURE_CAL_KPA)
        return (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in values) and values[0] != values[1] and values[2] > 0)

    def raw_to_kpa(self, raw):
        if not self.calibration_enabled():
            raise ValueError("Pressure calibration is incomplete or invalid")
        scale = config.PRESSURE_CAL_KPA / (config.PRESSURE_CAL_RAW - config.PRESSURE_ZERO_RAW)
        return (raw - config.PRESSURE_ZERO_RAW) * scale

    def _on_connect(self, *args):
        send(self.client, config.TOPIC_PRESSURE_STATUS, self.latest_status, retain=True)

    def calculate_drop(self, baseline, raw):
        if config.PRESSURE_DROP_WHEN_RAW_DECREASES:
            return baseline - raw
        else:
            return raw - baseline

    def cleanup_gpio(self):
        if self.gpio is None:
            return
        try:
            self.clock_low()
        except Exception:
            pass
        try:
            self._lgpio().gpio_free(self.gpio, config.PRESSURE_SCK_PIN)
        except Exception:
            pass
        try:
            self._lgpio().gpio_free(self.gpio, config.PRESSURE_OUT_PIN)
        except Exception:
            pass
        try:
            self._lgpio().gpiochip_close(self.gpio)
        except Exception:
            pass
        self.gpio = None

    def process_sample(self, raw, now, publish=True):
        result = self.detector.update(raw, now) if self.mode != "pressure" else None
        if result and result["event"]:
            event = {**result["event"], "session": self.session, "timestamp": time.time()}
            send(self.client, config.TOPIC_BUBBLE_EVENT, event)
        if not publish:
            return result
        self.latest_status = {"online": True, "ready": self.mode == "pressure" or result is not None,
            "mode": self.mode, "calibrated": self.calibration_enabled(), "error": None}
        send(self.client, config.TOPIC_PRESSURE_STATUS, self.latest_status, retain=True)
        send(self.client, config.TOPIC_PRESSURE_RAW, {"raw": raw}, retain=True)
        if self.mode != "bubble" and self.calibration_enabled():
            send(self.client, config.TOPIC_PRESSURE, {"pressure": round(self.raw_to_kpa(raw), 4)}, retain=True)
        if self.mode != "pressure":
            send(self.client, config.TOPIC_BUBBLE,
                 {"bubble": self.detector.active, "count": self.detector.count,
                  "session": self.session, "ready": result is not None}, retain=True)
            if result:
                send(self.client, config.TOPIC_PRESSURE_DROP, {"drop": round(result["drop"])}, retain=True)
                send(self.client, config.TOPIC_PRESSURE_THRESHOLD, {"threshold": round(result["threshold"])}, retain=True)
        return result

    def _read_loop(self):
        last_telemetry = -math.inf
        while True:
            try:
                raw = self.read_raw()
                if raw in (-8388608, 8388607):
                    raise OSError("HX710B ADC saturated; check sensor wiring and pressure range")
                now = time.monotonic()
                due = now - last_telemetry >= config.PRESSURE_TELEMETRY_INTERVAL_S
                self.process_sample(raw, now, publish=due)
                if due:
                    last_telemetry = now
            except (TimeoutError, OSError) as exc:
                # Missing samples cannot contribute to a sustained bubble hold.
                self.clock_low()
                self.detector.reset()
                self.latest_status = {"online": True, "ready": False, "mode": self.mode,
                    "calibrated": self.calibration_enabled(), "error": str(exc)}
                send(self.client, config.TOPIC_PRESSURE_STATUS, self.latest_status, retain=True)
                log.warning("%s", exc)
                time.sleep(0.1)

    def run(self):
        try:
            self.setup_gpio()
            connect_and_start(self.client, "pressure-hardware")
            self._read_loop()
        finally:
            self.cleanup_gpio()
            send(self.client, config.TOPIC_PRESSURE_STATUS, {"online": False}, retain=True)
            self.client.disconnect()
            self.client.loop_stop()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="HX710B pressure and bubble monitoring")
    parser.add_argument("--mode", choices=("pressure", "bubble", "both"), default=config.PRESSURE_MODE)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop_requested)
    client = PressureHardwareClient(args.mode)
    client.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception:
        log.exception("Fatal error")
        raise
