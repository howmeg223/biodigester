# Run automatically at boot with systemd

systemd runs `run-services.sh` once for each process. The instances restart
independently, so a dashboard or OLED failure does not stop temperature control.
`biodigester.target` groups the core instances under one start/stop command.
The service configuration below assumes user `pi` and `/home/pi/biodigester`.

## Install

Stop any copies you started manually before enabling the services. In
particular, do not run duplicate GPIO clients or duplicate dashboard loggers.

Copy the service update files into your repository, then run:

```bash
cd ~/biodigester
sudo apt install mosquitto python3-venv python3-dev build-essential swig liblgpio-dev
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-pi.txt
# Add pi to the groups owning the GPIO/SPI devices (see note below).
sudo cp deploy/biodigester@.service deploy/biodigester.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mosquitto.service
sudo systemctl enable --now biodigester.target
```

No interactive virtual-environment activation is needed: the script uses
`/home/pi/biodigester/.venv/bin/python` directly. The system service runs as `pi`,
so existing CSV files must be writable by that user. SPI and 1-Wire must already
be enabled, and the hardware must be wired and configured as in HARDWARE.md.
Check `ls -l /dev/gpiochip* /dev/spidev*` and add `pi` to the owning device
groups with `sudo usermod -aG GROUP pi`. Raspberry Pi OS commonly uses `gpio`
and `spi`; your Ubuntu SPI devices may use `dialout`. Only add groups that exist.
If you use I2C for the OLED, also give `pi` membership in the `i2c` group.
Group membership is loaded when each system service starts.

The target starts temperature, pressure, Peltier, controller and dashboard.
Enable the optional OLED once the screen and its dependencies are ready:

```bash
sudo systemctl enable --now biodigester@oled.service
```

This adds OLED to the target for future boots. Stop/disable it independently with
`sudo systemctl disable --now biodigester@oled.service` if needed. The dashboard
serves the same port 8050 and continues logging even without browser tabs open.

## Check and manage

```bash
# Check every process: an active target alone does not prove all instances work.
systemctl status biodigester@{temperature,pressure,peltier,controller,dashboard}.service

# View logs, or follow a particular process.
journalctl -u 'biodigester@*.service' -n 100 --no-pager
journalctl -u biodigester@dashboard.service -f

# Stop, start or restart the whole application, including enabled OLED.
sudo systemctl stop biodigester.target
sudo systemctl start biodigester.target
sudo systemctl restart biodigester.target

# Restart only the dashboard after editing its code.
sudo systemctl restart biodigester@dashboard.service

# Prevent startup at boot and stop now.
sudo systemctl disable --now biodigester.target
```

SIGTERM runs the Python clients' cleanup; the Peltier outputs are forced off.
An unresponsive process is forcibly stopped after 15 seconds. A failed instance
restarts after five seconds, with a limit of ten starts per minute. Once you fix
a repeatedly failing instance, run `sudo systemctl reset-failed` and restart it.
The existing actuator watchdog/latch still applies. A controller or broker
failure may require an operator acknowledgement before heating resumes; systemd
does not automatically bypass the safety latch.

## Optional pressure mode override

With no override, the pressure client uses your existing `config.py` setting.
To override it for service startup, create `service.env` in the repository:

```text
PRESSURE_MODE=both
```

Use `pressure`, `bubble`, or `both`, then restart
`biodigester@pressure.service`. Do not put `export` before the variable.

If your username or installation folder differs, update `User`,
`WorkingDirectory`, `EnvironmentFile` and `ExecStart` in the installed template.
Then run `sudo systemctl daemon-reload` and restart the target.
