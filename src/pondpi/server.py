import argparse
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import serial
from flask import Flask, jsonify, request

from pondpi import read_sensor, sensor_mode, sensor_power
from pondpi.commit_sha import read_commit_sha
from pondpi.duration import format_duration
from pondpi.sensors.a02yyuw_sensor import A02YYUWSensor
from pondpi.signal_processor_config import load_signal_processors

app = Flask(__name__)

# Comfortably above the sensor's ~100ms response time and our 150ms default
# poll interval -- used to flag /health degraded if no valid "raw" reading
# has landed in this long. A separate, driver-owned threshold of the same
# name governs each sensor's own internal resync behavior (see
# sensors/a02yyuw_sensor.py) -- the two are conceptually distinct even
# though they default to the same value.
STALE_READING_THRESHOLD_S = 3.0

_state_lock = threading.Lock()
_state = {
    "instantaneous_mm": None,
    "rolling_avg_mm": None,
    "processed_mm": None,
    "processors": {},
    "processor_names": [],
    "emit_flags": {},
    "configs": {},
    "primary_name": None,
    "polling_interval_ms": None,
    "commit_sha": read_commit_sha(Path.cwd()),
    "last_reading_monotonic": None,
    "last_reset_at": None,
}

_poll_thread = None
_sensor = None
_reset_lock = threading.Lock()
_started_at = datetime.now(timezone.utc)
_started_monotonic = time.monotonic()


def poll_sensor(sensor, processors, primary_name, stop_event, poll_interval_s):
    """Sensor-agnostic polling loop: repeatedly calls `sensor.read()` and
    routes whichever named signals it returns into shared state. "raw"
    readings are run through the configured signal processor pipeline;
    "processed" readings (if the sensor reports any -- not every driver
    will) are cached as-is, since a sensor's own onboard smoothing isn't
    something further Pi-side processing should second-guess."""
    while not stop_event.is_set():
        readings = sensor.read()

        if "raw" in readings:
            distance_mm = readings["raw"]
            results = {}
            for name, processor in processors.items():
                results[name] = {"value": processor.add(distance_mm), **processor.extra_state()}

            with _state_lock:
                _state["instantaneous_mm"] = distance_mm
                _state["processors"] = results
                _state["rolling_avg_mm"] = results[primary_name]["value"]
                _state["last_reading_monotonic"] = time.monotonic()

        if "processed" in readings:
            with _state_lock:
                _state["processed_mm"] = readings["processed"]

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

    last_reading_monotonic = _state["last_reading_monotonic"]
    if last_reading_monotonic is None:
        last_reading_age_s = None
        stale = False
    else:
        last_reading_age_s = round(time.monotonic() - last_reading_monotonic, 1)
        stale = last_reading_age_s > STALE_READING_THRESHOLD_S

    status = "ok" if poller_alive and not stale else "degraded"

    last_reset_at = _state["last_reset_at"]

    payload = jsonify(
        status=status,
        poller_alive=poller_alive,
        last_reading_age_s=last_reading_age_s,
        last_reset_at=last_reset_at.isoformat() if last_reset_at else None,
        started_at=_started_at.isoformat(),
        uptime_seconds=uptime_seconds,
        uptime_human=format_duration(uptime_seconds),
        processors=_state["processor_names"],
        commit_sha=_state["commit_sha"],
    )
    return payload if status == "ok" else (payload, 503)


@app.route("/reset", methods=["POST"])
def reset():
    """Power-cycles the sensor to force a hardware reset -- e.g. if it
    appears wedged/stuck and a serial buffer flush alone hasn't helped.
    Not every sensor driver supports this; returns 501 if the current
    one doesn't."""
    if not _sensor.supports_reset:
        return jsonify(error="sensor does not support reset"), 501

    with _reset_lock:
        _sensor.reset()
        reset_at = datetime.now(timezone.utc)
        with _state_lock:
            _state["last_reset_at"] = reset_at

    return jsonify(status="reset", reset_at=reset_at.isoformat())


@app.route("/level")
def level():
    mode = request.args.get("mode", "raw")

    if mode == "processed":
        with _state_lock:
            processed_mm = _state["processed_mm"]
            if processed_mm is None:
                return jsonify(error="no readings yet"), 503

            return jsonify(
                measure_name="level",
                units="cm",
                mode="processed",
                distance_cm=round(processed_mm / 10.0, 1),
            )

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
            mode="raw",
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
    global _poll_thread, _sensor

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
        "--mode-select-pin",
        type=int,
        default=25,
        help="BCM GPIO pin wired to the sensor's RX/mode-select line (default: 25)",
    )
    parser.add_argument(
        "--power-pin",
        type=int,
        default=24,
        help="BCM GPIO pin wired to the sensor's power supply, used by POST /reset (default: 24)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use synthetic sensor data instead of a real serial connection (for local development)",
    )
    args = parser.parse_args()

    if args.simulate:
        ser = read_sensor.SimulatedSerial()
        mode_controller = sensor_mode.NullModeController()
        power_controller = sensor_power.NullPowerController()
    else:
        # Initialize serial port at 9600 baud rate
        ser = serial.Serial('/dev/serial0', baudrate=9600, timeout=1)
        mode_controller = sensor_mode.GpioModeController(args.mode_select_pin)
        power_controller = sensor_power.GpioPowerController(args.power_pin)

    _sensor = A02YYUWSensor(ser, mode_controller, power_controller)

    processors, primary_name, emit_flags, configs = load_signal_processors(args.processors_config)
    _state["processor_names"] = list(processors)
    _state["emit_flags"] = emit_flags
    _state["configs"] = configs
    _state["primary_name"] = primary_name
    _state["polling_interval_ms"] = args.polling_interval_ms

    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_sensor,
        args=(_sensor, processors, primary_name, stop_event, args.polling_interval_ms / 1000),
        daemon=True,
    )
    poll_thread.start()
    _poll_thread = poll_thread

    try:
        app.run(host=args.host, port=args.port)
    finally:
        stop_event.set()
        _sensor.close()


if __name__ == "__main__":
    main()
