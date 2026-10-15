#!/usr/bin/env python3

import logging
import signal
import math
import threading
import time
# Support both python -m pi.NAME and python pi/NAME.py.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from mqtt_common import (connect_and_start, connected_ok, make_client, read_json,
                         read_power, send, stop_requested, limit_peltier_power,
                         peltier_power_limit, POWER_CONTROL_VERSION)

log = logging.getLogger("peltier-hardware")


class PeltierHardwareClient:
    def __init__(self, clock=time.monotonic):
        peltier_power_limit()  # Reject invalid limits before opening any GPIO.
        self.clock = clock
        self.client = make_client("biodigester-peltier-hardware")
        self.gpio = None
        self.lock = threading.RLock()
        self.current_direction = 0
        self.requested_power = 0.0
        self.applied_power = 0.0
        self.last_command_time = None
        self.watchdog_timed_out = False
        self.safety_latched = False
        self.last_error = None
        self.output_unknown = False
        self.stop_event = threading.Event()
        self._configure_mqtt()

    @staticmethod
    def _lgpio():
        try:
            import lgpio
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "lgpio is required on the Raspberry Pi running peltier.py. "
                "Install the Raspberry Pi lgpio package before starting this client."
            ) from exc
        return lgpio
    
    def _configure_mqtt(self):
        import json
        self.client.will_set(config.TOPIC_PELTIER_STATE,
            json.dumps({"online": False}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    
    def setup_gpio(self):
        """
        Open /dev/gpiochip0 and claim the two PWM GPIOs. 
        Both outputs start LOW.
        """
        lgpio = self._lgpio()
        log.info("Opening /dev/gpiochip%d", config.PELTIER_GPIO_CHIP)
        log.info("%s: driver=%s config=%s maximum PWM=%.1f%% frequency=%s Hz",
                 POWER_CONTROL_VERSION, __file__, config.__file__,
                 peltier_power_limit(), config.PELTIER_PWM_FREQUENCY_HZ)
        self.gpio = lgpio.gpiochip_open(config.PELTIER_GPIO_CHIP)
        lgpio.gpio_claim_output(self.gpio, config.PELTIER_RPWM_PIN, 0)
        lgpio.gpio_claim_output(self.gpio, config.PELTIER_LPWM_PIN, 0)
        lgpio.gpio_write(self.gpio, config.PELTIER_RPWM_PIN, 0)
        lgpio.gpio_write(self.gpio, config.PELTIER_LPWM_PIN, 0)
        
        log.info(
            "GPIO ready: RPWM=GPIO%d LPWM=GPIO%d",
            config.PELTIER_RPWM_PIN,
            config.PELTIER_LPWM_PIN,
        )

    
    def _stop_pwm(self, pin):
        """
        Stop output on one direction.

        Important:
        This uses a VALID PWM frequency with 0% duty cycle.

        Do NOT use:
            lgpio.tx_pwm(..., 0, 0)

        because the lgpio version on this machine rejects a
        zero frequency with 'bad PWM micros'.
        """

        lgpio = self._lgpio()

        lgpio.tx_pwm(
            self.gpio,
            pin,
            config.PELTIER_PWM_FREQUENCY_HZ,
            0
        )

        # Explicitly hold the pin LOW
        lgpio.gpio_write(
            self.gpio,
            pin,
            0
        )


    def _all_off(self):
        """
        Turn both sides of the H-bridge OFF.
        """
        errors = []
        for pin in (config.PELTIER_RPWM_PIN, config.PELTIER_LPWM_PIN):
            try:
                self._stop_pwm(pin)
            except Exception as exc:
                errors.append(exc)
        if errors:
            self.output_unknown = True
            self.safety_latched = True
            raise errors[0]
        self.output_unknown = False
        self.current_direction = 0
        self.applied_power = 0.0

    
    def emergency_off(self, reason=None):
        """
        Best-effort shutdown.

        Used during exceptions and cleanup.
        """
        with self.lock:
            if self.gpio is None:
                self.current_direction = 0
                self.applied_power = 0.0
                return
            try:
                self._all_off()
            except Exception as exc:
                self.last_error = f"Output shutdown failed: {exc}"
                self.safety_latched = True
                lgpio = self._lgpio()
                for pin in (config.PELTIER_RPWM_PIN, config.PELTIER_LPWM_PIN):
                    try:
                        lgpio.gpio_write(self.gpio, pin, 0)
                    except Exception:
                        pass
                self.current_direction = 0
                self.applied_power = 0.0
            if reason:
                log.warning("Peltier forced OFF: %s", reason)



    
    def cleanup_gpio(self):
        with self.lock:
            if self.gpio is None:
                return
            lgpio = self._lgpio()
            self.emergency_off("client shutdown")
            for pin in (config.PELTIER_RPWM_PIN, config.PELTIER_LPWM_PIN):
                try:
                    lgpio.gpio_free(self.gpio, pin)
                except Exception:
                    pass
            try:
                lgpio.gpiochip_close(self.gpio)
            except Exception:
                pass
            self.gpio = None


    def set_power(self, command):
        command = read_power({"power": command})
        if not math.isfinite(command):
            raise ValueError("Peltier command must be finite")
        if command < -100 or command > 100:
            raise ValueError(f"Peltier command must be between -100 and 100; got {command}")
        if abs(command) < config.PELTIER_DEADBAND_PERCENT:
            command = 0.0

        with self.lock:
            self.requested_power = command
            if self.safety_latched and command != 0:
                self._all_off()
                self.last_error = "Command ignored because the software safety latch is active."
                self.publish_state()
                return

            # A zero command must still turn output off if configuration is invalid.
            if command != 0:
                command = limit_peltier_power(command)
            new_direction = 1 if command > 0 else (-1 if command < 0 else 0)
            duty = abs(command)

            if new_direction == 0:
                # Zero power turns both bridge outputs off.
                self._all_off()
                self.last_error = None
                log.info("Peltier OFF")
                self.publish_state()
                return

            if self.current_direction != 0 and new_direction != self.current_direction:
                # Pause with both outputs off before reversing current.
                self._all_off()
                time.sleep(config.PELTIER_DIRECTION_CHANGE_DELAY_S)

            lgpio = self._lgpio()
            if new_direction > 0:
                # Positive power drives RPWM; keep LPWM off.
                self._stop_pwm(config.PELTIER_LPWM_PIN)
                lgpio.tx_pwm(
                    self.gpio,
                    config.PELTIER_RPWM_PIN,
                    config.PELTIER_PWM_FREQUENCY_HZ,
                    duty,
                )
            else:
                # Negative power drives LPWM; keep RPWM off.
                self._stop_pwm(config.PELTIER_RPWM_PIN)
                lgpio.tx_pwm(
                    self.gpio,
                    config.PELTIER_LPWM_PIN,
                    config.PELTIER_PWM_FREQUENCY_HZ,
                    duty,
                )

            self.current_direction = new_direction
            self.applied_power = command
            self.last_error = None
            log.info("Peltier applied %.1f%%", command)
            self.publish_state()

    
    def state_payload(self):
        if self.current_direction > 0:
            direction = "POSITIVE"
        elif self.current_direction < 0:
            direction = "NEGATIVE"
        else:
            direction = "OFF"
        return {
            "online": not self.output_unknown,
            "output_unknown": self.output_unknown,
            "requested_power": round(self.requested_power, 2),
            "applied_power": round(self.applied_power, 2),
            "max_power": peltier_power_limit(),
            "driver_version": POWER_CONTROL_VERSION,
            "driver_file": __file__,
            "config_file": config.__file__,
            "pwm_frequency_hz": config.PELTIER_PWM_FREQUENCY_HZ,
            "direction": direction,
            "safety_latched": self.safety_latched,
            "watchdog_timed_out": self.watchdog_timed_out,
            "last_error": self.last_error,
        }
    

    def publish_state(self):
        send(self.client, config.TOPIC_PELTIER_STATE, self.state_payload(), retain=True)




    def _on_connect(self, client, userdata, flags, reason_code, *extra):
        if connected_ok(reason_code):
            client.subscribe(config.TOPIC_PELTIER_CMD, qos=1)
            client.subscribe(config.TOPIC_PELTIER_SAFETY_CMD, qos=1)
            log.info("Connected to MQTT broker and subscribed to Peltier commands")
            self.publish_state()
        else:
            log.error("MQTT connection failed: %s", reason_code)



    def _on_disconnect(self, *args):
        with self.lock:
            self.safety_latched = True
            self.last_command_time = None
            self.emergency_off("MQTT disconnected")



    def _on_message(self, client, userdata, message):
        if getattr(message, "retain", False):
            log.warning("Ignoring saved actuator command on %s", message.topic)
            return
        data = read_json(message)
        if not isinstance(data, dict):
            log.warning("Ignoring non-JSON MQTT payload on %s", message.topic)
            return

        try:
            if message.topic == config.TOPIC_PELTIER_CMD:
                try:
                    value = read_power(data)
                except ValueError as exc:
                    log.warning("Invalid Peltier MQTT command %r: %s", data, exc)
                    return
                with self.lock:
                    self.last_command_time = self.clock()
                    self.watchdog_timed_out = False
                    self.set_power(value)

            elif message.topic == config.TOPIC_PELTIER_SAFETY_CMD:
                action = data.get("action")
                if action == "TRIP":
                    with self.lock:
                        self.safety_latched = True
                        self.emergency_off("software safety trip")
                        self.publish_state()
                elif action == "RESET":
                    with self.lock:
                        self._all_off()
                        self.emergency_off("safety reset: awaiting fresh power command")
                        self.requested_power = 0.0
                        self.last_command_time = None
                        self.safety_latched = False
                        self.watchdog_timed_out = False
                        self.last_error = None
                        self.publish_state()
        except Exception as exc:
            self.last_error = str(exc)
            self.safety_latched = True
            log.exception("Error applying Peltier command")
            self.emergency_off("command error")
            self.publish_state()

    # ------------------------------------------------------------------ Watchdog
    def watchdog_step(self):
        with self.lock:
            if self.last_command_time is None or self.safety_latched:
                return False
            age = self.clock() - self.last_command_time
            if config.PELTIER_COMMAND_TIMEOUT_S is None or age <= config.PELTIER_COMMAND_TIMEOUT_S:
                return False
            if not self.watchdog_timed_out:
                self.watchdog_timed_out = True
                self.safety_latched = True
                self.emergency_off(
                    f"no controller command for {age:.1f} s; software safety latch set"
                )
                self.publish_state()
                return True
            return False

    def _watchdog_loop(self):
        while not self.stop_event.wait(0.5):
            try:
                self.watchdog_step()
                with self.lock:
                    self.publish_state()
            except Exception:
                log.exception("Peltier watchdog/state error")
                with self.lock:
                    self.safety_latched = True
                    self.emergency_off("watchdog error")

    def run(self):
        thread = None
        try:
            self.setup_gpio()
            connect_and_start(self.client, "peltier-hardware")
            thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="peltier-watchdog")
            thread.start()
            while True:
                time.sleep(1)
        finally:
            self.stop_event.set()
            if thread:
                thread.join(timeout=2)
            self.cleanup_gpio()
            send(self.client, config.TOPIC_PELTIER_STATE, {"online": False}, retain=True)
            self.client.disconnect()
            self.client.loop_stop()


def main():
    """Start the Raspberry Pi Peltier hardware client."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, stop_requested)
    client = PeltierHardwareClient()
    client.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception:
        log.exception("Fatal error")
        raise
