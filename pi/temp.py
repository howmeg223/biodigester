#!/usr/bin/env python3

import glob
import logging
import time

import paho.mqtt.client as mqtt


# ============================================================
# CONFIGURATION
# ============================================================

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "temperature"

PUBLISH_INTERVAL = 20

# Publish as retained message so new subscribers immediately
# receive the most recent temperature.
MQTT_RETAIN = True


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("ds18b20")


# ============================================================
# DS18B20
# ============================================================

def find_sensor():
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


def read_temperature(sensor_path):
    """
    Read temperature from the Linux w1_therm driver.

    The temperature file returns millidegrees Celsius.
    """

    temperature_file = (
        sensor_path + "/temperature"
    )

    with open(
        temperature_file,
        "r",
        encoding="utf-8"
    ) as f:

        raw = f.read().strip()

    temperature_c = float(raw) / 1000.0

    return temperature_c


# ============================================================
# MQTT
# ============================================================

def create_mqtt_client():

    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2
        )

    except AttributeError:
        client = mqtt.Client()

    return client


# ============================================================
# MAIN
# ============================================================

def main():

    sensor = find_sensor()

    client = create_mqtt_client()

    log.info(
        "Connecting to MQTT broker %s:%d",
        MQTT_BROKER,
        MQTT_PORT
    )

    client.connect(
        MQTT_BROKER,
        MQTT_PORT,
        keepalive=60
    )

    client.loop_start()

    try:

        while True:

            try:

                temperature = read_temperature(
                    sensor
                )

                payload = f"{temperature:.3f}"

                result = client.publish(
                    MQTT_TOPIC,
                    payload,
                    qos=0,
                    retain=MQTT_RETAIN
                )

                log.info(
                    "Temperature %.3f C -> %s",
                    temperature,
                    MQTT_TOPIC
                )

            except FileNotFoundError:

                log.warning(
                    "DS18B20 disappeared; trying to find it again"
                )

                sensor = find_sensor()

            except Exception:

                log.exception(
                    "Temperature read/publish failed"
                )

            time.sleep(
                PUBLISH_INTERVAL
            )

    finally:

        client.loop_stop()
        client.disconnect()


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
