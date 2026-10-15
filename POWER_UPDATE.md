# Verified Peltier 30% update (pwm-cap-v2)

Copy biodigester-power-fix-v2.zip into /home/pi/biodigester and run:

```bash
cd ~/biodigester
unzip -o biodigester-power-fix-v2.zip -d /tmp/biodigester-power-fix-v2
.venv/bin/python /tmp/biodigester-power-fix-v2/install_power_update.py --project "$PWD"
```

Use the installer, rather than copying individual files. The shared helper, controller, fault definitions and GPIO driver must match.

The installer backs up replaced files, changes only PELTIER_HEAT_POWER and PELTIER_MAX_POWER in your existing config.py to 30.0, and preserves pressure calibration, temperature settings, OLED rotation and pinouts. It checks the configured systemd WorkingDirectory before changing files. It stops the controller and Peltier services, installs the scripts, removes their old bytecode caches, starts the driver and verifies its live version/path/limit before starting the controller. It then verifies fresh controller and driver reports and restarts an active dashboard. If verification fails, it stops both control services and prints the reason and backup directory. It does not send manual power commands or clear safety latches over MQTT.

Live verification uses your configured MQTT host, port, credentials and topics. Saved broker messages do not count as evidence of a successful install. It checks software reports, not physical current or voltage. A verified idle/fault state is allowed; the installer does not force heating for a test.

## Result

- Automatic heating requests 30% PWM duty. Automatic regulation still heats or switches off; cooling remains manual.
- The driver independently caps all commands: +100 becomes +30, -100 becomes -30, and smaller commands remain unchanged.
- Controller feedback checks use the capped command, avoiding false mismatch faults.
- Zero and sub-deadband limits keep heating off without false failed-to-switch-on faults.
- Invalid limits are rejected before GPIO startup. Driver application errors latch output off.
- If feedback reports duty above the configured cap, control sends zero and trips the software latch on its next cycle without waiting for the normal mismatch delay.
- Live MQTT state includes driver_version, max_power, driver_file, config_file and pwm_frequency_hz. The dashboard shows reported PWM duty, maximum and simulator/hardware source.
- Both signed directions, reversal delay, watchdog, safety latch and invalid commands are covered by tests. The full suite passed 220 tests, including a real local MQTT broker driving the actual Pi driver callbacks with mocked GPIO.

## Verify later

```bash
mosquitto_sub -h localhost -t biodigester/peltier/state -v -C 3
```

Expect driver_version "pwm-cap-v2", max_power 30.0 and applied_power between -30 and +30. requested_power can exceed the cap if a manual client sends a larger request; applied_power is the duty the driver submitted to GPIO. Current and actual PWM waveform are not measured by this software. PWM still switches full supply voltage during each ON pulse; a peak-voltage measurement alone does not establish duty cycle.

To run the installer checks again without changing files:

```bash
.venv/bin/python /tmp/biodigester-power-fix-v2/install_power_update.py --project "$PWD" --verify-only
```

If installation fails, include its output and these logs when reporting the problem:

```bash
sudo journalctl -u biodigester@peltier.service -u biodigester@controller.service -b -n 60 --no-pager
```

This ZIP has not been installed remotely on your Pi. It does not depend on pulling changes from GitHub.
