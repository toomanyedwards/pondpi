import argparse
import threading
import time

import serial
from flask import Flask, jsonify

import read_sensor
from rolling_average import RollingAverage

app = Flask(__name__)

_state_lock = threading.Lock()
_state = {
    "distance_mm": None,
    "distance_cm": None,
    "reading_count": 0,
    "window_size": None,
}

_poll_thread = None


def poll_sensor(ser, rolling_avg, stop_event):
    while not stop_event.is_set():
        distance_mm = read_sensor.read_frame(ser)

        if distance_mm is not None and read_sensor.is_valid_reading(distance_mm):
            avg_mm = rolling_avg.add(distance_mm)
            with _state_lock:
                _state["distance_mm"] = avg_mm
                _state["distance_cm"] = avg_mm / 10.0
                _state["reading_count"] = rolling_avg.count

        time.sleep(0.01)


@app.route("/health")
def health():
    poller_alive = _poll_thread is not None and _poll_thread.is_alive()

    if poller_alive:
        return jsonify(status="ok", poller_alive=True)

    return jsonify(status="degraded", poller_alive=False), 503


@app.route("/level")
def level():
    with _state_lock:
        if _state["distance_mm"] is None:
            return jsonify(error="no readings yet"), 503

        return jsonify(
            distance_mm=round(_state["distance_mm"]),
            distance_cm=round(_state["distance_cm"], 1),
            reading_count=_state["reading_count"],
            window_size=_state["window_size"],
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

    _state["window_size"] = args.window_size
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
