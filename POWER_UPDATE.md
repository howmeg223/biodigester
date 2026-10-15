# Peltier 30% PWM limit

Extract this update inside /home/pi/biodigester. It does not include config.py, so existing calibration, pinouts and display settings are preserved.

The updated scripts default to a 30% maximum even if the existing config.py lacks the new setting. To make the settings explicit, add PELTIER_MAX_POWER = 30.0 and change the existing PELTIER_HEAT_POWER to 30.0 in config.py.

Restart after installing:

```bash
sudo systemctl stop biodigester@controller.service
sudo systemctl restart biodigester@peltier.service
sudo systemctl start biodigester@controller.service
```

Positive commands heat, negative commands cool (subject to physical polarity). Commands above +30 or below -30 are capped. Lower commands are unchanged. Zero stops output. Automatic control still heats or turns off; this change does not add automatic cooling. Watchdog, safety latch and reversal delay remain enabled.

These percentages set PWM duty, not measured heat output or a regulated output voltage. MQTT requested_power records the original request; applied_power records the capped PWM duty.
