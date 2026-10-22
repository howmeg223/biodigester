"""Dash UI for observing the biodigester controller.

Safety decisions do not live here. The dashboard receives controller state,
presents fault notifications/history, and sends operator requests such as range
changes, acknowledgements, or simulator-only fault injections.
"""
import argparse
import logging
import math
from datetime import datetime, timedelta
import signal
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from plotly.subplots import make_subplots

import config
from monitoring.monitoring_service import MonitoringService
from monitoring.archive_cache import ArchiveCache
from monitoring.ph_monitor import classify, validate_limits
from monitoring.reporting import read_table, sampling_intervals, analysis_readings, bubble_rate
from mqtt_common import stop_requested

monitor = MonitoringService()
lock = monitor.lock  # Compatibility for callers; archive work never acquires it.
archive_cache = ArchiveCache(getattr(config, "DASHBOARD_ARCHIVE_CACHE_S", 10))
DASHBOARD_VERSION = "live-archive-v4"

RANGES = {
    "Latest logged 24 hours": "latest",
    "Last 15 minutes": 15,
    "Last hour": 60,
    "Last 6 hours": 360,
    "Last 24 hours": 1440,
    "Last 7 days": 10080,
    "All data": None,
}


REPORT_SENSORS = {**config.SENSORS, "bubble_rate": {"label": "Bubble rate", "unit": "bubbles/min"}}


def read_analysis_files(include_bubbles=True, refresh=False):
    def load(path, columns):
        return archive_cache.read(path, columns,
            lambda path, columns: read_table(path, columns, file_lock=monitor.logger.file_lock), refresh)
    # Never acquire monitor.lock while reading or parsing archives. It protects
    # the live MQTT state, not potentially large files.
    readings = load(monitor.log_file, ["timestamp", "sensor", "value"])
    markers = load(monitor.sampling_file, ["timestamp", "event"])
    events = (load(monitor.bubble_file, ["timestamp", "session", "count"])
              if include_bubbles else pd.DataFrame(columns=["timestamp", "session", "count"]))
    coverage = (load(monitor.coverage_file, ["timestamp", "available"])
                if include_bubbles else pd.DataFrame(columns=["timestamp", "available"]))
    return readings, events, markers, coverage


def load_df(minutes=None, include_sampling=False, keys=None):
    return report_data(keys or list(config.SENSORS), minutes,
                       include_sampling=include_sampling)[0]


def report_data(keys, minutes=None, start=None, end=None, interval="auto", include_sampling=False, refresh=False):
    readings, events, markers, coverage = read_analysis_files("bubble_rate" in keys, refresh)
    selected = readings[readings.sensor.isin(keys)]
    archives = (selected, events, coverage) if "bubble_rate" in keys else (selected,)
    archives = [frame for frame in archives if not frame.empty]
    latest = max((frame.timestamp.max() for frame in archives), default=None)
    earliest = min((frame.timestamp.min() for frame in archives), default=None)
    # Archive modes follow recorded timestamps, independent of process restart
    # time or a wall clock that has since moved. Add one microsecond so the
    # newest observation is included in the half-open interval.
    archive_mode = start is None and minutes in (None, "latest")
    if end is None:
        end = latest + pd.Timedelta(microseconds=1) if archive_mode and latest is not None else datetime.now()
    end = pd.Timestamp(end)
    if start is None:
        if minutes == "latest":
            start = end - pd.Timedelta(hours=24)
        elif minutes:
            start = end - pd.Timedelta(minutes=minutes)
        else:
            start = earliest if earliest is not None else end - pd.Timedelta(hours=1)
    start = pd.Timestamp(start)
    if start >= end:
        raise ValueError("Start must be before end")
    periods = sampling_intervals(markers, end)
    # Select before copying/converting analysis metadata, especially for the
    # pH graph, which otherwise processes every pressure/temperature sample.
    df = analysis_readings(selected[(selected.timestamp >= start) & (selected.timestamp < end)],
                           periods, include_sampling)
    df = df[df.sensor.isin(keys) & (df.timestamp >= start) & (df.timestamp < end)]
    effective_interval = None
    if "bubble_rate" in keys:
        coverage = coverage[(coverage.timestamp >= start-pd.Timedelta(seconds=config.PRESSURE_STALE_AFTER_S)) & (coverage.timestamp <= end)]
        rate = bubble_rate(events, coverage, start, end, interval,
                           () if include_sampling else periods)
        effective_interval = rate.attrs["bubble_interval_minutes"]
        df = pd.concat([df, rate], ignore_index=True)
    df = df.sort_values("timestamp", kind="stable")
    df.attrs["bubble_interval_minutes"] = effective_interval
    return df, periods, start, end


def graph_samples(data, key, periods, start, end, include_sampling):
    if key == "ph" or data.empty:
        return data
    data = data.copy()
    original_stamps = data.timestamp.sort_values()
    if key != "bubble_rate" and len(data) > 5000:
        seconds = max(1, math.ceil((data.timestamp.max()-data.timestamp.min()).total_seconds()/5000))
        data = data.set_index("timestamp")[["value"]].resample(f"{seconds}s").mean().reset_index()
        data["source"] = f"{seconds}-second mean (CSV export retains individual samples)"
    # Explicit gaps stop Plotly drawing straight lines through excluded periods.
    gaps = []
    if not include_sampling:
        for a,b in periods:
            if a < end and b > start:
                gaps.append({"timestamp": max(a,start), "value": float("nan"), "source": "sampling"})
    limit = config.TEMP_STALE_AFTER_S if key == "temperature" else config.PRESSURE_STALE_AFTER_S
    if key != "bubble_rate":
        for previous in original_stamps.shift(1)[original_stamps.diff().dt.total_seconds() > limit]:
            gaps.append({"timestamp": previous + pd.Timedelta(microseconds=1), "value": float("nan"), "source": "data gap"})
    if key == "bubble_rate" and not data.empty and data.timestamp.iloc[-1] < end:
        final = data.iloc[-1].copy()
        final["timestamp"] = end
        data = pd.concat([data, pd.DataFrame([final])], ignore_index=True)
    if gaps:
        data = pd.concat([data, pd.DataFrame(gaps)], ignore_index=True).sort_values("timestamp", kind="stable")
    return data


def empty_fig(message):
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 16})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(height=350, margin={"t": 20})
    return fig


def history_fig(key, minutes, limits=None):
    """Plot one sensor's history with temperature limits where applicable."""
    df, periods, start, end = report_data([key], minutes)
    if key == "ph":
        return ph_figure(df, limits).update_layout(uirevision=f"ph:{minutes}")
    if df.empty:
        if not monitor.log_file.exists():
            return empty_fig(f"Sensor CSV missing: {monitor.log_file}")
        return empty_fig("No logged data for this sensor in the selected range")
    sensor = config.SENSORS[key]
    df = graph_samples(df, key, periods, start, end, False)
    fig = go.Figure(go.Scatter(x=df["timestamp"], y=df["value"], mode="lines+markers",
                              marker={"size": 3}, connectgaps=False))
    fig.update_layout(height=380, margin={"t": 20},
                      yaxis_title=f"{sensor['label']} ({sensor['unit']})", uirevision=f"{key}:{minutes}")
    if key == "temperature":
        status = monitor.controller_status()
        if "low" in status and "high" in status:
            fig.add_hrect(y0=status["low"], y1=status["high"], opacity=0.12,
                          line_width=0, annotation_text="Normal control band")
        fig.add_hline(y=config.HIGH_TEMP_C, line_dash="dash", annotation_text="High")
        fig.add_hline(y=config.HIGH_HIGH_TEMP_C, line_dash="dot", annotation_text="High-high")
    return fig


def report_fig(keys, minutes, limits=None, start=None, end=None, interval="auto", include_sampling=False, refresh=False):
    """Compare CSV histories on separate axes, including calculated bubble rates."""
    keys = [key for key in dict.fromkeys(keys or []) if key in REPORT_SENSORS]
    if not keys:
        return empty_fig("Tick at least one sensor")
    df, periods, start, end = report_data(keys, minutes, start, end, interval, include_sampling, refresh)
    if df.empty or not df.value.notna().any():
        return empty_fig("No logged data or confirmed bubble coverage in this time range")
    fig = make_subplots(rows=len(keys), cols=1, shared_xaxes=True, vertical_spacing=0.06)
    for row, key in enumerate(keys, start=1):
        data = graph_samples(df[df.sensor == key], key, periods, start, end, include_sampling)
        sensor = REPORT_SENSORS[key]
        fig.add_trace(go.Scatter(x=data["timestamp"], y=data["value"],
                                 mode="markers" if key == "ph" else "lines+markers",
                                 marker=ph_markers(data, limits) if key == "ph" else {"size": 3},
                                 customdata=data["source"], connectgaps=False,
                                 line={"shape": "hv"} if key == "bubble_rate" else None,
                                 hovertemplate="%{x}<br>%{y:.3f}<br>%{customdata}<extra></extra>",
                                 name=sensor["label"]), row=row, col=1)
        if key == "ph":
            add_ph_limits(fig, limits, row=row, col=1)
        fig.update_yaxes(title_text=f"{sensor['label']} ({sensor['unit']})", row=row, col=1)
        for a,b in periods:
            if a < end and b > start:
                fig.add_vrect(x0=max(a,start), x1=min(b,end), fillcolor="#f59e0b", opacity=.15,
                              line_width=0, row=row, col=1)
    fig.update_xaxes(range=[start, end])
    actual_interval = df.attrs.get("bubble_interval_minutes")
    interval_label = f" · bubble interval {actual_interval:g} min" if actual_interval is not None else ""
    fig.update_layout(height=260 * len(keys) + 80, margin={"t": 30}, showlegend=False,
                      title=f"CSV report{interval_label} · sampling periods {'included' if include_sampling else 'excluded'}")
    return fig


def ph_limits(limits):
    if not isinstance(limits, dict) or validate_limits(limits.get("low"), limits.get("high")):
        return config.DEFAULT_PH_MIN, config.DEFAULT_PH_MAX
    return limits["low"], limits["high"]


def ph_markers(data, limits):
    low, high = ph_limits(limits)
    return {"size": 9, "color": [classify(v, low, high)[1] for v in data["value"]]}


def add_ph_limits(fig, limits, **kwargs):
    low, high = ph_limits(limits)
    for value, label, color in ((low, "Minimum", "#b45309"), (high, "Maximum", "#b3261e")):
        fig.add_hline(y=value, line_dash="dash", line_color=color,
                      annotation_text=f"{label}: {value:g}", **kwargs)


def ph_figure(data, limits):
    fig = go.Figure(go.Scatter(x=data["timestamp"], y=data["value"], mode="markers",
                              marker=ph_markers(data, limits), name="pH reading",
                              hovertemplate="%{x|%d %b %H:%M:%S}<br>pH %{y:.2f}<extra></extra>"))
    add_ph_limits(fig, limits)
    if data.empty:
        fig.add_annotation(text="Waiting for a valid pH measurement", x=0.5, y=0.5,
                           xref="paper", yref="paper", showarrow=False)
    fig.update_layout(template="plotly_white", height=330, margin={"t": 35, "r": 35, "b": 45},
                      xaxis_title="Measurement time", yaxis_title="pH", uirevision="ph")
    fig.update_xaxes(type="date")
    return fig


CARD = {"padding": "14px 20px", "margin": "6px", "minWidth": "190px", "textAlign": "left",
        "border": "2px solid #ccc", "borderRadius": "10px", "background": "white", "cursor": "pointer"}
CARD_ON = {**CARD, "border": "2px solid #2e7d32", "background": "#e8f5e9"}
FAULT_BOX = {"padding": "10px", "margin": "8px 0", "border": "1px solid #b3261e",
             "borderRadius": "8px", "background": "#fff4f3"}


def sensor_card(key):
    sensor = config.SENSORS[key]
    return html.Button(id={"type": "card", "key": key}, n_clicks=0, style=CARD, children=[
        html.Div(sensor["label"], style={"fontWeight": "bold"}),
        html.Div("—", id={"type": "val", "key": key}, style={"fontSize": "28px", "margin": "4px 0"}),
        html.Div("waiting for data", id={"type": "ts", "key": key},
                 style={"fontSize": "12px", "color": "#666"}),
    ])


def render_fault(fault):
    severity = fault.get("severity", "FAULT")
    status = "acknowledged" if fault.get("acknowledged") else "not acknowledged"
    return html.Div(style=FAULT_BOX, children=[
        html.Strong(f"{severity} — {fault.get('code', 'UNKNOWN')}"),
        html.Div(fault.get("message", "")),
        html.Div(f"Automatic response: {fault.get('action', '')}"),
        html.Small(status),
    ])


range_options = [{"label": key, "value": key} for key in RANGES]
first_key = next(iter(config.SENSORS))

app = Dash(__name__, title="Biodigester Monitor")
# The layout describes what appears in the browser. Component IDs connect these
# controls to callbacks, which run when their Input values change.
app.layout = html.Div(style={"fontFamily": "sans-serif", "maxWidth": "1050px", "margin": "0 auto", "padding": "16px"}, children=[
    html.H2("Biodigester Monitoring Application"),
    html.Small(f"Dashboard {DASHBOARD_VERSION}", id="dashboard-version"),
    html.Div(id="status", style={"marginBottom": "12px"}),
    html.Div(id="logging-status", role="status", style={"color": "#b3261e"}),
    html.Div(id="archive-status", role="status", style={"color": "#b45309", "marginBottom": "8px"}),
    html.Div(style={"padding": "16px", "background": "#fff7e6", "borderRadius": "10px", "marginBottom": "16px"}, children=[
        html.H3("Vessel sampling"),
        html.P("Pause analysis before opening the vessel. Raw readings and alarms are still recorded; temperature control stays active. "
               "Marked periods are excluded from reports by default, while manual pH samples are kept."),
        dcc.Input(id="sampling-note", type="text", value="pH sampling / vessel open", placeholder="Sampling note", style={"width": "260px"}),
        html.Button("Pause analysis for sampling", id="pause-analysis", n_clicks=0),
        html.Label(" Settling time after closing (seconds) "),
        dcc.Input(id="settle-seconds", type="number", min=0, max=3600, value=getattr(config, "SAMPLING_SETTLE_S", 60), style={"width": "90px"}),
        html.Button("Resume after settling", id="resume-analysis", n_clicks=0),
        html.Div(id="sampling-action", role="status"),
        html.Strong(id="sampling-status", role="status"),
    ]),

    html.Div([sensor_card(key) for key in config.SENSORS],
             style={"display": "flex", "justifyContent": "center", "flexWrap": "wrap", "marginBottom": "16px"}),

    html.Div(style={"background": "#f3f5f4", "padding": "16px", "borderRadius": "10px"}, children=[
        html.H3("Pressure and gas bubbles"),
        html.Div(id="pressure-status"),
        html.Div(id="bubble-status"),
        html.H3("Controller and Peltier"),
        html.Div(id="controller-status", role="status"),
        html.Div(style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "marginTop": "12px"}, children=[
            html.Label("Lower temperature (°C)", htmlFor="range-low"),
            dcc.Input(id="range-low", type="number", value=config.DEFAULT_LOW_C, step=0.1),
            html.Label("Upper temperature (°C)", htmlFor="range-high"),
            dcc.Input(id="range-high", type="number", value=config.DEFAULT_HIGH_C, step=0.1),
            html.Button("Save range", id="save-range", n_clicks=0),
        ]),
        html.Div(id="range-result", role="status"),
    ]),

    html.Div(style={"marginTop": "16px", "padding": "20px", "border": "1px solid #d9e2e7",
                    "borderRadius": "10px", "background": "#fafcfd"}, children=[
        html.H3("pH monitoring"),
        html.H4("Log a manual pH measurement"),
        html.Label("Measured pH ", htmlFor="manual-ph"),
        dcc.Input(id="manual-ph", type="number", min=0, max=14, step=0.01, placeholder="e.g. 5.25"),
        html.Label(" Measurement time (optional, Pi local time) ", htmlFor="manual-ph-time"),
        dcc.Input(id="manual-ph-time", type="text", placeholder="YYYY-MM-DD HH:MM"),
        html.Button("Publish pH measurement", id="publish-ph", n_clicks=0),
        html.Div(id="manual-ph-result", role="status"),
        html.P("Notification limits only — these settings do not control pH or the heater."),
        html.Div(style={"display": "flex", "gap": "10px", "flexWrap": "wrap", "alignItems": "center"}, children=[
            html.Label("Minimum pH", htmlFor="ph-low"),
            dcc.Input(id="ph-low", type="number", min=0, max=14, step=0.1, value=config.DEFAULT_PH_MIN),
            html.Label("Maximum pH", htmlFor="ph-high"),
            dcc.Input(id="ph-high", type="number", min=0, max=14, step=0.1, value=config.DEFAULT_PH_MAX),
            html.Button("Save pH limits", id="save-ph", n_clicks=0),
        ]),
        html.Div(id="ph-result", role="status", style={"marginTop": "8px"}),
        html.Div(id="ph-notification", role="status", **{"aria-live": "polite"},
                 style={"padding": "12px 0"}),
        html.H4("pH measurements over time"),
        html.P("Each dot is one measurement. Simulation records one sample every 10 seconds. "
               "Dot colours use the currently saved limits."),
        dcc.Dropdown(range_options, "Latest logged 24 hours", id="ph-history-range", clearable=False,
                     style={"width": "220px"}),
        dcc.Graph(id="ph-graph"),
    ]),
    dcc.Store(id="ph-limits", storage_type="local"),

    html.Div(style={"marginTop": "16px"}, children=[
        html.H3("Active faults and alarms"),
        html.Div(id="fault-list"),
        html.Button("Acknowledge faults / request safe reset", id="ack-faults", n_clicks=0),
        html.Div(id="ack-result", role="status"),
        html.H4("Recent fault events"),
        html.Div(id="fault-events"),
    ]),

    html.Div(style={} if config.SHOW_SIM_PANEL else {"display": "none"}, children=[
        html.H3("Simulator testing"),
        html.P("These buttons only change simulated hardware behaviour. The controller is not told which fault was selected."),
        html.Div(style={"display": "flex", "gap": "8px", "flexWrap": "wrap"}, children=[
            html.Button("Temperature sensor stops", id={"type": "fault", "key": "temp_sensor_dropout"}, n_clicks=0),
            html.Button("Temperature sensor freezes", id={"type": "fault", "key": "temp_sensor_frozen"}, n_clicks=0),
            html.Button("Sensor out of range", id={"type": "fault", "key": "temp_sensor_out_of_range"}, n_clicks=0),
            html.Button("Heater reports ON but produces no heat", id={"type": "fault", "key": "heater_no_heat"}, n_clicks=0),
            html.Button("Peltier output stuck ON", id={"type": "fault", "key": "heater_stuck_on"}, n_clicks=0),
            html.Button("Clear injected fault", id={"type": "fault", "key": "none"}, n_clicks=0),
        ]),
        html.Div(id="fault-result", role="status"),
        html.Div(id="sim-status"),
    ]),

    dcc.Interval(id="tick", interval=2000),
    dcc.Interval(id="graph-tick", interval=int(getattr(config, "DASHBOARD_GRAPH_REFRESH_S", 30) * 1000)),
    dcc.Store(id="selected", data=first_key),
    dcc.Tabs([
        dcc.Tab(label="Sensor history", children=[
            html.H3(id="sensor-title"), html.P(id="sensor-desc"),
            dcc.Dropdown(range_options, "Latest logged 24 hours", id="hist-range", clearable=False, style={"width": "220px"}),
            dcc.Graph(id="hist-graph"),
        ]),
        dcc.Tab(label="Report generator", children=[
            html.Div(style={"marginTop": "12px"}, children=[
                html.H3("Report generator"),
                html.P("Source: saved CSV files. Choose sensors and a time range. Each sensor has its own axis. "
                       "Bubble rate = recorded events per interval divided by interval duration in minutes. "
                       "Incomplete coverage is labelled in the hover text and CSV; recorded rates may undercount detections. "
                       "Quiet intervals with unknown coverage and sampling periods remain gaps. "
                       "Latest logged 24 hours follows the archive, even after a restart. "
                       "Large temperature/pressure graphs use interval means; CSV exports retain individual samples."),
                dcc.Checklist(id="report-sensors", value=["temperature", "pressure", "bubble_rate"], inline=True,
                              options=[{"label": f" {REPORT_SENSORS[k]['label']}  ", "value": k}
                                       for k in REPORT_SENSORS]),
                dcc.Dropdown(range_options + [{"label": "Custom dates", "value": "Custom dates"}], "Latest logged 24 hours", id="report-range", clearable=False, style={"width": "220px"}),
                html.Div(id="custom-report-range", children=[
                    html.Label("Custom dates (Pi local time) "),
                    dcc.DatePickerRange(id="report-dates", display_format="DD MMM YYYY"),
                    html.Label(" Start time "), dcc.Input(id="report-start-time", type="text", value="00:00", style={"width": "80px"}),
                    html.Label(" End time "), dcc.Input(id="report-end-time", type="text", value="23:59", style={"width": "80px"}),
                ]),
                html.Label("Bubble-rate interval "),
                dcc.Dropdown([{"label": "Automatic", "value": "auto"}] +
                             [{"label": f"{n} minute{'s' if n>1 else ''}", "value": n} for n in (1,5,15,60,240,1440)],
                             "auto", id="bubble-interval", clearable=False, style={"width": "180px"}),
                dcc.Checklist(id="include-sampling", options=[{"label": " Include sampling periods in analysis", "value": "include"}], value=[]),
                html.Button("Generate / refresh report", id="generate-report", n_clicks=0),
                html.Button("Download report CSV", id="download-report", n_clicks=0),
                dcc.Download(id="report-download"),
                html.Div(id="report-error", role="status"),
                dcc.Graph(id="report-graph"),
            ])
        ]),
    ]),
])


@app.callback(Output({"type": "val", "key": ALL}, "children"),
              Output({"type": "ts", "key": ALL}, "children"),
              Output("status", "children"), Input("tick", "n_intervals"))
def refresh_cards(_):
    # ALL updates every matching sensor card. The unused argument is the tick
    # count; the interval simply tells us when to refresh the displayed data.
    latest, _, connected = monitor.snapshot()
    values, stamps = [], []
    for key, sensor in config.SENSORS.items():
        if key not in latest:
            values.append("—")
            stamps.append("waiting for pH measurement" if key == "ph" else "waiting for data")
            continue
        value, stamp, retained, received = latest[key]
        values.append(f"{value:.2f} {sensor['unit']}")
        if key == "ph":
            source = "Simulated pH" if monitor.ph_simulated else "pH measurement"
            qualifier = "saved; measurement time unknown" if retained else stamp.strftime("last measured %d %b %H:%M:%S")
            if not connected or time.monotonic() - received > config.PH_STALE_AFTER_S:
                qualifier += "; no recent reading"
            stamps.append(f"{source} · {qualifier}")
            continue
        stale = time.monotonic() - received > (config.PRESSURE_STALE_AFTER_S if key == "pressure" else config.TEMP_STALE_AFTER_S)
        if retained:
            stamps.append("saved value; waiting for live data")
        elif stale or not connected:
            stamps.append(stamp.strftime("stale; last update %H:%M:%S"))
        else:
            stamps.append(stamp.strftime("updated %H:%M:%S"))
    status = "● Connected to broker" if connected else "○ Not connected to broker"
    receiver = monitor.receiver_status()
    if connected and receiver["error"]:
        status += " · message processing error; see logging status"
    elif connected and receiver["last_message_age"] is not None and receiver["last_message_age"] > max(10, config.KEEPALIVE_S * 2):
        status += f" · no MQTT messages for {receiver['last_message_age']:.0f} seconds"
    return values, stamps, status


@app.callback(Output("selected", "data"), Output({"type": "card", "key": ALL}, "style"),
              Input({"type": "card", "key": ALL}, "n_clicks"))
def pick_sensor(_):
    key = first_key
    if ctx.triggered_id:
        key = ctx.triggered_id["key"]

    styles = []
    for sensor_name in config.SENSORS:
        if sensor_name == key:
            styles.append(CARD_ON)
        else:
            styles.append(CARD)
    return key, styles


@app.callback(Output("hist-graph", "figure"), Output("sensor-title", "children"),
              Output("sensor-desc", "children"), Input("selected", "data"),
              Input("hist-range", "value"), Input("graph-tick", "n_intervals"), Input("ph-limits", "data"))
def update_history(key, range_name, _, limits=None):
    sensor = config.SENSORS[key]
    try:
        figure = history_fig(key, RANGES[range_name], limits)
    except (KeyError, ValueError, OSError, pd.errors.ParserError) as exc:
        figure = empty_fig(f"Cannot read CSV history: {exc}")
    return figure, sensor["label"], sensor["description"]


def report_bounds(range_name, start_date, end_date, start_time, end_time):
    if range_name != "Custom dates":
        return RANGES[range_name], None, None
    if not start_date or not end_date:
        raise ValueError("Choose both custom dates")
    start = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(end_date).replace(hour=0, minute=0, second=0, microsecond=0)
    first = datetime.strptime(start_time or "00:00", "%H:%M")
    last = datetime.strptime(end_time or "23:59", "%H:%M")
    start = start.replace(hour=first.hour, minute=first.minute)
    end = end.replace(hour=last.hour, minute=last.minute) + timedelta(minutes=1)
    if start >= end:
        raise ValueError("Start must be before end")
    return None, start, end


@app.callback(Output("report-graph", "figure"), Output("report-error", "children"),
              Input("report-sensors", "value"), Input("report-range", "value"),
              Input("generate-report", "n_clicks"), Input("ph-limits", "data"),
              Input("report-dates", "start_date"), Input("report-dates", "end_date"),
              Input("report-start-time", "value"), Input("report-end-time", "value"),
              Input("bubble-interval", "value"), Input("include-sampling", "value"))
def update_report(keys, range_name, _, limits=None, start_date=None, end_date=None,
                  start_time="00:00", end_time="23:59", interval="auto", include=None):
    try:
        minutes, start, end = report_bounds(range_name, start_date, end_date, start_time, end_time)
        return report_fig(keys, minutes, limits, start, end, interval, "include" in (include or []), refresh=True), ""
    except (KeyError, ValueError, OSError, pd.errors.ParserError) as error:
        return empty_fig("Cannot generate report"), str(error)


@app.callback(Output("report-download", "data"), Input("download-report", "n_clicks"),
              State("report-sensors", "value"), State("report-range", "value"),
              State("report-dates", "start_date"), State("report-dates", "end_date"),
              State("report-start-time", "value"), State("report-end-time", "value"),
              State("bubble-interval", "value"), State("include-sampling", "value"), prevent_initial_call=True)
def download_report(_, keys, range_name, start_date, end_date, start_time, end_time, interval, include):
    try:
        minutes, start, end = report_bounds(range_name, start_date, end_date, start_time, end_time)
        keys = [key for key in (keys or []) if key in REPORT_SENSORS]
        if not keys: return no_update
        data, _, _, _ = report_data(keys, minutes, start, end, interval, "include" in (include or []), refresh=True)
        return dcc.send_data_frame(data.to_csv, "biodigester-report.csv", index=False)
    except (ValueError, OSError, pd.errors.ParserError):
        return no_update


@app.callback(Output("manual-ph-result", "children"), Input("publish-ph", "n_clicks"),
              State("manual-ph", "value"), State("manual-ph-time", "value"), prevent_initial_call=True)
def publish_manual_ph(_, value, measured_at):
    return monitor.log_manual_ph(value, measured_at)


@app.callback(Output("sampling-action", "children"), Input("pause-analysis", "n_clicks"),
              Input("resume-analysis", "n_clicks"), State("sampling-note", "value"),
              State("settle-seconds", "value"), prevent_initial_call=True)
def change_sampling(_, __, note, seconds):
    if ctx.triggered_id not in ("pause-analysis", "resume-analysis"):
        return no_update
    return monitor.set_sampling(ctx.triggered_id == "pause-analysis", note or "Vessel sampling", seconds or 0)


@app.callback(Output("sampling-status", "children"), Input("tick", "n_intervals"))
def refresh_sampling(_):
    state = monitor.sampling_status()
    if state["settling"]:
        return f"Settling: analysis resumes at {state['resume_at']:%H:%M:%S} (Pi local time)."
    if state["paused"]:
        return f"Analysis paused for sampling: {state['reason']}. Raw logging and control remain active."
    return "Analysis running. Sampling periods remain marked in the archive."


@app.callback(Output("logging-status", "children"), Input("tick", "n_intervals"))
def refresh_logging(_):
    errors = monitor.logging_status()
    return ("CSV logging needs attention: " + "; ".join(f"{key}: {value}" for key, value in errors.items())) if errors else ""


@app.callback(Output("archive-status", "children"), Input("tick", "n_intervals"))
def refresh_archive_status(_):
    try:
        if not monitor.log_file.exists():
            return (f"Sensor CSV missing: {monitor.log_file}. Temperature, pressure and pH history "
                    "need this file. Keep or restore the existing archive; a GitHub code update does not restore it.")
        if monitor.log_file.stat().st_size == 0:
            return f"Sensor CSV is empty: {monitor.log_file}. Waiting for logged measurements."
    except OSError as exc:
        return f"Cannot access sensor CSV: {exc}"
    return ""


@app.callback(Output("controller-status", "children"), Output("save-range", "disabled"),
              Output("sim-status", "children"), Output("fault-list", "children"),
              Output("fault-events", "children"), Input("tick", "n_intervals"))
def refresh_controller(_):
    status = monitor.controller_status()
    _, messages, connected = monitor.snapshot()
    mode = status.get("mode", "OFFLINE")
    children = [html.Strong(f"{mode}: "), html.Span(status.get("reason", ""))]
    if "low" in status and "high" in status:
        children.append(html.Div(f"Control band: {status['low']:g} to {status['high']:g} °C"))
    heater_entry = messages.get(config.TOPIC_PELTIER_STATE)
    if heater_entry:
        heater, received, retained = heater_entry
        stale = (retained or not connected or heater.get('online') is False
                 or heater.get('output_unknown') is True
                 or time.monotonic() - received > config.ACTUATOR_FEEDBACK_TIMEOUT_S)
        qualifier = " (saved/stale)" if stale else ""
        cutout = "unknown" if heater.get('online') is False else ("LATCHED" if heater.get("safety_latched") else "ready")
        children.append(html.Div(
            f"Peltier PWM duty: {heater.get('applied_power', '?')}%; "
            f"limit: {heater.get('max_power', 'unknown (older driver)')}%; "
            f"source: {'simulator' if heater.get('simulated') else 'hardware driver'}; "
            f"software safety latch: {cutout}{qualifier}"))
    if status.get("note"):
        children.append(html.Div(status["note"]))

    sim_text = "Waiting for simulator."
    sim_entry = messages.get(config.TOPIC_SIM_STATE)
    if sim_entry:
        sim, received, retained = sim_entry
        stale = retained or not connected or time.monotonic() - received > config.TEMP_STALE_AFTER_S
        qualifier = " (saved/stale)" if stale else ""
        sim_text = f"Injected scenario: {sim.get('fault', 'unknown')}; speed: {sim.get('speed', '?')}×{qualifier}"

    faults = monitor.active_faults()
    fault_children = [render_fault(fault) for fault in faults]
    if not fault_children:
        fault_children = [html.Div("No active faults or alarms.")]
    events = monitor.notifications.recent()[:8]
    event_children = []
    for event in events:
        received_time = event["received_at"].strftime("%H:%M:%S")
        text = (
            f"{received_time} — {event.get('event')} "
            f"{event.get('code')} ({event.get('severity')})"
        )
        event_children.append(html.Div(text))
    if not event_children:
        event_children = [html.Div("No fault events received yet.")]
    return children, mode == "OFFLINE", sim_text, fault_children, event_children


@app.callback(Output("range-result", "children"), Input("save-range", "n_clicks"),
              State("range-low", "value"), State("range-high", "value"), prevent_initial_call=True)
def save_range(_, low, high):
    return monitor.set_range(low, high)


@app.callback(Output("fault-result", "children"),
              Input({"type": "fault", "key": ALL}, "n_clicks"), prevent_initial_call=True)
def inject_fault(_):
    if not ctx.triggered_id:
        return ""
    return monitor.set_fault(ctx.triggered_id["key"])


@app.callback(Output("ack-result", "children"), Input("ack-faults", "n_clicks"), prevent_initial_call=True)
def acknowledge_faults(_):
    return monitor.acknowledge_faults()


@app.callback(Output("ph-limits", "data"), Output("ph-result", "children"),
              Input("save-ph", "n_clicks"), State("ph-low", "value"),
              State("ph-high", "value"), prevent_initial_call=True)
def save_ph_limits(_, low, high):
    problem = validate_limits(low, high)
    if problem:
        return no_update, problem
    return {"low": float(low), "high": float(high)}, "pH notification limits saved in this browser."


@app.callback(Output("ph-low", "value"), Output("ph-high", "value"), Input("ph-limits", "data"))
def restore_ph_limits(limits):
    return ph_limits(limits)


@app.callback(Output("ph-graph", "figure"), Input("graph-tick", "n_intervals"),
              Input("ph-limits", "data"), Input("ph-history-range", "value"))
def refresh_ph_graph(_, limits, range_name, refresh=False):
    try:
        data = report_data(["ph"], RANGES[range_name], refresh=refresh)[0]
        figure = ph_figure(data[data["sensor"] == "ph"], limits)
        figure.update_layout(uirevision=f"ph:{range_name}")
    except (KeyError, ValueError, OSError, pd.errors.ParserError) as exc:
        figure = empty_fig(f"Cannot read pH CSV history: {exc}")
    return figure


@app.callback(Output("ph-notification", "children"), Output({"type": "val", "key": "ph"}, "style"),
              Input("tick", "n_intervals"), Input("ph-limits", "data"))
def refresh_ph_status(_, limits):
    low, high = ph_limits(limits)
    latest, _, connected = monitor.snapshot()
    entry = latest.get("ph")
    color = "#666"
    if entry is None:
        message = "Waiting for a valid pH measurement."
    else:
        value, stamp, retained, received = entry
        label, color = classify(value, low, high)
        source = "Simulated pH" if monitor.ph_simulated else "Measured pH"
        detail = (f"below minimum {low:g}" if value < low else
                  f"above maximum {high:g}" if value > high else f"within {low:g}–{high:g}")
        message = f"{source} · {label}: {value:.2f} — {detail}. "
        if retained:
            message += "Saved reading; measurement time unknown. Waiting for a new measurement."
            color = "#666"
        else:
            message += stamp.strftime("Last measured %d %b %Y at %H:%M:%S.")
            if not connected or time.monotonic() - received > config.PH_STALE_AFTER_S:
                message += " No recent pH reading; this status describes the last measurement only."
                color = "#666"
    return html.Strong(message, style={"color": color}), {
        "fontSize": "28px", "margin": "4px 0", "color": color}


def refresh_ph(tick, limits, range_name):
    """Combined helper retained for callers; live HTTP updates use no CSV IO."""
    message, style = refresh_ph_status(tick, limits)
    return message, refresh_ph_graph(tick, limits, range_name, refresh=True), style


@app.callback(Output("pressure-status", "children"), Output("bubble-status", "children"),
              Input("tick", "n_intervals"))
def refresh_bubbles(_):
    pressure = monitor.pressure_summary()
    bubble = monitor.bubble_summary()
    last = bubble["last_bubble"].strftime("%H:%M:%S") if bubble["last_bubble"] else "none observed"
    state = {"recovering": "relearning baseline", "warming_up": "warming up", "disabled": "disabled"}.get(
        bubble.get("state"), ("ACTIVE" if bubble["active"] else "idle") if bubble["live"] else "waiting/stale")
    return pressure["description"], (
        f"Bubbles: {state}; count this hardware session: {bubble['count']}; "
        f"observed last 60 s: {bubble['rate']}/min; last observed: {last}")


def main():
    """Start the browser app and optionally start its demo processes."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("Dashboard %s; source=%s; sensor CSV=%s",
                                    DASHBOARD_VERSION, Path(__file__).resolve(), monitor.log_file)
    parser = argparse.ArgumentParser(description="Biodigester monitoring dashboard")
    parser.add_argument("--demo", action="store_true",
                        help="start controller and simulator as child processes")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop_requested)
    children = []
    try:
        if args.demo:
            folder = Path(__file__).resolve().parent
            for script in ("controller.py", "simulator.py"):
                children.append(subprocess.Popen(
                    [sys.executable, "-u", str(folder / script)], cwd=folder,
                    start_new_session=True))
        monitor.start()
        app.run(debug=False, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)
    finally:
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        monitor.stop()


if __name__ == "__main__":
    main()
