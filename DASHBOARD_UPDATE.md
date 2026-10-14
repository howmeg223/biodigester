# Installing the dashboard analysis update

Use `biodigester-dashboard-update.zip` for a Pi already running the fixed
hardware version. The update ZIP replaces application files only; it contains
neither `config.py` nor `data/`, so your calibration, broker and pin settings,
and all recorded data, are preserved.

1. Stop the dashboard and your existing pressure client.
2. Extract the ZIP contents directly into `~/biodigester`, replacing files.
3. Activate your existing virtual environment:

   ```bash
   cd ~/biodigester
   source .venv/bin/activate
   ```

4. Restart the pressure client in its own terminal using your normal mode, e.g.
   `python -m pi.pressure --mode both`. Run only one pressure client.
5. Restart the dashboard with `python dashboard.py` and reload the browser page.

No new Python dependencies are needed beyond the project's existing requirements.
The new optional config settings have backward-compatible defaults:

```python
SAMPLING_SETTLE_S = 60
REPORT_MAX_BINS = 20000
TOPIC_BUBBLE_RESET = f"{ROOT}/sensors/bubble/reset"
```

Pressure baseline relearning requires restarting the updated pressure client;
the dashboard reports a request, not confirmation that the hardware completed it.

The archive is migrated when the first new live reading arrives. Existing CSV
rows are preserved. Keep one dashboard/logger process per archive directory;
multiple browser tabs share the same sampling state and are supported.

The dashboard must remain running to log MQTT data. Keeping a browser tab open
is not necessary. Sampling pause marks data for exclusion rather than turning
recording or temperature control off.
