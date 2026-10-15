# Dashboard update

Use the current repository files together. Preserve your local `config.py`,
`service.env`, virtual environment and `data/` directory when updating.
Do not use the obsolete Peltier installer or its `payload/` copies.

With the updated source in `~/biodigester`:

```bash
cd ~/biodigester
sudo apt install liblgpio-dev swig python3-dev build-essential
.venv/bin/python -m pip install -r requirements-pi.txt
sudo systemctl reset-failed
sudo systemctl restart biodigester.target
```

Reload the dashboard browser page after restarting. If you manage instances
individually instead of using the target, restart the controller, Peltier,
dashboard and OLED instances that you have enabled. Run only one copy of each.
The dashboard binds to `DASHBOARD_HOST` and `DASHBOARD_PORT` from `config.py`;
the current defaults permit remote access on port 8050.

CSV write failures now appear on the dashboard without stopping live MQTT
reception. Fix the reported disk, permission or schema problem to resume logging;
readings missed while storage was unavailable are not backfilled.
Malformed archive files produce a graph error rather than breaking the page.
Existing legacy readings CSV headers are migrated when the next live reading
arrives. Archive files are never deliberately replaced with empty files.

The dashboard must remain running to record data, but no browser tab is needed.
Sampling pause keeps raw logging and control active while excluding disturbed
measurements from analysis. Manual pH remains included. Resume can include a
settling period and requests pressure baseline relearning. Temperature and
pressure history graphs show gaps across sampling periods and missing data.
Bubble reports count unique events per interval; missing coverage is not zero.

If the dashboard still fails, collect:

```bash
sudo journalctl -u biodigester@dashboard.service -b -n 80 --no-pager
```

Also report whether the page cannot connect, remains blank, or displays a
callback error. This distinguishes server startup from browser/runtime errors.
