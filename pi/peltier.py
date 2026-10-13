#!/usr/bin/env python3

import logging
import math
import threading
import time

import lgpio
import paho.mqtt.client as mqtt


# ============================================================
# CONFIGURATION
# ============================================================

# MQTT broker
MQTT_BROKER = "127.0.01"
MQTT_PORT = 1883
MQTT_TOPIC = "peltier/power"

# Set these if your broker requires authentication
MQTT_USERNAME = None
MQTT_PASSWORD = None

# Raspberry Pi 5 GPIO chip
GPIO_CHIP = 0

# BCM GPIO numbers
#
# GPIO18 -> BTS7960 RPWM
# GPIO19 -> BTS7960 LPWM
RPWM_PIN = 18
LPWM_PIN = 19

# PWM frequency in Hz
PWM_FREQUENCY = 1000

# When reversing direction, turn everything off for this long
DIRECTION_CHANGE_DELAY = 0.050  # 50 ms

# If no valid MQTT command is received for this many seconds,
# shut the Peltier off.
MQTT_TIMEOUT = None

# Commands smaller than this are treated as zero
DEADBAND = 1.0


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("peltier")


# ============================================================
# GLOBAL STATE
# ============================================================

gpio = None

lock = threading.Lock()

current_direction = 0
last_command_time = 0.0
timed_out = False


# ============================================================
# GPIO FUNCTIONS
# ============================================================

def setup_gpio():
    """
    Open /dev/gpiochip0 and claim the two PWM GPIOs.
    Both outputs start LOW.
    """
    global gpio

    log.info("Opening /dev/gpiochip%d", GPIO_CHIP)

    gpio = lgpio.gpiochip_open(GPIO_CHIP)

    lgpio.gpio_claim_output(
        gpio,
        RPWM_PIN,
        0
    )

    lgpio.gpio_claim_output(
        gpio,
        LPWM_PIN,
        0
    )

    # Explicitly make sure both outputs start LOW
    lgpio.gpio_write(
        gpio,
        RPWM_PIN,
        0
    )

    lgpio.gpio_write(
        gpio,
        LPWM_PIN,
        0
    )

    log.info(
        "GPIO ready: RPWM=GPIO%d LPWM=GPIO%d",
        RPWM_PIN,
        LPWM_PIN
    )


def stop_pwm(pin):
    """
    Stop output on one direction.

    Important:
    This uses a VALID PWM frequency with 0% duty cycle.

    Do NOT use:
        lgpio.tx_pwm(..., 0, 0)

    because the lgpio version on this machine rejects a
    zero frequency with 'bad PWM micros'.
    """

    lgpio.tx_pwm(
        gpio,
        pin,
        PWM_FREQUENCY,
        0
    )

    # Explicitly hold the pin LOW
    lgpio.gpio_write(
        gpio,
        pin,
        0
    )


def all_off():
    """
    Turn both sides of the H-bridge OFF.
    """

    stop_pwm(RPWM_PIN)
    stop_pwm(LPWM_PIN)


def emergency_off():
    """
    Best-effort shutdown.

    Used during exceptions and cleanup.
    """

    global current_direction

    if gpio is None:
        return

    try:
        all_off()
    except Exception:
        try:
            lgpio.gpio_write(
                gpio,
                RPWM_PIN,
                0
            )
        except Exception:
            pass

        try:
            lgpio.gpio_write(
                gpio,
                LPWM_PIN,
                0
            )
        except Exception:
            pass

    current_direction = 0


# ============================================================
# PELTIER CONTROL
# ============================================================

def set_peltier(command):
    """
    Set Peltier power.

      +100 = full power in positive direction
       +50 = 50% positive direction
         0 = OFF
       -50 = 50% negative direction
      -100 = full power in negative direction

    Only ONE PWM input is active at a time.
    """

    global current_direction

    command = float(command)

    if not math.isfinite(command):
        raise ValueError("Command must be a finite number")

    if command < -100 or command > 100:
        raise ValueError(
            f"Command must be between -100 and 100; got {command}"
        )

    # Deadband around zero
    if abs(command) < DEADBAND:
        command = 0.0

    if command > 0:
        new_direction = 1

    elif command < 0:
        new_direction = -1

    else:
        new_direction = 0

    duty = abs(command)

    with lock:

        # ----------------------------------------------------
        # OFF
        # ----------------------------------------------------

        if new_direction == 0:

            all_off()

            current_direction = 0

            log.info("Peltier OFF")

            return

        # ----------------------------------------------------
        # DIRECTION CHANGE
        # ----------------------------------------------------

        if (
            current_direction != 0
            and new_direction != current_direction
        ):

            log.info(
                "Direction change: bridge OFF for %.0f ms",
                DIRECTION_CHANGE_DELAY * 1000
            )

            all_off()

            current_direction = 0

            time.sleep(
                DIRECTION_CHANGE_DELAY
            )

        # ----------------------------------------------------
        # POSITIVE DIRECTION
        # ----------------------------------------------------

        if new_direction > 0:

            # Opposite side MUST be off first
            stop_pwm(LPWM_PIN)

            lgpio.tx_pwm(
                gpio,
                RPWM_PIN,
                PWM_FREQUENCY,
                duty
            )

            current_direction = 1

            log.info(
                "Peltier + direction: %.1f%%",
                duty
            )

        # ----------------------------------------------------
        # NEGATIVE DIRECTION
        # ----------------------------------------------------

        else:

            # Opposite side MUST be off first
            stop_pwm(RPWM_PIN)

            lgpio.tx_pwm(
                gpio,
                LPWM_PIN,
                PWM_FREQUENCY,
                duty
            )

            current_direction = -1

            log.info(
                "Peltier - direction: %.1f%%",
                duty
            )


# ============================================================
# MQTT CALLBACKS
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties=None
):

    if reason_code == 0:

        log.info(
            "Connected to MQTT broker %s:%d",
            MQTT_BROKER,
            MQTT_PORT
        )

        client.subscribe(
            MQTT_TOPIC
        )

        log.info(
            "Subscribed to MQTT topic: %s",
            MQTT_TOPIC
        )

    else:

        log.error(
            "MQTT connection failed: %s",
            reason_code
        )


def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties=None
):

    log.warning(
        "MQTT disconnected: %s",
        reason_code
    )

    # Immediately shut the Peltier off if MQTT disappears
    with lock:
        emergency_off()


def on_message(
    client,
    userdata,
    message
):

    global last_command_time
    global timed_out

    try:

        payload = (
            message.payload
            .decode("utf-8")
            .strip()
        )

        value = float(payload)

        if not math.isfinite(value):

            log.warning(
                "Ignoring invalid MQTT value: %r",
                payload
            )

            return

        if value < -100 or value > 100:

            log.warning(
                "Ignoring out-of-range MQTT command: %s",
                payload
            )

            return

        # Only update watchdog after receiving a valid command
        last_command_time = time.monotonic()

        timed_out = False

        log.info(
            "MQTT command received: %.1f",
            value
        )

        set_peltier(
            value
        )

    except UnicodeDecodeError:

        log.warning(
            "MQTT payload was not valid UTF-8: %r",
            message.payload
        )

    except ValueError as exc:

        log.warning(
            "Invalid MQTT payload %r: %s",
            message.payload,
            exc
        )

    except Exception:

        log.exception(
            "Error processing MQTT message"
        )

        with lock:
            emergency_off()


# ============================================================
# WATCHDOG
# ============================================================

def watchdog():

    global timed_out
    global current_direction

    while True:

        time.sleep(0.5)

        # Haven't received our first command yet
        if last_command_time == 0:
            continue

        age = (
            time.monotonic()
            - last_command_time
        )

        if (
            age > MQTT_TIMEOUT
            and not timed_out
        ):

            with lock:

                all_off()

                current_direction = 0

            timed_out = True

            log.warning(
                "No MQTT command for %.1f seconds - Peltier OFF",
                age
            )


# ============================================================
# MQTT SETUP
# ============================================================

def create_mqtt_client():

    # Current Paho MQTT versions
    try:

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2
        )

        client.on_disconnect = on_disconnect

    # Fallback for an older Paho version
    except AttributeError:

        client = mqtt.Client()

    client.on_connect = on_connect
    client.on_message = on_message

    if MQTT_USERNAME is not None:

        client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

    return client


# ============================================================
# CLEANUP
# ============================================================

def cleanup_gpio():

    global gpio

    if gpio is None:
        return

    log.info(
        "Turning H-bridge OFF"
    )

    emergency_off()

    try:

        lgpio.gpio_free(
            gpio,
            RPWM_PIN
        )

    except Exception:
        pass

    try:

        lgpio.gpio_free(
            gpio,
            LPWM_PIN
        )

    except Exception:
        pass

    try:

        lgpio.gpiochip_close(
            gpio
        )

    except Exception:
        pass

    gpio = None


# ============================================================
# MAIN
# ============================================================

def main():

    setup_gpio()

    # Outputs were claimed LOW in setup_gpio().
    # There is deliberately no tx_pwm(..., 0, 0) here.

    log.info(
        "Starting Peltier MQTT controller"
    )

    log.info(
        "Control range: -100 to +100"
    )

    log.info(
        "PWM frequency: %d Hz",
        PWM_FREQUENCY
    )

    log.info(
        "MQTT watchdog timeout: %.1f seconds",
        MQTT_TIMEOUT
    )

    watchdog_thread = threading.Thread(
        target=watchdog,
        daemon=True,
        name="peltier-watchdog"
    )

    watchdog_thread.start()

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

    try:

        client.loop_forever()

    finally:

        try:
            client.disconnect()
        except Exception:
            pass


# ============================================================
# START
# ============================================================

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

    finally:

        cleanup_gpio()
