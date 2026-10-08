"""
What should be on the controller program:


"""

import argparse
import json
import math
import signal
import sys
import threading
import time

import config
from control_logic import MESSAGES, Regulator, Settings, Thresholds
from mqtt_common import connect_and_start, connected_ok, make_client, read_json, send


def tidy(value):
    """Show 40.0 as 40, and leave anything that is not a number as it came."""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def make_regulator(strategy):
    """Copy config.py settings into the object that makes heating decisions."""
    return Regulator(Thresholds(), Settings(strategy=strategy))


def read_temperature(data):
    """Read a decimal temperature; mark missing or broken values as NaN."""
    try:
        return float(data["temperature"])
    except (TypeError, KeyError, ValueError):
        return math.nan


def stop_requested(signum, frame):
    """A system stop request exits through main()'s finally block."""
    sys.exit(0)


def main():
    # 1. Read optional terminal arguments, then prepare the heating rules.
    parser = argparse.ArgumentParser(description="Biodigester temperature controller")
    parser.add_argument("--strategy", choices=["hysteresis", "simple"],
                        default=config.STRATEGY)
    args = parser.parse_args()

    regulator = make_regulator(args.strategy)

    # MQTT receives messages in a background thread while main() runs here.
    # The lock lets only one thread use these shared values at a time.
    lock = threading.Lock()
    latest = {"temperature": None, "received_at": None, "heater_reported_on": None,
              "note": "", "note_id": 0}

    # 2. Prepare MQTT. A 'will' is a message the broker sends if we disappear.
    client = make_client("biodigester-controller")
    client.will_set(config.TOPIC_STATUS,
                    json.dumps({"mode": "OFFLINE", "heating": False,
                                "reason": "The controller has stopped."}),
                    qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, *extra):
        """MQTT calls this after connecting, including after a reconnection."""
        if connected_ok(reason_code):
            client.subscribe(config.TOPIC_TEMPERATURE, qos=1)
            client.subscribe(config.TOPIC_THRESHOLDS, qos=1)
            client.subscribe(config.TOPIC_HEATER_STATE, qos=1)
            print("[controller] Connected to the broker.")

    def on_message(client, userdata, msg):
        """MQTT calls this for each message on a subscribed topic."""
        data = read_json(msg)

        if msg.topic == config.TOPIC_TEMPERATURE:
            value = read_temperature(data)
            with lock:
                latest["temperature"] = value
                latest["received_at"] = time.monotonic()

        elif msg.topic == config.TOPIC_THRESHOLDS:
            if not msg.payload:
                return          # an empty message only clears the saved range on the broker
            if data is None:
                low = None
                high = None
            else:
                low = data.get("low")
                high = data.get("high")
            with lock:
                problem = regulator.set_thresholds(low, high)
                if problem is None:
                    latest["note"] = (f"Range set to {regulator.th.low:g} to "
                                      f"{regulator.th.high:g} °C.")
                else:
                    latest["note"] = (f"Range {tidy(low)} to {tidy(high)} was rejected. "
                                      f"{problem}")
                latest["note_id"] += 1      # lets the app spot a new reply
                print(f"[controller] {latest['note']}")

        elif msg.topic == config.TOPIC_HEATER_STATE:
            if data is None:
                return
            state = data.get("state")
            if state in ("ON", "OFF"):
                with lock:
                    latest["heater_reported_on"] = (state == "ON")

    client.on_connect = on_connect
    client.on_message = on_message

    # System stop requests use the same cleanup as Ctrl+C.
    signal.signal(signal.SIGTERM, stop_requested)

    # 3. Connect and start MQTT's background message receiver.
    connect_and_start(client, "controller")
    print(f"[controller] Strategy: {args.strategy}. "
          f"Holding {regulator.th.low:g} to {regulator.th.high:g} °C.")

    last_printed = None
    try:
        # 4. Repeat: read shared values, decide, send results, then wait.
        while True:
            now = time.monotonic()
            with lock:
                temperature = latest["temperature"]
                received_at = latest["received_at"]
                if received_at is None:
                    reading_age = None
                else:
                    reading_age = now - received_at
                decision = regulator.update(now, temperature, reading_age,
                                            latest["heater_reported_on"])
                # 'th' is the regulator's target range (its thresholds).
                low, high = regulator.th.low, regulator.th.high
                note, note_id = latest["note"], latest["note_id"]

            if decision.heating:
                action = "ON"
            else:
                action = "OFF"
            send(client, config.TOPIC_HEATER_CMD, {"action": action})

            usable = temperature is not None and math.isfinite(temperature)
            if usable:
                displayed_temperature = round(temperature, 2)
            else:
                displayed_temperature = None
            send(client, config.TOPIC_STATUS,
                 {"mode": decision.mode,
                  "heating": decision.heating,
                  "fault": decision.fault,
                  "alarms": decision.alarms,
                  "reason": decision.reason,
                  "alarm_text": [MESSAGES[a] for a in decision.alarms],
                  "temperature": displayed_temperature,
                  "low": low,
                  "high": high,
                  "strategy": args.strategy,
                  "note": note,
                  "note_id": note_id},
                 retain=True)

            # Print only when something changes, so the terminal reads as a log.
            summary = (decision.mode, decision.fault, tuple(decision.alarms), decision.heating)
            if summary != last_printed:
                last_printed = summary
                if usable:
                    shown = f"{temperature:.2f} °C"
                else:
                    shown = "no usable reading"
                extra = ""
                if decision.alarms:
                    extra = f"  alarms: {', '.join(decision.alarms)}"
                print(f"[controller] {decision.mode:<8} {shown:<18} {decision.reason}{extra}")

            time.sleep(config.CONTROL_PERIOD_S)
    except KeyboardInterrupt:
        pass
    finally:
        # 5. Always clean up when leaving the loop, including after Ctrl+C.
        print("\n[controller] Stopping: heater commanded off.")
        if client.is_connected():
            client.publish(config.TOPIC_HEATER_CMD, json.dumps({"action": "OFF"}), qos=1)
            last = client.publish(config.TOPIC_STATUS,
                                  json.dumps({"mode": "OFFLINE", "heating": False,
                                              "reason": "The controller was stopped."}),
                                  qos=1, retain=True)
            last.wait_for_publish(timeout=2)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
