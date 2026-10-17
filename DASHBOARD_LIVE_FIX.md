# Dashboard live-data fix: live-archive-v3

Based on GitHub main `dd7501753315959f4372e0e3362cf00919a337a1`.

## Install all supplied files together

The ZIP contains replacement source files in their repository folders. Upload
them to GitHub preserving those folders, or extract them into the Pi checkout.
In particular, `monitoring/archive_cache.py` is a new required file.
There is no installer and no new dependency.

The package contains no `config.py`, sensor drivers, service definitions or CSV
data. Existing calibration, power limits, GPIO settings and archives are preserved.

After uploading the files to GitHub, run on the Pi:

```bash
cd /home/pi/biodigester
git pull --ff-only
grep -n 'DASHBOARD_VERSION =' dashboard.py
sudo systemctl restart biodigester@dashboard.service
sudo journalctl -u biodigester@dashboard.service --since '2 minutes ago' --no-pager
```

The grep output and dashboard page must show `live-archive-v3`. Startup logs also
print that version, the source path and sensor CSV path. Close old dashboard tabs
and reopen the page, or use Ctrl+Shift+R. Old tabs can continue requesting callbacks
from the previous dashboard version until reloaded.

If git pull reports conflicting local changes, keep them and inspect the conflict;
do not reset the checkout or remove data to force the update.

## Behaviour

* Live cards, pH status, controller state and fault displays update every two seconds
  without reading the sensor CSV.
* History and pH graphs refresh every 30 seconds. Selecting a different range also
  refreshes a graph. Reports refresh when requested or their options change.
* Parsed archive tables are shared between browsers and cached for ten seconds.
  Unchanged files are not reparsed. Generate/refresh report and CSV downloads check
  for newer data immediately.
* Archive parsing never holds the MQTT live-state lock. A separate file lock
  protects copying complete CSV rows; parsing happens after releasing that lock.
* A running monitoring service queues sensor, fault, bubble and coverage records
  for a dedicated CSV writer, preserving arrival order. Slow storage does not stop
  live MQTT updates. Direct use of a monitor before start() remains synchronous.
* Write failures, a write stalled for over ten seconds, a full queue, and MQTT
  processing failures appear in the logging-status area. Queue-overflow warnings
  remain visible because affected measurements were not saved. Receiver exceptions
  are logged with their topic and do not terminate message reception.
* The writer uses a bounded 10,000-record memory queue and attempts to drain it
  during graceful shutdown for up to five seconds. This is not a durable spool;
  queued records can be lost during a power failure or forced shutdown.
* pH sampling markers are saved before changing sampling state. Reports continue
  to exclude marked periods by default; live monitoring and control continue.
* Retained MQTT readings remain labelled as saved values and are not logged again.

Optional overrides can be added to the existing config.py; none are required:

```python
DASHBOARD_GRAPH_REFRESH_S = 30
DASHBOARD_ARCHIVE_CACHE_S = 10
MONITOR_LOG_QUEUE_SIZE = 10000
```

## Check reception on the Pi

Keep the real temperature service running. After the dashboard starts, wait a few
seconds and run the command below twice. Its last timestamp should advance:

```bash
awk -F, '$2 == "temperature" {latest=$0} END {print latest}' \
  /home/pi/biodigester/data/readings.csv
```

If live cards advance but the CSV does not, the logging-status area now identifies
the writer error or backlog. Check errors from the current service run with:

```bash
invocation=$(systemctl show biodigester@dashboard.service --property=InvocationID --value)
sudo journalctl "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager -o cat |
  grep -v 'HTTP/1.1" 200' | tail -n 100
```

## Validation

Full suite: 228 tests passed with Dash 4.4.1, pandas 3.0.6 and Plotly 6.9.0.
The 36 dashboard, archive, concurrency and real MQTT integration tests also pass
with Dash 3.2.0, pandas 2.2.3 and Plotly 6.3.0.

Tests cover delayed archive parsing, blocked CSV writes, queue overflow, writer
failures and recovery, MQTT callback failures and recovery, retained readings,
explicit report refresh, and separating graph timers from live callbacks.

A real private MQTT broker test sends 30 decimal temperature messages while three
concurrent Dash HTTP graph requests process a 350,000-row archive. All graph
responses succeed, the live value reaches the final message, and all 30 readings
are appended in order. This does not access the Pi broker or physical hardware.

Control tests temporarily fix their predefined test thresholds and timeout in
pytest fixtures, including constructor defaults bound at import. The operator's
production config.py is never modified to satisfy a test scenario.

Tests run under Python 3.12; Python 3.14 and the Raspberry Pi deployment must be
verified on the actual Pi.
