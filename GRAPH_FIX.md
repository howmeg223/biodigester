# Dashboard archive graph fixes — 17 October 2026

Based on fresh GitHub main e2aa2548e225d9245177b750fec3b035e676b351.
Only the latest repository code and its committed data were used as the code
baseline. The earlier attached sensor CSV was additionally checked read-only.

## Confirmed causes

1. The committed bubble coverage archive contains both 28 July and October
   timestamps. All data with a fixed one-minute interval exceeded the 20,000-bin
   limit and returned a report error. No archive dates have been deleted.
2. History and reports defaulted to Last hour. Older data therefore disappeared
   from view after downtime, even when still present in the CSV files.
3. Graphs used lines without markers. A single reading, or isolated readings
   separated by gaps, could be present in the graph data but invisible.
4. Partial coverage erased recorded event counts from plots: in the October
   data, 4,938 of 13,354 timestamped events belonged to minute bins rendered NaN.
5. History plots retained the same zoom identity when changing time ranges,
   allowing a previous zoom to keep the newly selected data out of view.
6. GitHub contains bubble_events.csv and bubble_coverage.csv but NOT readings.csv.
   The latter is excluded by .gitignore and is a local runtime archive. This does
   not establish whether it is missing on your Pi; its presence must be checked.

## Result

- Default to Latest logged 24 hours, anchored to the selected archive's last
  timestamp rather than startup time or the current wall clock. pH uses the pH
  archive dates. Existing Last hour/day/week wall-clock ranges remain available.
- All data includes the last recorded timestamp, even if the current system
  clock is earlier. Custom ranges retain their requested bounds.
- Automatic bubble intervals keep long archives within the bin limit; the
  actual interval is shown in the report title and interval_seconds CSV column.
  An explicitly selected interval still reports an error if too fine.
- Show actual logged bubble events during incomplete coverage as recorded-count
  lower bounds. Hover text and the coverage_confirmed CSV column identify them.
  Unknown quiet intervals stay gaps; complete quiet coverage can establish zero.
  Sampling exclusions remain active. No events are invented or interpolated.
- Show markers alongside lines and reset the previous plot zoom when the user
  changes history range. Tick updates within a range preserve user zoom.
- Display the configured sensor archive path when the file is missing/empty.
- Tests specify their own 30% power scenario instead of assuming the operator's
  config. The current production PELTIER_MAX_POWER = 10 remains unchanged; an
  explicit 10% control scenario is covered too.

## Install

Extract the update ZIP and upload its contents at the repository root on GitHub,
preserving dashboard.py, monitoring/ and tests/ paths. Get the updated files onto
the Pi through your normal pull/copy process. There is no custom installer and
no dependency, config, data, pinout, calibration or hardware-driver replacement.

Keep your existing data directory. Do not replace it with an empty copy.
The two runtime files that must be updated together are dashboard.py and
monitoring/reporting.py. Tests and this document are included for review.

Then run on the Pi:

```bash
ls -lh /home/pi/biodigester/data/readings.csv
sudo systemctl restart biodigester@dashboard.service
```

Reload the browser page (Ctrl+Shift+R). History defaults to Latest logged 24 hours.
Use All data to inspect the full archive and Automatic for the bubble interval.

If the first command says the file is missing, restore your original readings.csv
at that path. Temperature, pressure and pH histories come from this file;
copying code from GitHub cannot reconstruct its historical contents. New live
readings will create a new file, but do not recover the older measurements.
Do not overwrite a newer existing archive with an older copy.

For a remaining failure, provide:

```bash
sudo journalctl -u biodigester@dashboard.service -b -n 80 --no-pager
```

Also include the range/sensors selected and any on-screen error. Malformed CSV
schema, sampling marker errors and disk-write failures are still reported.

## Validation

- 218 tests passed with Dash 4.4.1/pandas 3.0.6/Plotly 6.9.0 on Python 3.12.
- The 216-test suite before the two final compatibility/zoom regressions passed
  with Dash 3.2.0/pandas 2.2.3/Plotly 6.3.0; the final archive/dashboard subset
  was repeated with those dependencies after the last edits.
- Actual GitHub All data report: 11,712 bins at 10-minute resolution; all 13,354
  valid timestamped event records accounted for and visible. Latest logged day:
  1,441 bins at one-minute resolution, including 7,857 event records.
- Actual Dash report callback requests returned HTTP 200 and no report error,
  both using GitHub's bubble archive alone and supplemented read-only with the
  previously attached readings.csv. Temperature and pressure histories contained
  thousands of finite plotted values from that sensor archive.
- Sampling, unknown quiet coverage, missing archives, single-point visibility,
  newest archived timestamps and optional config compatibility have regressions.
- Existing real MQTT/control/driver tests pass with mocked GPIO. Hardware outputs
  and Pi Python 3.14 have not been bench-tested here. Dependency deprecation
  warnings remain; none caused test failures.

This addresses graph loading/display. The previously diagnosed bubble-detector
rearming issue can still cause real missed detections, which these graph fixes
cannot reconstruct. No changes were pushed to GitHub or installed on the Pi by
this review.
