"""mqtt_common.py - the few MQTT helpers shared by the controller, simulator and app."""

import json
import time

import paho.mqtt.client as mqtt

import config


def make_client(client_id):
    """Create a client that reconnects automatically."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    return client


def connect_and_start(client, name):
    """Connect to the broker, retrying until it answers, then start the network loop."""
    while True:
        try:
            client.connect(config.BROKER_HOST, config.BROKER_PORT, keepalive=config.KEEPALIVE_S)
            break
        except OSError as error:
            print(f"[{name}] Cannot reach the MQTT broker at "
                  f"{config.BROKER_HOST}:{config.BROKER_PORT} ({error}). "
                  "Is Mosquitto running? Trying again in 3 s.")
            time.sleep(3)
    # Receive MQTT messages in a background thread so the main program can
    # keep running its controller or simulator loop.
    client.loop_start()


def connected_ok(reason_code):
    """Did on_connect report success? Handles paho 1.x (int) and 2.x (ReasonCode)."""
    if hasattr(reason_code, "is_failure"):
        return not reason_code.is_failure  # paho-mqtt 2.x
    return reason_code == 0                # paho-mqtt 1.x


def send(client, topic, payload, retain=False):
    """Publish a dict as JSON. Skipped while disconnected, so old commands never"""
    if client.is_connected():
        # JSON turns a dictionary into text that another program can read.
        # qos=1 requests delivery acknowledgement (duplicates are possible).
        # retain=True asks the broker to save the latest value for new clients.
        client.publish(topic, json.dumps(payload), qos=1, retain=retain)


def read_json(msg):
    """Return the message payload as a dict, or None if it is not a JSON object."""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        return data
    return None
