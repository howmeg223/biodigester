#!/usr/bin/env python3

import json
import logging
import math
import statistics
import time

import lgpio
import paho.mqtt.client as mqtt


# ============================================================
# MQTT CONFIGURATION
# ============================================================

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

MQTT_TOPIC_RAW = "digester/pressure_raw"
MQTT_TOPIC_DROP = "digester/pressure_drop"
MQTT_TOPIC_THRESHOLD = "digester/pressure_threshold"

MQTT_TOPIC_BUBBLE = "digester/bubble"
MQTT_TOPIC_BUBBLE_EVENT = "digester/bubble/event"

# Don't publish all 40 samples/sec over MQTT.
# Detection still happens at the full sensor rate.
MQTT_TELEMETRY_INTERVAL = 0.2       # 5 Hz


# ============================================================
# GPIO
# ============================================================

GPIO_CHIP = 0

# BCM numbering
SCK_PIN = 23
OUT_PIN = 24


# ============================================================
# HX710B
# ============================================================

READ_TIMEOUT = 1.0

# 27 clocks selects differential-input 40 Hz mode
EXTRA_CLOCKS = 3


# ============================================================
# BUBBLE DETECTION CONFIGURATION
# ============================================================

# Number of samples used to learn initial baseline.
#
# 80 samples @ 40 Hz = approximately 2 seconds.
WARMUP_SAMPLES = 80


# ------------------------------------------------------------
# DIRECTION
# ------------------------------------------------------------
#
# Normally pressure falling causes the raw ADC value to fall:
#
#     True:
#       baseline = 1,000,000
#       reading  =   900,000
#       drop     =   100,000
#
# If your sensor behaves backwards, change this to False.

DROP_WHEN_RAW_DECREASES = True


# ------------------------------------------------------------
# MINIMUM DROP
# ------------------------------------------------------------
#
# Absolute minimum change in raw ADC counts before it can
# possibly be considered a bubble.
#
# You'll probably want to tune this after watching the output.

MIN_DROP_COUNTS = 10000


# ------------------------------------------------------------
# NOISE REJECTION
# ------------------------------------------------------------
#
# The script also learns normal sensor noise.
#
# Detection threshold becomes:
#
# max(
#     MIN_DROP_COUNTS,
#     noise * NOISE_MULTIPLIER
# )

NOISE_MULTIPLIER = 6.0


# ------------------------------------------------------------
# HOLD TIME
# ------------------------------------------------------------
#
# Pressure must remain below the threshold for this long before
# a bubble is declared.
#
# At 40 Hz:
#
# 0.075 sec ~= 3 samples
# 0.100 sec ~= 4 samples
# 0.150 sec ~= 6 samples
# 0.250 sec ~= 10 samples

DROP_HOLD_SECONDS = 0.150


# ------------------------------------------------------------
# CLEARING THE EVENT
# ------------------------------------------------------------
#
# Bubble stays active until pressure returns close to normal.

RELEASE_THRESHOLD_RATIO = 0.40

# Must remain recovered for this long before bubble becomes 0.
CLEAR_HOLD_SECONDS = 0.250


# ------------------------------------------------------------
# BASELINE TRACKING
# ------------------------------------------------------------
#
# Slow adaptation allows gradual pressure changes without
# interpreting them as bubbles.
#
# Smaller = slower.

BASELINE_ALPHA = 0.002

# Noise estimate update speed
NOISE_ALPHA = 0.01


# ============================================================
# OPTIONAL CALIBRATION
# ============================================================
#
# Bubble detection DOES NOT need calibration.
#
# Leave these None unless you also want to publish kPa.

ZERO_RAW = None
CAL_RAW = None
CAL_KPA = None

MQTT_TOPIC_KPA = "digester/pressure"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("pressure")


# ============================================================
# GLOBALS
# ============================================================

gpio = None

bubble_active = False
bubble_count = 0


# ============================================================
# GPIO SETUP
# ============================================================

def setup_gpio():

    global gpio

    log.info(
        "Opening /dev/gpiochip%d",
        GPIO_CHIP
    )

    gpio = lgpio.gpiochip_open(
        GPIO_CHIP
    )

    lgpio.gpio_claim_output(
        gpio,
        SCK_PIN,
        0
    )

    lgpio.gpio_claim_input(
        gpio,
        OUT_PIN
    )

    lgpio.gpio_write(
        gpio,
        SCK_PIN,
        0
    )

    log.info(
        "HX710B GPIO ready: SCK=GPIO%d OUT=GPIO%d",
        SCK_PIN,
        OUT_PIN
    )


# ============================================================
# HX710B
# ============================================================

def wait_ready():

    deadline = (
        time.monotonic()
        + READ_TIMEOUT
    )

    # DOUT goes LOW when a conversion is ready.
    while lgpio.gpio_read(
        gpio,
        OUT_PIN
    ):

        if time.monotonic() >= deadline:

            raise TimeoutError(
                "HX710B did not become ready"
            )

        time.sleep(0.00025)


def clock_high():

    lgpio.gpio_write(
        gpio,
        SCK_PIN,
        1
    )


def clock_low():

    lgpio.gpio_write(
        gpio,
        SCK_PIN,
        0
    )


def read_raw():
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

    clock_low()

    wait_ready()

    value = 0

    # Read 24 data bits MSB first.
    #
    # Do not insert sleeps while SCK is HIGH.
    for _ in range(24):

        clock_high()

        bit = lgpio.gpio_read(
            gpio,
            OUT_PIN
        )

        value = (
            (value << 1)
            | bit
        )

        clock_low()

    # We've already sent 24 pulses.
    #
    # Send 3 additional pulses:
    #
    # 25
    # 26
    # 27
    #
    # selecting differential input @ 40 Hz.

    for _ in range(EXTRA_CLOCKS):

        clock_high()
        clock_low()

    # Convert 24-bit two's-complement number
    # into a normal Python signed integer.

    if value & 0x800000:
        value -= 1 << 24

    return value


# ============================================================
# PRESSURE CALIBRATION
# ============================================================

def calibration_enabled():

    return (
        ZERO_RAW is not None
        and CAL_RAW is not None
        and CAL_KPA is not None
        and CAL_RAW != ZERO_RAW
    )


def raw_to_kpa(raw):

    scale = (
        CAL_KPA
        / (CAL_RAW - ZERO_RAW)
    )

    return (
        raw - ZERO_RAW
    ) * scale


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
# DROP CALCULATION
# ============================================================

def calculate_drop(
    baseline,
    raw
):

    if DROP_WHEN_RAW_DECREASES:

        return (
            baseline - raw
        )

    else:

        return (
            raw - baseline
        )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_gpio():

    global gpio

    if gpio is None:
        return

    try:
        clock_low()
    except Exception:
        pass

    try:
        lgpio.gpio_free(
            gpio,
            SCK_PIN
        )
    except Exception:
        pass

    try:
        lgpio.gpio_free(
            gpio,
            OUT_PIN
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

    global bubble_active
    global bubble_count

    setup_gpio()

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

    # --------------------------------------------------------
    # Tell HX710B to enter 40 Hz mode
    # --------------------------------------------------------
    #
    # On power-up the first conversion may still be 10 Hz.
    # read_raw() sends 27 clocks, causing subsequent
    # conversions to run at 40 Hz.

    log.info(
        "Starting HX710B in 40 Hz differential mode"
    )

    first_reading = read_raw()

    log.info(
        "First raw reading: %d",
        first_reading
    )

    # --------------------------------------------------------
    # BASELINE WARMUP
    # --------------------------------------------------------

    log.info(
        "Learning pressure baseline for %d samples...",
        WARMUP_SAMPLES
    )

    warmup = []

    while len(warmup) < WARMUP_SAMPLES:

        try:

            raw = read_raw()

            warmup.append(
                raw
            )

        except TimeoutError as exc:

            log.warning(
                "%s",
                exc
            )

    baseline = float(
        statistics.median(
            warmup
        )
    )

    noise = float(
        statistics.pstdev(
            warmup
        )
    )

    # Prevent zero noise from making the adaptive
    # threshold meaningless.

    noise = max(
        noise,
        1.0
    )

    log.info(
        "Baseline learned: %.1f",
        baseline
    )

    log.info(
        "Initial noise: %.1f counts",
        noise
    )

    log.info(
        "Minimum drop threshold: %d counts",
        MIN_DROP_COUNTS
    )

    log.info(
        "Drop hold time: %.3f sec",
        DROP_HOLD_SECONDS
    )

    # Publish initial state

    client.publish(
        MQTT_TOPIC_BUBBLE,
        "0",
        retain=True
    )

    candidate_start = None
    clear_start = None

    last_telemetry = 0
    last_log = 0

    try:

        while True:

            try:

                # ============================================
                # GET NEXT 40 Hz SAMPLE
                # ============================================

                raw = read_raw()

                now = time.monotonic()


                # ============================================
                # DETECTION THRESHOLD
                # ============================================

                adaptive_threshold = (
                    noise
                    * NOISE_MULTIPLIER
                )

                threshold = max(
                    float(MIN_DROP_COUNTS),
                    adaptive_threshold
                )


                # ============================================
                # PRESSURE DROP
                # ============================================

                drop = calculate_drop(
                    baseline,
                    raw
                )


                # ============================================
                # NOT CURRENTLY IN A BUBBLE
                # ============================================

                if not bubble_active:

                    if drop >= threshold:

                        # Pressure has crossed the threshold.

                        if candidate_start is None:

                            candidate_start = now

                        held_for = (
                            now
                            - candidate_start
                        )

                        if held_for >= DROP_HOLD_SECONDS:

                            bubble_active = True

                            bubble_count += 1

                            clear_start = None

                            log.warning(
                                "BUBBLE DETECTED #%d "
                                "drop=%.0f "
                                "threshold=%.0f "
                                "raw=%d "
                                "baseline=%.0f "
                                "held=%.3fs",
                                bubble_count,
                                drop,
                                threshold,
                                raw,
                                baseline,
                                held_for
                            )

                            client.publish(
                                MQTT_TOPIC_BUBBLE,
                                "1",
                                retain=True
                            )

                            event = {
                                "event": "bubble",
                                "count": bubble_count,
                                "raw": raw,
                                "baseline": round(
                                    baseline
                                ),
                                "drop": round(
                                    drop
                                ),
                                "threshold": round(
                                    threshold
                                ),
                                "hold_seconds": round(
                                    held_for,
                                    3
                                )
                            }

                            client.publish(
                                MQTT_TOPIC_BUBBLE_EVENT,
                                json.dumps(
                                    event
                                ),
                                retain=False
                            )

                    else:

                        # Drop disappeared before hold time
                        candidate_start = None


                        # ------------------------------------
                        # BASELINE TRACKING
                        # ------------------------------------
                        #
                        # Only update baseline while we're in
                        # normal conditions.

                        error = (
                            raw - baseline
                        )

                        baseline += (
                            BASELINE_ALPHA
                            * error
                        )


                        # ------------------------------------
                        # NOISE TRACKING
                        # ------------------------------------

                        deviation = abs(
                            raw - baseline
                        )

                        noise += (
                            NOISE_ALPHA
                            * (
                                deviation
                                - noise
                            )
                        )

                        noise = max(
                            noise,
                            1.0
                        )


                # ============================================
                # BUBBLE ACTIVE
                # ============================================

                else:

                    release_level = (
                        threshold
                        * RELEASE_THRESHOLD_RATIO
                    )

                    if drop <= release_level:

                        if clear_start is None:

                            clear_start = now

                        recovered_for = (
                            now
                            - clear_start
                        )

                        if recovered_for >= CLEAR_HOLD_SECONDS:

                            bubble_active = False

                            candidate_start = None
                            clear_start = None

                            log.info(
                                "Bubble cleared "
                                "drop=%.0f raw=%d",
                                drop,
                                raw
                            )

                            client.publish(
                                MQTT_TOPIC_BUBBLE,
                                "0",
                                retain=True
                            )

                    else:

                        clear_start = None


                # ============================================
                # MQTT TELEMETRY
                # ============================================

                if (
                    now - last_telemetry
                    >= MQTT_TELEMETRY_INTERVAL
                ):

                    client.publish(
                        MQTT_TOPIC_RAW,
                        str(raw),
                        retain=True
                    )

                    client.publish(
                        MQTT_TOPIC_DROP,
                        f"{drop:.0f}",
                        retain=True
                    )

                    client.publish(
                        MQTT_TOPIC_THRESHOLD,
                        f"{threshold:.0f}",
                        retain=True
                    )

                    if calibration_enabled():

                        kpa = raw_to_kpa(
                            raw
                        )

                        client.publish(
                            MQTT_TOPIC_KPA,
                            f"{kpa:.4f}",
                            retain=True
                        )

                    last_telemetry = now


                # ============================================
                # CONSOLE STATUS
                # ============================================

                if now - last_log >= 1.0:

                    log.info(
                        "raw=%d "
                        "baseline=%.0f "
                        "drop=%.0f "
                        "threshold=%.0f "
                        "noise=%.0f "
                        "bubble=%d",
                        raw,
                        baseline,
                        drop,
                        threshold,
                        noise,
                        1 if bubble_active else 0
                    )

                    last_log = now


            except TimeoutError as exc:

                log.warning(
                    "%s",
                    exc
                )

            except Exception:

                log.exception(
                    "Pressure processing error"
                )

    finally:

        client.publish(
            MQTT_TOPIC_BUBBLE,
            "0",
            retain=True
        )

        client.loop_stop()

        client.disconnect()


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
