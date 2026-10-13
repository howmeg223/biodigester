"""Small MQTT helpers shared by the three executable processes.

`paho-mqtt` is imported lazily so pure domain/unit tests can run without the
networking dependency installed. Running controller.py, simulator.py or the
monitoring service still requires the package from requirements.txt.
"""
import json
import time

import config


def make_client(client_id):
    """Create a named MQTT client with automatic reconnection delays."""
    try:
        import paho.mqtt.client as mqtt
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "paho-mqtt is required to run MQTT processes. Install requirements.txt first."
        ) from exc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    return client


def connect_and_start(client, name):
    """Retry until connected, then receive MQTT messages in the background."""
    while True:
        try:
            client.connect(config.BROKER_HOST, config.BROKER_PORT, keepalive=config.KEEPALIVE_S)
            break
        except OSError as error:
            print(f"[{name}] Cannot reach MQTT broker at {config.BROKER_HOST}:{config.BROKER_PORT} "
                  f"({error}). Retrying in 3 s.")
            time.sleep(3)
    client.loop_start()


def connected_ok(reason_code):
    if hasattr(reason_code, "is_failure"):
        return not reason_code.is_failure
    return reason_code == 0


def send(client, topic, payload, retain=False):
    """Send a dictionary as JSON when connected.

    qos=1 asks MQTT to deliver at least once. retain=True asks the broker to
    keep the latest message for clients that subscribe later.
    """
    if client.is_connected():
        client.publish(topic, json.dumps(payload), qos=1, retain=retain)


def read_json(msg):
    """Decode message bytes into a dictionary, returning None for bad input."""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        return data
    return None
