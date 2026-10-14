"""File-based reports and interval bubble rates; never reads live card values."""
from datetime import datetime
from pathlib import Path
import math

import pandas as pd
import config


def read_table(path, columns):
    try:
        data = pd.read_csv(Path(path))
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=columns)
    if not set(columns).issubset(data.columns):
        raise ValueError(f"Unexpected columns in {Path(path).name}")
    if "timestamp" in data:
        data["timestamp"] = pd.to_datetime(data["timestamp"], format="mixed", errors="coerce")
        data = data.dropna(subset=["timestamp"]).sort_values("timestamp", kind="stable")
    return data


def sampling_intervals(markers, end=None):
    end = pd.Timestamp(end or datetime.now())
    intervals = []
    opened = None
    for row in markers.itertuples():
        if row.event == "PAUSE":
            if opened is None:
                opened = row.timestamp
        elif row.event == "RESUME" and opened is not None:
            resume = pd.to_datetime(getattr(row, "resume_at", None), errors="coerce")
            if pd.isna(resume): resume = row.timestamp
            if resume > opened: intervals.append((opened, resume))
            opened = None
    if opened is not None: intervals.append((opened, end))
    return intervals


def analysis_readings(data, intervals, include_sampling=False):
    data = data.copy()
    data["value"] = pd.to_numeric(data["value"], errors="coerce")
    data = data[data["value"].notna() & data["value"].map(math.isfinite)]
    if "source" not in data: data["source"] = "unknown"
    if "excluded" not in data: data["excluded"] = False
    flagged = data["excluded"].astype(str).str.lower().isin(["true", "1"])
    for start, end in intervals:
        flagged |= (data["timestamp"] >= start) & (data["timestamp"] < end)
    # Manual pH samples are the purpose of opening the vessel, so keep them.
    flagged &= ~((data["sensor"] == "ph") & (data["source"] == "manual"))
    data["excluded"] = flagged
    if not include_sampling: data = data[~flagged]
    return data


def coverage_intervals(coverage, end):
    intervals = []
    rows = list(coverage.itertuples())
    for i, row in enumerate(rows):
        if str(row.available).lower() not in ("true", "1"): continue
        stop = min(row.timestamp + pd.Timedelta(seconds=config.PRESSURE_STALE_AFTER_S), end)
        if i+1 < len(rows): stop = min(stop, rows[i+1].timestamp)
        if stop <= row.timestamp: continue
        if intervals and row.timestamp <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(stop, intervals[-1][1]))
        else: intervals.append((row.timestamp, stop))
    return intervals


def bubble_rate(events, coverage, start, end, minutes=1, exclusions=()):
    """Count unique events in fixed bins and divide by bin duration in minutes.

    Missing coverage or a sampling interval produces NaN, never invented zeros.
    Old event-only files can show counts where events exist, but do not establish
    zero-bubble coverage. No straight-line interpolation across outages is done.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start >= end: raise ValueError("Start must be before end")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes < 1:
        raise ValueError("Bubble interval must be at least one minute")
    width = pd.Timedelta(minutes=minutes)
    if (end-start)/width > getattr(config, "REPORT_MAX_BINS", 20000):
        raise ValueError("Time range is too large for that bubble interval; choose a larger interval or shorter range")
    edges = pd.date_range(start=start.floor(width), end=end, freq=width)
    events = events.copy()
    if not events.empty:
        events = events.drop_duplicates(["session", "count"])
        events = events[(events.timestamp >= start) & (events.timestamp < end)]
        counts = events.groupby(events.timestamp.dt.floor(width)).size()
    else: counts = pd.Series(dtype=int)
    known = coverage_intervals(coverage, end)
    index = 0
    rows = []
    for edge in edges:
        left, right = max(start, edge), min(end, edge+width)
        if right <= left: continue
        seconds = (right-left).total_seconds()
        while index < len(known) and known[index][1] < right and known[index][1] <= left:
            index += 1
        available = index < len(known) and known[index][0] <= left and known[index][1] >= right
        paused = any(a < right and b > left for a,b in exclusions)
        count = int(counts.get(edge, 0))
        # Old files have event evidence but cannot establish quiet intervals.
        legacy = coverage.empty and count > 0
        value = count * 60.0 / seconds if not paused and (available or legacy) else math.nan
        rows.append({"timestamp": left, "sensor": "bubble_rate", "value": value,
                     "count": count, "interval_seconds": seconds,
                     "source": "events" if available else "events (coverage unknown)", "excluded": paused})
    return pd.DataFrame(rows)
