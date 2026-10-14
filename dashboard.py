"""Dash UI for observing the biodigester controller.

Safety decisions do not live here. The dashboard receives controller state,
presents fault notifications/history, and sends operator requests such as range
changes, acknowledgements, or simulator-only fault injections.
"""
import argparse
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
from monitoring.ph_monitor import classify, validate_limits

monitor = MonitoringService()
lock = monitor.lock

RANGES = {
    "Last 15 minutes": 15,
    "Last hour": 60,
    "Last 6 hours": 360,
    "Last 24 hours": 1440,
    "Last 7 days": 10080,
    "All data": None,
}


def load_df(minutes=None):
    """Read logged samples into a pandas table, optionally keeping recent rows."""
    with lock:
        try:
            df = pd.read_csv(monitor.log_file, parse_dates=["timestamp"])
        except (FileNotFoundError, pd.errors.EmptyDataError):
            df = pd.DataFrame(columns=["timestamp", "sensor", "value"])
    if minutes and len(df):
        df = df[df["timestamp"] >= datetime.now() - timedelta(minutes=minutes)]
    return df


def empty_fig(message):
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 16})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(height=350, margin={"t": 20})
    return fig


def history_fig(key, minutes, limits=None):
    """Plot one sensor's history with temperature limits where applicable."""
    df = load_df(minutes)
    df = df[df["sensor"] == key]
    if key == "ph":
        return ph_figure(df, limits)
    if df.empty:
        return empty_fig("No logged data yet for this sensor")
    sensor = config.SENSORS[key]
    fig = go.Figure(go.Scatter(x=df["timestamp"], y=df["value"], mode="lines"))
    fig.update_layout(height=380, margin={"t": 20},
                      yaxis_title=f"{sensor['label']} ({sensor['unit']})", uirevision=key)
    if key == "temperature":
        status = monitor.controller_status()
        if "low" in status and "high" in status:
            fig.add_hrect(y0=status["low"], y1=status["high"], opacity=0.12,
                          line_width=0, annotation_text="Normal control band")
        fig.add_hline(y=config.HIGH_TEMP_C, line_dash="dash", annotation_text="High")
        fig.add_hline(y=config.HIGH_HIGH_TEMP_C, line_dash="dot", annotation_text="High-high")
    return fig


def report_fig(keys, minutes, limits=None):
    """Plot each selected sensor on its own row so units stay separate."""
    if not keys:
        return empty_fig("Tick at least one sensor")
    df = load_df(minutes)
    df = df[df["sensor"].isin(keys)]
    if df.empty:
        return empty_fig("No logged data in this time range")
    fig = make_subplots(rows=len(keys), cols=1, shared_xaxes=True, vertical_spacing=0.06)
    for row, key in enumerate(keys, start=1):
        data = df[df["sensor"] == key]
        sensor = config.SENSORS[key]
        fig.add_trace(go.Scatter(x=data["timestamp"], y=data["value"],
                                 mode="markers" if key == "ph" else "lines",
                                 marker=ph_markers(data, limits) if key == "ph" else None,
                                 name=sensor["label"]), row=row, col=1)
        if key == "ph":
            add_ph_limits(fig, limits, row=row, col=1)
        fig.update_yaxes(title_text=f"{sensor['label']} ({sensor['unit']})", row=row, col=1)
    fig.update_layout(height=260 * len(keys) + 80, margin={"t": 20}, showlegend=False)
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
    html.Div(id="status", style={"marginBottom": "12px"}),

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
        dcc.Dropdown(range_options, "Last hour", id="ph-history-range", clearable=False,
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
    dcc.Store(id="selected", data=first_key),
    dcc.Tabs([
        dcc.Tab(label="Sensor history", children=[
            html.H3(id="sensor-title"), html.P(id="sensor-desc"),
            dcc.Dropdown(range_options, "Last hour", id="hist-range", clearable=False, style={"width": "220px"}),
            dcc.Graph(id="hist-graph"),
        ]),
        dcc.Tab(label="Report", children=[
            html.Div(style={"marginTop": "12px"}, children=[
                dcc.Checklist(id="report-sensors", value=[first_key], inline=True,
                              options=[{"label": f" {config.SENSORS[k]['label']}  ", "value": k}
                                       for k in config.SENSORS]),
                dcc.Dropdown(range_options, "Last hour", id="report-range", clearable=False, style={"width": "220px"}),
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
    return values, stamps, ("● Connected to broker" if connected else "○ Not connected to broker")


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
              Input("hist-range", "value"), Input("tick", "n_intervals"), Input("ph-limits", "data"))
def update_history(key, range_name, _, limits=None):
    sensor = config.SENSORS[key]
    return history_fig(key, RANGES[range_name], limits), sensor["label"], sensor["description"]


@app.callback(Output("report-graph", "figure"), Input("report-sensors", "value"),
              Input("report-range", "value"), Input("tick", "n_intervals"), Input("ph-limits", "data"))
def update_report(keys, range_name, _, limits=None):
    return report_fig(keys, RANGES[range_name], limits)


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
        stale = retained or not connected or time.monotonic() - received > config.TEMP_STALE_AFTER_S
        qualifier = " (saved/stale)" if stale else ""
        cutout = "LATCHED" if heater.get("safety_latched") else "ready"
        children.append(html.Div(
            f"Peltier output: {heater.get('applied_power', '?')}%; "
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


@app.callback(Output("ph-notification", "children"), Output("ph-graph", "figure"),
              Output({"type": "val", "key": "ph"}, "style"),
              Input("tick", "n_intervals"), Input("ph-limits", "data"),
              Input("ph-history-range", "value"))
def refresh_ph(_, limits, range_name):
    low, high = ph_limits(limits)
    data = load_df(RANGES[range_name])
    figure = ph_figure(data[data["sensor"] == "ph"], limits)
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
    return html.Strong(message, style={"color": color}), figure, {
        "fontSize": "28px", "margin": "4px 0", "color": color}


@app.callback(Output("pressure-status", "children"), Output("bubble-status", "children"),
              Input("tick", "n_intervals"))
def refresh_bubbles(_):
    pressure = monitor.pressure_summary()
    bubble = monitor.bubble_summary()
    last = bubble["last_bubble"].strftime("%H:%M:%S") if bubble["last_bubble"] else "none observed"
    state = ("ACTIVE" if bubble["active"] else "idle") if bubble["live"] else "waiting/stale"
    return pressure["description"], (
        f"Bubbles: {state}; count this hardware session: {bubble['count']}; "
        f"observed last 60 s: {bubble['rate']}/min; last observed: {last}")


def main():
    """Start the browser app and optionally start its demo processes."""
    parser = argparse.ArgumentParser(description="Biodigester monitoring dashboard")
    parser.add_argument("--demo", action="store_true",
                        help="start controller and simulator as child processes")
    args = parser.parse_args()
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
