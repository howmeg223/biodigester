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
    if config.MQTT_USERNAME is not None:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
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
        client.publish(topic, json.dumps(payload, allow_nan=False), qos=1, retain=retain)


def read_json(msg):
    """Decode message bytes into a dictionary, returning None for bad input."""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        return data
    return None


def read_power(data, key="power"):
    """Validate the shared signed Peltier MQTT contract."""
    import math
    if not isinstance(data, dict) or isinstance(data.get(key), bool):
        raise ValueError("power must be numeric")
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("power must be numeric") from exc
    if not math.isfinite(value) or not -100 <= value <= 100:
        raise ValueError("power must be finite and between -100 and 100")
    return value


POWER_CONTROL_VERSION = "pwm-cap-v2"


def peltier_power_limit():
    """Read and validate the maximum PWM duty in the active config module."""
    import math
    limit = getattr(config, "PELTIER_MAX_POWER", 30.0)
    if isinstance(limit, bool):
        raise ValueError("PELTIER_MAX_POWER must be numeric and between 0 and 100")
    limit = float(limit)
    if not math.isfinite(limit) or not 0 <= limit <= 100:
        raise ValueError("PELTIER_MAX_POWER must be finite and between 0 and 100")
    return limit


def limit_peltier_power(power):
    """Cap signed duty and apply the same deadband in all processes."""
    limit = peltier_power_limit()
    power = read_power({"power": power})
    power = max(-limit, min(limit, power))
    return 0.0 if abs(power) < config.PELTIER_DEADBAND_PERCENT else power


def peltier_heating_power():
    """Automatic heating must never request reverse polarity."""
    power = read_power({"power": config.PELTIER_HEAT_POWER})
    if power < 0:
        raise ValueError("PELTIER_HEAT_POWER must be between 0 and 100")
    return limit_peltier_power(power)


def stop_requested(signum, frame):
    """Let hardware main loops run their finally cleanup on SIGTERM."""
    raise KeyboardInterrupt
