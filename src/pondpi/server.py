import argparse
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import serial
from flask import Flask, jsonify

from pondpi import read_sensor
from pondpi.commit_sha import read_commit_sha
from pondpi.duration import format_duration
from pondpi.signal_processor_config import load_signal_processors

app = Flask(__name__)

_state_lock = threading.Lock()
_state = {
    "instantaneous_mm": None,
    "rolling_avg_mm": None,
    "processors": {},
    "processor_names": [],
    "emit_flags": {},
    "configs": {},
    "primary_name": None,
    "polling_interval_ms": None,
    "commit_sha": read_commit_sha(Path.cwd()),
}

_poll_thread = None
_started_at = datetime.now(timezone.utc)
_started_monotonic = time.monotonic()


def poll_sensor(ser, processors, primary_name, stop_event, poll_interval_s):
    while not stop_event.is_set():
        distance_mm = read_sensor.read_frame(ser)

        if distance_mm is not None and read_sensor.is_valid_reading(distance_mm):
            results = {}
            for name, processor in processors.items():
                results[name] = {"value": processor.add(distance_mm), **processor.extra_state()}

            with _state_lock:
                _state["instantaneous_mm"] = distance_mm
                _state["processors"] = results
                _state["rolling_avg_mm"] = results[primary_name]["value"]

        time.sleep(poll_interval_s)


def _processor_output(result):
    """Converts one processor's cached poll_sensor() result ({"value": mm,
    **extra_state}) into its /level and /diag output shape
    ({"distance_cm": cm, **extra_state})."""
    extra_state = {k: v for k, v in result.items() if k != "value"}
    return {"distance_cm": round(result["value"] / 10.0, 1), **extra_state}


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
        uptime_human=format_duration(uptime_seconds),
        processors=_state["processor_names"],
        commit_sha=_state["commit_sha"],
    )
    return payload if poller_alive else (payload, 503)


@app.route("/level")
def level():
    with _state_lock:
        if _state["instantaneous_mm"] is None:
            return jsonify(error="no readings yet"), 503

        signals = {}
        for name, result in _state["processors"].items():
            if _state["emit_flags"].get(name, True):
                signals[name] = _processor_output(result)["distance_cm"]

        rolling_avg_distance_cm = round(_state["rolling_avg_mm"] / 10.0, 1)

        return jsonify(
            measure_name="level",
            units="cm",
            polling_interval_ms=_state["polling_interval_ms"],
            primary_signal={"value": rolling_avg_distance_cm, "name": _state["primary_name"]},
            signals=signals,
        )


@app.route("/diag")
def diag():
    """Full config + live output for every configured signal processor,
    regardless of `emit` -- unlike /level's `signals`, which only shows
    processors meant to be read as final output."""
    with _state_lock:
        if _state["instantaneous_mm"] is None:
            return jsonify(error="no readings yet"), 503

        processors = {
            name: {"config": _state["configs"][name], "output": _processor_output(result)}
            for name, result in _state["processors"].items()
        }

        return jsonify(processors=processors)


def main():
    global _poll_thread

    parser = argparse.ArgumentParser(description="A02YYUW distance HTTP server with rolling average smoothing")
    parser.add_argument(
        "--processors-config",
        type=Path,
        default=Path.cwd() / "config" / "processors.yaml",
        help="path to the YAML file configuring level-processing processors (default: config/processors.yaml)",
    )
    parser.add_argument(
        "--polling-interval-ms",
        type=int,
        default=150,
        help="how often to check for a new sensor reading, in milliseconds (default: 150)",
    )
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

    processors, primary_name, emit_flags, configs = load_signal_processors(args.processors_config)
    _state["processor_names"] = list(processors)
    _state["emit_flags"] = emit_flags
    _state["configs"] = configs
    _state["primary_name"] = primary_name
    _state["polling_interval_ms"] = args.polling_interval_ms

    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_sensor,
        args=(ser, processors, primary_name, stop_event, args.polling_interval_ms / 1000),
        daemon=True,
    )
    poll_thread.start()
    _poll_thread = poll_thread

    try:
        app.run(host=args.host, port=args.port)
    finally:
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
