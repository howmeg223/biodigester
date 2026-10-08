"""
What should be in the dashboard file:
- The cosmetics of anything displayed on the dashboard
- It should not contain the classes for faults, and the sensor data.

- Separate the fault handling and real data



"""



import argparse
import csv
import json
import math
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html
from plotly.subplots import make_subplots

import config
from config import LOG_FILE, SENSORS
from control_logic import Settings, check_thresholds
from mqtt_common import connected_ok, make_client

class Monitor:
    def __init__(self, log_file=config.LOG_FILE):
        self.log_file = Path(log_file)
        self.lock = threading.RLock()
        self.latest = {}
        self.messages = {}
        self.connected = False
        self.client = None
        self.sensor_topics = {s["topic"]: k for k, s in config.SENSORS.items()}

    def ensure_log(self):
        with self.lock:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.log_file.exists() or self.log_file.stat().st_size == 0:
                with self.log_file.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["timestamp", "sensor", "value"])

    def handle_message(self, topic, payload, retained=False):
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        now = time.monotonic()
        with self.lock:
            if topic in self.sensor_topics:
                key = self.sensor_topics[topic]
                raw = data.get(key) if isinstance(data, dict) else data
                if isinstance(raw, bool):
                    return
                try:
                    value = float(raw)
                except (ValueError, TypeError, OverflowError):
                    return
                if not math.isfinite(value):
                    return
                stamp = datetime.now()
                self.latest[key] = (value, stamp, retained, now)
                if not retained:
                    self.ensure_log()
                    with self.log_file.open("a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerow([stamp.isoformat(timespec="seconds"), key, value])
            elif topic in (config.TOPIC_STATUS, config.TOPIC_HEATER_STATE, config.TOPIC_SIM_STATE):
                if not isinstance(data, dict):
                    return
                if topic == config.TOPIC_STATUS:
                    if data.get("mode") not in ("HEATING", "IDLE", "FAULT", "OFFLINE"):
                        return
                    for key in ("low", "high"):
                        if key in data and (isinstance(data[key], bool) or
                                            not isinstance(data[key], (int, float)) or
                                            not math.isfinite(data[key])):
                            return
                    if not isinstance(data.get("reason", ""), str) or not isinstance(data.get("note", ""), str):
                        return
                    alarms = data.get("alarm_text", [])
                    if not isinstance(alarms, list) or not all(isinstance(a, str) for a in alarms):
                        return
                elif topic == config.TOPIC_HEATER_STATE and data.get("state") not in ("ON", "OFF"):
                    return
                self.messages[topic] = (data, now, retained)

    def snapshot(self):
        with self.lock:
            return dict(self.latest), dict(self.messages), self.connected

    def controller_status(self):
        _, messages, connected = self.snapshot()
        entry = messages.get(config.TOPIC_STATUS)
        if not entry:
            return {"mode": "OFFLINE", "reason": "Waiting for the controller."}
        data, received, retained = entry
        if not connected or time.monotonic() - received > config.STALE_AFTER_S:
            return {"mode": "OFFLINE", "reason": "No live controller status. Check the controller and broker."}
        if retained and data.get("mode") != "OFFLINE":
            return {"mode": "OFFLINE", "reason": "Saved controller status received; waiting for a live update."}
        return dict(data)

    def start(self):
        self.ensure_log()
        client = make_client(f"biodigester-dashboard-{uuid.uuid4().hex[:8]}")

        def on_connect(c, userdata, flags, reason_code, *extra):
            with self.lock:
                self.connected = connected_ok(reason_code)
            if self.connected:
                for topic in (*self.sensor_topics, config.TOPIC_STATUS,
                              config.TOPIC_HEATER_STATE, config.TOPIC_SIM_STATE):
                    c.subscribe(topic, qos=1)

        def on_disconnect(*args):
            with self.lock:
                self.connected = False

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = lambda c, u, m: self.handle_message(m.topic, m.payload, bool(m.retain))
        self.client = client
        client.connect_async(config.BROKER_HOST, config.BROKER_PORT, config.KEEPALIVE_S)
        client.loop_start()

    def stop(self):
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def publish(self, topic, data, retain=False):
        if not self.client or not self.client.is_connected():
            return "Not connected to the broker. Nothing was sent."
        result = self.client.publish(topic, json.dumps(data), qos=1, retain=retain)
        if result.rc != 0:
            return "Could not send the request. Try again after reconnecting."
        return None

    def set_range(self, low, high):
        problem = check_thresholds(low, high, Settings())
        if problem:
            return problem
        if self.controller_status().get("mode") == "OFFLINE":
            return "Wait for a live controller before changing the range."
        problem = self.publish(config.TOPIC_THRESHOLDS,
                               {"low": float(low), "high": float(high)}, retain=True)
        return problem or "Range request sent. Check the active range and controller reply below."

    def set_fault(self, fault):
        if not config.SHOW_SIM_PANEL:
            return "Simulator controls are disabled."
        if fault not in ("sensor_dropout", "sensor_bad", "heater_failure", "none"):
            return "Unknown simulator fault."
        problem = self.publish(config.TOPIC_SIM_FAULT, {"fault": fault})
        return problem or "Simulator request sent."


monitor = Monitor()
lock = monitor.lock

RANGES = {
    "Last 15 minutes": 15,
    "Last hour": 60,
    "Last 6 hours": 360,
    "Last 24 hours": 1440,
    "Last 7 days": 10080,
    "All data": None,
}


# ---------------------------------------------------------------- data helpers
def load_df(minutes=None):
    with lock:
        try:
            df = pd.read_csv(LOG_FILE, parse_dates=["timestamp"])
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


def history_fig(key, minutes):
    df = load_df(minutes)
    df = df[df["sensor"] == key]
    if df.empty:
        return empty_fig("No logged data yet for this sensor")
    s = SENSORS[key]
    fig = go.Figure(go.Scatter(x=df["timestamp"], y=df["value"], mode="lines"))
    fig.update_layout(
        height=380,
        margin={"t": 20},
        yaxis_title=f"{s['label']} ({s['unit']})",
        uirevision=key,  # keeps zoom when the graph refreshes
    )
    if key == "temperature":
        status = monitor.controller_status()
        if "low" in status and "high" in status:
            fig.add_hrect(y0=status["low"], y1=status["high"], fillcolor="green",
                          opacity=0.12, line_width=0, annotation_text="Current target range")
    return fig


def report_fig(keys, minutes):
    if not keys:
        return empty_fig("Tick at least one sensor")
    df = load_df(minutes)
    df = df[df["sensor"].isin(keys)]
    if df.empty:
        return empty_fig("No logged data in this time range")
    # One stacked panel per sensor, shared time axis (units differ, so no shared y).
    fig = make_subplots(rows=len(keys), cols=1, shared_xaxes=True, vertical_spacing=0.06)
    for i, key in enumerate(keys, start=1):
        d = df[df["sensor"] == key]
        s = SENSORS[key]
        fig.add_trace(go.Scatter(x=d["timestamp"], y=d["value"], mode="lines", name=s["label"]), row=i, col=1)
        fig.update_yaxes(title_text=f"{s['label']} ({s['unit']})", row=i, col=1)
    fig.update_layout(height=260 * len(keys) + 80, margin={"t": 20}, showlegend=False)
    return fig


# ---------------------------------------------------------------- layout
CARD = {"padding": "14px 20px", "margin": "6px", "minWidth": "170px", "textAlign": "left",
        "border": "2px solid #ccc", "borderRadius": "10px", "background": "white", "cursor": "pointer"}
CARD_ON = {**CARD, "border": "2px solid #2e7d32", "background": "#e8f5e9"}


def card(key):
    return html.Button(
        id={"type": "card", "key": key}, n_clicks=0, style=CARD,
        children=[
            html.Div(SENSORS[key]["label"], style={"fontWeight": "bold"}),
            html.Div("—", id={"type": "val", "key": key}, style={"fontSize": "28px", "margin": "4px 0"}),
            html.Div("waiting for data", id={"type": "ts", "key": key}, style={"fontSize": "12px", "color": "#666"}),
        ],
    )


range_options = [{"label": k, "value": k} for k in RANGES]
first_key = next(iter(SENSORS))

app = Dash(__name__, title="Biogas Monitor")
app.layout = html.Div(
    style={"fontFamily": "sans-serif", "maxWidth": "1000px", "margin": "0 auto", "padding": "16px"},
    children=[
        html.H2("Biogas Digester Monitor"),
        html.Div(id="status", style={"marginBottom": "8px"}),
        html.Div([
            html.H3("Temperature controller"),
            html.Div(id="controller-status", role="status"),
            html.Div([
                html.Label("Lower temperature (°C) ", htmlFor="range-low"),
                dcc.Input(id="range-low", type="number", value=config.DEFAULT_LOW_C, step=0.1),
                html.Label(" Upper temperature (°C) ", htmlFor="range-high"),
                dcc.Input(id="range-high", type="number", value=config.DEFAULT_HIGH_C, step=0.1),
                html.Button("Save range", id="save-range", n_clicks=0),
            ], style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "marginTop": "12px"}),
            html.Div(id="range-result", role="status"),
        ], style={"background": "#f3f5f4", "padding": "16px", "borderRadius": "10px"}),
        html.Div([
            html.H4("Simulator testing"),
            html.Div([
                html.Button(label, id={"type": "fault", "key": fault}, n_clicks=0)
                for label, fault in [("Silence sensor", "sensor_dropout"),
                                     ("Send nonsense reading", "sensor_bad"),
                                     ("Break heater", "heater_failure"), ("Clear fault", "none")]
            ], style={"display": "flex", "gap": "8px", "flexWrap": "wrap"}),
            html.Div(id="fault-result", role="status"),
            html.Div(id="sim-status"),
        ], style={} if config.SHOW_SIM_PANEL else {"display": "none"}),
        dcc.Interval(id="tick", interval=2000),
        dcc.Store(id="selected", data=first_key),
        dcc.Tabs([
            dcc.Tab(label="Live", children=[
                html.Div([card(k) for k in SENSORS], style={"display": "flex", "flexWrap": "wrap", "marginTop": "12px"}),
                html.H3(id="sensor-title"),
                html.P(id="sensor-desc"),
                dcc.Dropdown(range_options, "Last hour", id="hist-range", clearable=False, style={"width": "220px"}),
                dcc.Graph(id="hist-graph"),
            ]),
            dcc.Tab(label="Report", children=[
                html.Div(style={"marginTop": "12px"}, children=[
                    html.B("Sensors: "),
                    dcc.Checklist(
                        id="report-sensors", value=[first_key], inline=True,
                        options=[{"label": f" {SENSORS[k]['label']}  ", "value": k} for k in SENSORS],
                    ),
                    html.Div(style={"display": "flex", "gap": "12px", "margin": "10px 0"}, children=[
                        dcc.Dropdown(range_options, "Last hour", id="report-range", clearable=False, style={"width": "220px"}),
                        html.Button("Refresh", id="report-refresh", n_clicks=0),
                    ]),
                    dcc.Graph(id="report-graph"),
                ]),
            ]),
        ]),
    ],
)


# ---------------------------------------------------------------- callbacks
@app.callback(
    Output({"type": "val", "key": ALL}, "children"),
    Output({"type": "ts", "key": ALL}, "children"),
    Output("status", "children"),
    Input("tick", "n_intervals"),
)
def refresh_cards(_):
    snap, _, connected = monitor.snapshot()
    vals, stamps = [], []
    for key, s in SENSORS.items():
        if key in snap:
            value, t, retained, received = snap[key]
            vals.append(f"{value:.2f} {s['unit']}")
            stale = time.monotonic() - received > config.STALE_AFTER_S
            stamps.append("saved value; waiting for live data" if retained else
                          t.strftime("stale; last update %H:%M:%S" if stale or not connected else "updated %H:%M:%S"))
        else:
            vals.append("—")
            stamps.append("waiting for data")
    conn = "● Connected to broker" if connected else "○ Not connected to broker"
    return vals, stamps, conn


@app.callback(
    Output("selected", "data"),
    Output({"type": "card", "key": ALL}, "style"),
    Input({"type": "card", "key": ALL}, "n_clicks"),
)
def pick_sensor(_):
    key = ctx.triggered_id["key"] if ctx.triggered_id else first_key
    return key, [CARD_ON if k == key else CARD for k in SENSORS]


@app.callback(
    Output("hist-graph", "figure"),
    Output("sensor-title", "children"),
    Output("sensor-desc", "children"),
    Input("selected", "data"),
    Input("hist-range", "value"),
    Input("tick", "n_intervals"),
)
def update_history(key, range_name, _):
    return history_fig(key, RANGES[range_name]), SENSORS[key]["label"], SENSORS[key]["description"]


@app.callback(
    Output("report-graph", "figure"),
    Input("report-sensors", "value"),
    Input("report-range", "value"),
    Input("report-refresh", "n_clicks"),
)
def update_report(keys, range_name, _):
    return report_fig(keys, RANGES[range_name])


@app.callback(
    Output("controller-status", "children"),
    Output("save-range", "disabled"),
    Output("sim-status", "children"),
    Input("tick", "n_intervals"),
)
def refresh_controller(_):
    status = monitor.controller_status()
    _, messages, connected = monitor.snapshot()
    mode = status.get("mode", "OFFLINE")
    children = [html.Strong(f"{mode}: "), html.Span(status.get("reason", ""))]
    if "low" in status and "high" in status:
        children.append(html.Div(f"Active range: {status['low']:g} to {status['high']:g} °C"))
    heater = messages.get(config.TOPIC_HEATER_STATE)
    if heater:
        data, received, retained = heater
        qualifier = " (saved or stale report)" if retained or not connected or time.monotonic() - received > config.STALE_AFTER_S else ""
        children.append(html.Div(f"Heater reported: {data.get('state', 'unknown')}{qualifier}"))
    for alarm in status.get("alarm_text", []):
        children.append(html.Div(alarm, style={"color": "#b3261e"}))
    if status.get("note"):
        children.append(html.Div(status["note"]))
    sim = messages.get(config.TOPIC_SIM_STATE)
    sim_text = "Waiting for simulator."
    if sim:
        data, received, retained = sim
        qualifier = " (saved or stale report)" if retained or not connected or time.monotonic() - received > config.STALE_AFTER_S else ""
        sim_text = f"Fault: {data.get('fault', 'unknown')}; speed: {data.get('speed', '?')}×{qualifier}"
    return children, mode == "OFFLINE", sim_text


@app.callback(Output("range-result", "children"), Input("save-range", "n_clicks"),
              State("range-low", "value"), State("range-high", "value"),
              prevent_initial_call=True)
def save_range(_, low, high):
    return monitor.set_range(low, high)


@app.callback(Output("fault-result", "children"),
              Input({"type": "fault", "key": ALL}, "n_clicks"), prevent_initial_call=True)
def inject_fault(_):
    if not ctx.triggered_id:
        return ""
    return monitor.set_fault(ctx.triggered_id["key"])


def main():
    parser = argparse.ArgumentParser(description="Biodigester monitor")
    parser.add_argument("--demo", action="store_true",
                        help="also start the controller and simulated sensors")
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
        # Keep one process and disable the reloader so MQTT runs only once.
        app.run(debug=False, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)
    finally:
        # Stop only the demo processes started by this dashboard.
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
