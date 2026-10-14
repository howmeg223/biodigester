# Wiring and hardware startup

BCM GPIO numbers and physical 40-pin header numbers are different. The tables
below show both. Power down before changing wires. Connect by signal label, not
by the order of pins on a module; board variants may reverse VCC and GND.

## DS18B20 waterproof temperature probe

| Probe signal | Pi signal | Physical header pin |
|---|---|---:|
| VDD | 3.3 V | 1 or 17 |
| GND | Ground | 6 (or another GND) |
| DQ / DATA | BCM4, default Linux 1-Wire | 7 |
| Pull-up resistor | 4.7 kOhm between DATA and 3.3 V | Between pins 7 and 1 |

Use three-wire powered operation. Verify the probe's wire colours against its
supplier pinout. Enable 1-Wire in `raspi-config`; the equivalent overlay is
`dtoverlay=w1-gpio,gpiopin=4` in the active boot config. Reboot, then check:

```bash
ls /sys/bus/w1/devices/28-*/temperature
cat /sys/bus/w1/devices/28-*/temperature
```

Values are millidegrees C, for example 36562 means 36.562 C. The script refuses
to silently choose between multiple probes; set `TEMP_SENSOR_GLOB` to the
correct sensor directory if more than one is connected. Keep your existing
working overlay if it uses another pin. `TEMP_GPIO_PIN` is documentation only;
the kernel overlay controls the actual pin.

## HX710B pressure module (with bridge pressure sensor)

| Module signal | Pi signal | Physical header pin |
|---|---|---:|
| VCC | 3.3 V, for a module supporting 3.3 V | 1 or 17 |
| GND | Ground | 6 (or another GND) |
| SCK / CLK / PD_SCK | BCM23 | 16 |
| OUT / DOUT / DT | BCM24 | 18 |

GPIO23/24 are the assignments recovered from the original working repository
script. Use the module's pressure port for measurement; keep liquid out of the
sensor. Verify your module's supply requirements. Pi inputs must not receive
5 V: if the module requires a 5 V supply, use appropriate signal level conversion
rather than connecting 5 V DOUT directly to the Pi. Changing supply voltage can
change the ADC calibration.

Start with `python -m pi.pressure --mode both`, then view diagnostics:

```bash
mosquitto_sub -h localhost -t 'biodigester/diagnostics/pressure/#' -v
mosquitto_sub -h localhost -t 'biodigester/sensors/pressure/#' -v
mosquitto_sub -h localhost -t 'biodigester/events/bubble' -v
```

### Single known-pressure calibration

The existing zero is retained. In `config.py`, set:

```python
PRESSURE_ZERO_RAW = 1339145  # verify at atmospheric pressure
PRESSURE_CAL_RAW = 2014370  # EXAMPLE ONLY: replace with raw at the known pressure
PRESSURE_CAL_KPA = 2.5      # EXAMPLE ONLY: replace with that actual gauge pressure
```

Restart the pressure process. Conversion is
`(raw - ZERO_RAW) * CAL_KPA / (CAL_RAW - ZERO_RAW)`.
Both rising and falling raw slopes are supported. The known pressure must be
non-zero and trustworthy; a sensor reporting an arbitrary estimate cannot
be calibrated accurately from that estimate. With `CAL_RAW` or `CAL_KPA` unset,
raw readings and bubble detection still operate but kPa is unavailable.
Bubble baseline is learned from operating samples and is separate from the
zero-pressure calibration.

## BTS7960 / IBT-2 H-bridge and Peltier

| Bridge terminal/signal | Connection | Pi header pin |
|---|---|---:|
| RPWM | BCM18 | 12 |
| LPWM | BCM19 | 35 |
| Logic GND | Common Pi ground | 6 (or another GND) |
| Logic VCC | Regulated 5 V appropriate for the module | 2 or 4 if powered from Pi 5 V |
| R_EN and L_EN | Logic HIGH per module requirements (commonly tied to logic VCC) | — |
| B+ / B- | External Peltier supply positive / negative | **Not Pi GPIO** |
| M+ / M- | Peltier two power leads | **Not Pi GPIO** |
| R_IS / L_IS | Unconnected; current feedback is not implemented | — |

GPIO18/19 are preserved from the original H-bridge script. Match the IBT-2
module's logic input requirements: if its input buffer does not accept 3.3 V HIGH,
use a 3.3 V-to-5 V logic buffer for RPWM/LPWM. Never route Peltier power through
the Pi's 5 V pins. Use a correctly rated fused external supply, appropriately
sized wiring and heat removal. All signal grounds must share a reference.
Add pull-downs on RPWM and LPWM so outputs remain off while the Pi boots or is
unpowered. Tied-high enable inputs are not an independent safety disconnect.

Positive commands drive RPWM; negative commands drive LPWM. The controller
currently requests only positive heating or zero. Verify at low power with
`controller.py` stopped that positive power heats the intended face; if it cools,
swap the Peltier power leads after powering down. Software cannot infer orientation.
Do not leave the bridge/Peltier unattended during that test.

The software latch turns both PWM outputs off, but cannot isolate a failed
bridge. Add a separate hardware temperature cut-out/power disconnect for
independent protection. No extra cut-out GPIO is claimed by this code.

## Jaycar XC3728 1.3-inch OLED (SH1106, 128x64)

Jaycar's sample manual selects `U8GLIB_SH1106_128X64` with clock, MOSI, CS and
A0 signals. The default driver here is therefore **SH1106 four-wire SPI**,
not SSD1306 I2C. No board modifications are needed for the normal SPI version.

| OLED signal label | Pi signal | Physical header pin |
|---|---|---:|
| VCC | 3.3 V | 1 or 17 |
| GND | Ground | 6 (or another GND) |
| CLK / SCK / D0 | BCM11 / SPI0 SCLK | 23 |
| MOSI / DIN / D1 | BCM10 / SPI0 MOSI | 19 |
| CS | BCM8 / SPI0 CE0 | 24 |
| DC / A0 | BCM25 | 22 |
| RES / RST | BCM27 | 13 |

Use the printed labels on your exact board. BCM24 is already HX710B OUT, so the
OLED deliberately uses BCM25 for DC rather than luma's default BCM24. BCM27 is
OLED reset. SPI0 clock/MOSI/CE0 do not overlap with the temperature, pressure or
H-bridge assignments.

Enable SPI in `raspi-config`, reboot, and verify `/dev/spidev0.0` exists.
Install `requirements-pi.txt`, then run `python -m pi.oled`.
The SPI driver uses an `lgpio` adapter for DC/reset so it does not depend on the
old `RPi.GPIO` backend on a Pi 5. Hardware SPI carries display data.
Set `OLED_ROTATE = 2` if the display is upside down.

If your particular board has already been converted to I2C, set
`OLED_INTERFACE = "i2c"`, wire SDA to BCM2 (physical 3), SCL to BCM3 (physical 5),
and configure its detected 7-bit address in `OLED_I2C_ADDRESS` (usually 0x3C).
The software setting does not change the physical interface jumpers.

## Initial checks

1. Confirm the configured GPIO chip and all pin labels before powering up.
2. Start sensor clients and verify temperature, raw pressure and hardware health.
3. Enter real calibration values if using kPa; otherwise leave calibration unset.
4. Check the detector against actual bubbles and tune its count thresholds/timers.
5. Start the bridge service with the controller stopped; it must remain off.
6. Check heating polarity at low power, then stop output before starting control.
7. Start the controller. Disconnect temperature data and verify heating stops.
8. Stop the controller and verify zero output; test timeout/reset using a safe load.
9. Start dashboard/OLED and confirm that disconnected sensors show stale status.

These are physical acceptance checks; automated mocked GPIO tests cannot perform them.

References:
- [Jaycar XC3728 sample manual](https://media.jaycar.com.au/product/resources/XC3728_manualMain_92743.pdf)
- [luma OLED hardware and SPI wiring](https://luma-oled.readthedocs.io/en/latest/hardware.html)
- [AVIA HX710 datasheet](https://www.electronicscomp.com/datasheet/hx710b-ic-datasheet.pdf)
- [Adafruit DS18B20 wiring](https://learn.adafruit.com/adafruits-raspberry-pi-lesson-11-ds18b20-temperature-sensing/hardware)
