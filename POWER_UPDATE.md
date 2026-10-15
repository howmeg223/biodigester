# Peltier power configuration

Set `PELTIER_MAX_POWER = 30.0` in `config.py` for a 30% PWM duty limit in both directions.
Restart the Peltier and controller services after changing the configuration.
Automatic control heats at this limit or switches off; cooling remains manual.
The driver independently clamps all commands, so +100 becomes +30 and -100 becomes -30.
Smaller commands are preserved. Do not run the simulator alongside the physical driver.

The dashboard displays the driver-reported applied duty and configured maximum,
with saved/offline feedback marked stale. Requested duty may exceed the limit;
applied duty must not. PWM switches full supply voltage during each ON pulse,
so peak voltage alone does not measure duty cycle or average thermal power.

```bash
mosquitto_sub -h localhost -t biodigester/peltier/state -v -C 3
```

For a 30% setting, expect `max_power` 30 and `applied_power` between -30 and +30.
The software reports the duty submitted to GPIO; it does not measure current or
verify the electrical waveform. Existing watchdog, reversal delay and fault
latches remain active.

Use the normal repository files. The obsolete `payload/` directory and
`install_power_update.py` have been removed because they contained older source
that could overwrite the current implementation. No special updater is needed.
Keep your existing pressure calibration and pin settings in `config.py`.
