#!/usr/bin/env python3

import glob
import logging
import time

import config
from mqtt_common import connect_and_start, make_client, send

log = logging.getLogger("temperature_hardware")


class TemperatureHardwareClient:
 def _init_ (self):
     self.sensor_path = None
     self.client = make_client("biodigester_temperature_hardware")




def find_sensor(self):
    """
    DS18B20 devices have 1-Wire family ID 28.
    """

    sensors = glob.glob(
        "/sys/bus/w1/devices/28-*"
    )

    if not sensors:
        raise RuntimeError(
            "No DS18B20 found under /sys/bus/w1/devices/"
        )

    sensor_path = sensors[0]

    log.info(
        "Using DS18B20: %s",
        sensor_path
    )

    return sensor_path



def read_temperature(self):
    """
    Read temperature from the Linux w1_therm driver.

    The temperature file returns millidegrees Celsius.
    """

    if self.sensor_path is None:
        self.find_sensor

    temperature_file = (
        self.sensor_path + "/temperature"
    )

    with open(
        temperature_file,
        "r",
        encoding="utf-8"
    ) as f:

        raw = f.read().strip()

    temperature_c = float(raw) / 1000.0

    return temperature_c

def publish_temperature(self):
    send(
        self.client,
        config.TOPIC_TEMPERATURE,
        {"temperature": round(float(self.temperature_c), 3)},
        retain=config.MQTT_RETAIN,
    )
    log.info(
                    "Temperature %.3f C -> %s",
                    self.temperature_c,
                    config.TOPIC_TEMPERATURE
                )


def run(self):

    self.find_sensor()
    connect_and_start(self.client, "temperature_hardware")
    try:
        while True:
            try:
                self.publish_temperature(self.read_temperature()) 
            except FileNotFoundError:
                log.warning(
                    "DS18B20 disappeared; trying to find it again"
                )
                self.sensor_path = None

            try:
                self.find_sensor()
            except RuntimeError:
                pass

            except Exception:
                log.exception(
                    "Temperature read/publish failed"
                )
            time.sleep(
                config.PUBLISH_INTERVAL
            )
    finally:
        self.client.loop_stop()
        self.client.disconnect()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    client = TemperatureHardwareClient()
    client.run



if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        log.info(
            "Stopped by user"
        )

    except Exception:

        log.exception(
            "Fatal error"
        )
