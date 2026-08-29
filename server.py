import argparse
import threading
import time
from datetime import datetime, timezone

import serial
from flask import Flask, jsonify

import read_sensor
from rolling_average import RollingAverage

app = Flask(__name__)

POLL_INTERVAL_S = 0.01

_state_lock = threading.Lock()
_state = {
    "instantaneous_mm": None,
    "rolling_avg_mm": None,
    "samples_in_rolling_window": 0,
    "rolling_window_size": None,
}

_poll_thread = None
_started_at = datetime.now(timezone.utc)
_started_monotonic = time.monotonic()


def poll_sensor(ser, rolling_avg, stop_event):
    while not stop_event.is_set():
        distance_mm = read_sensor.read_frame(ser)

        if distance_mm is not None and read_sensor.is_valid_reading(distance_mm):
            avg_mm = rolling_avg.add(distance_mm)
            with _state_lock:
                _state["instantaneous_mm"] = distance_mm
                _state["rolling_avg_mm"] = avg_mm
                _state["samples_in_rolling_window"] = rolling_avg.count

        time.sleep(POLL_INTERVAL_S)


@app.route("/health")
def health():
    poller_alive = _poll_thread is not None and _poll_thread.is_alive()
    uptime_seconds = round(time.monotonic() - _started_monotonic, 1)
    status = "ok" if poller_alive else "degraded"

    payload = jsonify(
        status=status,
        poller_alive=poller_alive,
        started_at=_started_at.isoformat(),
        uptime_seconds=uptime_seconds,
    )
    return payload if poller_alive else (payload, 503)


@app.route("/level")
def level():
    with _state_lock:
        if _state["instantaneous_mm"] is None:
            return jsonify(error="no readings yet"), 503

        return jsonify(
            instantaneous_distance_cm=round(_state["instantaneous_mm"] / 10.0, 1),
            rolling_avg_distance_cm=round(_state["rolling_avg_mm"] / 10.0, 1),
            rolling_window_size=_state["rolling_window_size"],
            samples_in_rolling_window=_state["samples_in_rolling_window"],
            polling_interval_ms=round(POLL_INTERVAL_S * 1000),
        )


def main():
    global _poll_thread

    parser = argparse.ArgumentParser(description="A02YYUW distance HTTP server with rolling average smoothing")
    parser.add_argument("--window-size", type=int, default=25, help="number of readings to average over (default: 25)")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind the HTTP server to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="port to bind the HTTP server to (default: 8080)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use synthetic sensor data instead of a real serial connection (for local development)",
    )
    args = parser.parse_args()

    if args.simulate:
        ser = read_sensor.SimulatedSerial()
    else:
        # Initialize serial port at 9600 baud rate
        ser = serial.Serial('/dev/serial0', baudrate=9600, timeout=1)

    _state["rolling_window_size"] = args.window_size
    rolling_avg = RollingAverage(args.window_size)

    stop_event = threading.Event()
    poll_thread = threading.Thread(target=poll_sensor, args=(ser, rolling_avg, stop_event), daemon=True)
    poll_thread.start()
    _poll_thread = poll_thread

    try:
        app.run(host=args.host, port=args.port)
    finally:
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
