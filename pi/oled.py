"""Jaycar XC3728 SH1106 SPI OLED monitor. Run python -m pi.oled."""
import logging
import signal
import time

# Support both python -m pi.NAME and python pi/NAME.py.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from mqtt_common import stop_requested
from monitoring.monitoring_service import MonitoringService


class LgpioOutputs:
    """Small luma GPIO adapter using the same Pi 5 compatible lgpio as sensors."""
    OUT, LOW, HIGH = 1, 0, 1

    def __init__(self):
        import lgpio
        self.lgpio = lgpio
        self.handle = lgpio.gpiochip_open(config.OLED_GPIO_CHIP)
        self.pins = []

    def setup(self, pin, mode):
        if pin not in self.pins:
            self.lgpio.gpio_claim_output(self.handle, pin, 0)
            self.pins.append(pin)

    def output(self, pin, value):
        self.lgpio.gpio_write(self.handle, pin, int(bool(value)))

    def close(self):
        if self.handle is not None:
            for pin in self.pins:
                self.lgpio.gpio_free(self.handle, pin)
            self.lgpio.gpiochip_close(self.handle)
            self.handle = None


def screen_lines(monitor):
    latest, messages, connected = monitor.snapshot()
    now = time.monotonic()
    def reading(key, unit, timeout):
        entry = latest.get(key)
        if not entry or entry[2] or not connected or now - entry[3] > timeout:
            return f"{key.title()}: --"
        return f"{key.title()}: {entry[0]:.2f}{unit}"
    status = monitor.controller_status()
    pressure = monitor.pressure_summary()
    bubbles = monitor.bubble_summary()
    entry = messages.get(config.TOPIC_PELTIER_STATE)
    output = "Peltier: --"
    if entry and connected and not entry[2] and now - entry[1] <= config.ACTUATOR_FEEDBACK_TIMEOUT_S:
        data = entry[0]
        if data.get("online") is not False:
            output = "Peltier: LATCHED" if data.get("safety_latched") else f"Peltier: {data.get('applied_power', 0):g}%"
    faults = status.get("faults", [])
    return [f"Digester {status['mode']}", reading("temperature", "C", config.TEMP_STALE_AFTER_S),
            reading("pressure", "kPa", config.PRESSURE_STALE_AFTER_S) if pressure.get("live") and pressure.get("calibrated") and pressure.get("mode") != "bubble" else "Pressure: --",
            f"Bubbles: {bubbles['count']}" + (" stale" if not bubbles['live'] else f" {bubbles['rate']}/m"),
            output, faults[0].get("code", "Fault") if faults else ("MQTT connected" if connected else "MQTT offline")]


def open_device():
    from luma.core.interface.serial import spi, i2c
    from luma.oled.device import sh1106, ssd1306
    gpio = None
    serial = None
    try:
        if config.OLED_INTERFACE == "spi":
            gpio = LgpioOutputs()
            serial = spi(gpio=gpio, port=config.OLED_SPI_PORT, device=config.OLED_SPI_DEVICE,
                         gpio_DC=config.OLED_DC_PIN, gpio_RST=config.OLED_RESET_PIN,
                         bus_speed_hz=1000000, reset_hold_time=0.01, reset_release_time=0.1)
        elif config.OLED_INTERFACE == "i2c":
            serial = i2c(port=config.OLED_I2C_PORT, address=config.OLED_I2C_ADDRESS)
        else:
            raise ValueError("OLED_INTERFACE must be spi or i2c")
        driver = {"sh1106": sh1106, "ssd1306": ssd1306}[config.OLED_DRIVER]
        return driver(serial, width=128, height=64, rotate=config.OLED_ROTATE), gpio
    except Exception:
        try:
            if serial:
                serial.cleanup()
        finally:
            if gpio:
                gpio.close()
        raise


def draw_screen(device, lines):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("1", device.size)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=10)
    for row, line in enumerate(lines):
        while line and draw.textlength(line, font=font) > device.width:
            line = line[:-1]
        draw.text((0, row * 10), line, font=font, fill=255)
    device.display(image)
    return image


def main():
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, stop_requested)
    monitor = MonitoringService(log_enabled=False)
    device = gpio = None
    try:
        device, gpio = open_device()
        monitor.start()
        while True:
            draw_screen(device, screen_lines(monitor))
            time.sleep(config.OLED_REFRESH_S)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        try:
            if device:
                device.cleanup()
        finally:
            if gpio:
                gpio.close()


if __name__ == "__main__":
    main()
