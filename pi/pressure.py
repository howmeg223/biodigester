#!/usr/bin/env python3

import logging
import statistics
import time

import config
from mqtt_common import connect_and_start, make_client, send

log = logging.getLogger("pressure-hardware")


class PressureHardwareClient:
    def __init__(self):
        self.client = make_client("biodigester-pressure-hardware")
        self.gpio = None
        self.bubble_active = False
        self.bubble_count = 0

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
        lgpio = self._lgpio()
        self.clock_low()
        self.wait_ready()
        value = 0
        # Read MSB first without sleeping while SCK is high.
        for _ in range(24):
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 1)
            bit = lgpio.gpio_read(self.gpio, config.PRESSURE_OUT_PIN)
            value = value << 1 | bit
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)
        for _ in range(config.PRESSURE_EXTRA_CLOCKS):
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 1)
            lgpio.gpio_write(self.gpio, config.PRESSURE_SCK_PIN, 0)
        # Interpret the ADC's 24-bit two's-complement value.
        if value & 0x800000:
            value -= 1 << 24
        return value

    def calibration_enabled(self):
        return (
            config.PRESSURE_ZERO_RAW is not None
            and config.PRESSURE_CAL_RAW is not None
            and (config.PRESSURE_CAL_KPA is not None)
            and (config.PRESSURE_CAL_RAW != config.PRESSURE_ZERO_RAW)
        )

    def raw_to_kpa(self, raw):
        scale = config.PRESSURE_CAL_KPA / (
            config.PRESSURE_CAL_RAW - config.PRESSURE_ZERO_RAW
        )
        return (raw - config.PRESSURE_ZERO_RAW) * scale

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

    def _read_loop(self):
        # The first read selects 40 Hz mode for subsequent conversions.
        log.info("Starting HX710B in 40 Hz differential mode")
        first_reading = self.read_raw()
        log.info("First raw reading: %d", first_reading)
        log.info(
            "Learning pressure baseline for %d samples...",
            config.PRESSURE_WARMUP_SAMPLES,
        )
        warmup = []
        while len(warmup) < config.PRESSURE_WARMUP_SAMPLES:
            try:
                raw = self.read_raw()
                warmup.append(raw)
            except TimeoutError as exc:
                log.warning("%s", exc)
        baseline = float(statistics.median(warmup))
        noise = float(statistics.pstdev(warmup))
        noise = max(noise, 1.0)
        log.info("Baseline learned: %.1f", baseline)
        log.info("Initial noise: %.1f counts", noise)
        log.info("Minimum drop threshold: %d counts", config.PRESSURE_MIN_DROP_COUNTS)
        log.info("Drop hold time: %.3f sec", config.PRESSURE_DROP_HOLD_SECONDS)
        send(self.client, config.TOPIC_BUBBLE, {"bubble": 0}, retain=True)
        candidate_start = None
        clear_start = None
        last_telemetry = 0
        last_log = 0
        while True:
            try:
                raw = self.read_raw()
                now = time.monotonic()
                adaptive_threshold = noise * config.PRESSURE_NOISE_MULTIPLIER
                threshold = max(
                    float(config.PRESSURE_MIN_DROP_COUNTS), adaptive_threshold
                )
                drop = self.calculate_drop(baseline, raw)
                if not self.bubble_active:
                    if drop >= threshold:
                        if candidate_start is None:
                            candidate_start = now
                        held_for = now - candidate_start
                        if held_for >= config.PRESSURE_DROP_HOLD_SECONDS:
                            self.bubble_active = True
                            self.bubble_count += 1
                            clear_start = None
                            log.warning(
                                "BUBBLE DETECTED #%d drop=%.0f threshold=%.0f raw=%d baseline=%.0f held=%.3fs",
                                self.bubble_count,
                                drop,
                                threshold,
                                raw,
                                baseline,
                                held_for,
                            )
                            send(
                                self.client,
                                config.TOPIC_BUBBLE,
                                {"bubble": 1},
                                retain=True,
                            )
                            event = {
                                "event": "bubble",
                                "count": self.bubble_count,
                                "raw": raw,
                                "baseline": round(baseline),
                                "drop": round(drop),
                                "threshold": round(threshold),
                                "hold_seconds": round(held_for, 3),
                            }
                            send(
                                self.client,
                                config.TOPIC_BUBBLE_EVENT,
                                event,
                                retain=False,
                            )
                    else:
                        candidate_start = None
                        # Track baseline and noise only outside a pressure drop.
                        error = raw - baseline
                        baseline += config.PRESSURE_BASELINE_ALPHA * error
                        deviation = abs(raw - baseline)
                        noise += config.PRESSURE_NOISE_ALPHA * (deviation - noise)
                        noise = max(noise, 1.0)
                else:
                    # Require sustained recovery before clearing the bubble.
                    release_level = threshold * config.PRESSURE_RELEASE_THRESHOLD_RATIO
                    if drop <= release_level:
                        if clear_start is None:
                            clear_start = now
                        recovered_for = now - clear_start
                        if recovered_for >= config.PRESSURE_CLEAR_HOLD_SECONDS:
                            self.bubble_active = False
                            candidate_start = None
                            clear_start = None
                            log.info("Bubble cleared drop=%.0f raw=%d", drop, raw)
                            send(
                                self.client,
                                config.TOPIC_BUBBLE,
                                {"bubble": 0},
                                retain=True,
                            )
                    else:
                        clear_start = None
                if now - last_telemetry >= config.PRESSURE_TELEMETRY_INTERVAL_S:
                    # Limit MQTT traffic while detection runs at the sensor rate.
                    send(
                        self.client,
                        config.TOPIC_PRESSURE_RAW,
                        {"raw": raw},
                        retain=True,
                    )
                    send(
                        self.client,
                        config.TOPIC_PRESSURE_DROP,
                        {"drop": round(drop, 0)},
                        retain=True,
                    )
                    send(
                        self.client,
                        config.TOPIC_PRESSURE_THRESHOLD,
                        {"threshold": round(threshold, 0)},
                        retain=True,
                    )
                    if self.calibration_enabled():
                        kpa = self.raw_to_kpa(raw)
                        send(
                            self.client,
                            config.TOPIC_PRESSURE,
                            {"pressure": round(kpa, 4)},
                            retain=True,
                        )
                    last_telemetry = now
                if now - last_log >= 1.0:
                    log.info(
                        "raw=%d baseline=%.0f drop=%.0f threshold=%.0f noise=%.0f bubble=%d",
                        raw,
                        baseline,
                        drop,
                        threshold,
                        noise,
                        1 if self.bubble_active else 0,
                    )
                    last_log = now
            except TimeoutError as exc:
                log.warning("%s", exc)
            except Exception:
                log.exception("Pressure processing error")

    def run(self):
        try:
            self.setup_gpio()
            connect_and_start(self.client, "pressure-hardware")
            self._read_loop()
        finally:
            try:
                send(self.client, config.TOPIC_BUBBLE, {"bubble": 0}, retain=True)
            finally:
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                finally:
                    self.cleanup_gpio()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    client = PressureHardwareClient()
    client.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception:
        log.exception("Fatal error")
