import argparse
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

from pondpi.commit_sha import read_commit_sha
from pondpi.duration import format_duration
from pondpi.sensor_config import load_sensors

app = Flask(__name__)

# Comfortably above the sensor's ~100ms response time and our 150ms default
# poll interval -- used to flag a sensor degraded in /health if no valid
# "raw" reading has landed in this long. A separate, driver-owned threshold
# of the same name governs each A02YYUW's own internal resync behavior (see
# sensors/a02yyuw_sensor.py) -- the two are conceptually distinct even
# though they default to the same value.
STALE_READING_THRESHOLD_S = 3.0

_state_lock = threading.Lock()
_state = {}  # dict[sensor_name -> per-sensor state, see _new_sensor_state()]

_sensors = {}  # dict[sensor_name -> LevelSensor driver instance]
_signal_objects = {}  # dict[signal_name -> LevelSignal instance], global since signal names are unique file-wide
_poll_threads = {}  # dict[sensor_name -> Thread running poll_sensor()]
_polling_signal_threads = {}  # dict[signal_name -> Thread running that signal's own run_loop()]
_signal_owner = {}  # dict[signal_name -> sensor_name], global since signal names are unique file-wide
_commit_sha = read_commit_sha(Path.cwd())
_started_at = datetime.now(timezone.utc)
_started_monotonic = time.monotonic()


def _new_sensor_state():
    return {
        "signals": {},
        "signal_names": [],
        "configs": {},
        "last_reading_monotonic": None,
        "last_reset_at": None,
    }


def poll_sensor(name, sensor, signals, configs, stop_event, poll_interval_s):
    """Sensor-agnostic polling loop for one named sensor: repeatedly
    calls `sensor.read()` and routes whichever named readings it
    returns into that sensor's own state slot. Each reading key (e.g.
    "raw", "processed" -- see LevelSensor.read()) only feeds the
    signals rooted at that same `mode` (`configs[sname]["mode"]`, see
    signal_config.py) -- a signal either reads that reading directly (a
    `sensor`-type signal, config's `configs[name]` has no `"input"`) or
    reads whatever its `input:`-named signal just computed this same
    pass (`configs[name]["input"]`, already resolved into `results`
    since `signals`' iteration order is a valid dependency order).
    Signals rooted at other modes keep their last-computed value
    untouched -- results are merged into this sensor's state, not
    replaced wholesale, since "raw"- and "processed"-rooted signals
    update on different, independent cadences (see each signal's own
    `at` timestamp on /diag and /signals/<name> to tell how fresh a
    given one actually is).

    A signal with `owns_read_loop = True` (currently just
    RollingAverageSignal) is skipped here entirely -- it's fed by its
    own dedicated thread instead (see server.py's `main()`, which spawns
    one such thread per owns_read_loop signal in the sensor's group),
    sampling its `input:` signal's cached output on its own pace rather
    than being pushed a value on every one of this loop's much faster
    ticks.

    One of these runs per configured sensor, each in its own thread."""
    while not stop_event.is_set():
        readings = sensor.read()

        for reading_key, distance_mm in readings.items():
            at = datetime.now(timezone.utc).isoformat()
            results = {}
            for sname, signal in signals.items():
                if signal.owns_read_loop:
                    continue
                if configs[sname]["mode"] != reading_key:
                    continue
                input_name = configs[sname].get("input")
                value = distance_mm if input_name is None else results[input_name]["value"]
                results[sname] = {"value": signal.add(value), "at": at, **signal.extra_state()}

            with _state_lock:
                if results:
                    _state[name]["signals"].update(results)
                if reading_key == "raw":
                    _state[name]["last_reading_monotonic"] = time.monotonic()

        time.sleep(poll_interval_s)


def _signal_result(sensor_name, signal_name):
    """A signal's current {"value", "at", ...} result -- from its own
    background-maintained cache if it owns a read loop (just
    RollingAverageSignal today), or from poll_sensor()'s shared
    per-sensor cache otherwise. Returns None if there's no reading yet
    either way."""
    signal = _signal_objects[signal_name]
    if signal.owns_read_loop:
        return signal.current()

    with _state_lock:
        return _state[sensor_name]["signals"].get(signal_name)


def _signal_output(result, unit):
    """Converts one signal's cached result ({"value": mm, "at": ...,
    **extra_state}) into its /diag and /signals/<name> output shape
    ({"value": cm, "unit": ..., "at": ..., **extra_state}). `unit` is
    that signal's own configured/derived unit (see signal_config.py's
    `_config_summary()`) -- static per-signal metadata, not something
    recomputed every cycle, so it's passed in rather than read off
    `result`."""
    extra_state = {k: v for k, v in result.items() if k not in ("value", "at")}
    return {"value": round(result["value"] / 10.0, 1), "unit": unit, "at": result["at"], **extra_state}


@app.route("/health")
def health():
    sensors_health = {}
    overall_ok = True

    for name in _sensors:
        poll_thread = _poll_threads.get(name)
        poller_alive = poll_thread is not None and poll_thread.is_alive()

        last_reading_monotonic = _state[name]["last_reading_monotonic"]
        if last_reading_monotonic is None:
            last_reading_age_s = None
            stale = False
        else:
            last_reading_age_s = round(time.monotonic() - last_reading_monotonic, 1)
            stale = last_reading_age_s > STALE_READING_THRESHOLD_S

        status = "ok" if poller_alive and not stale else "degraded"
        overall_ok = overall_ok and status == "ok"

        last_reset_at = _state[name]["last_reset_at"]

        sensors_health[name] = {
            "status": status,
            "poller_alive": poller_alive,
            "last_reading_age_s": last_reading_age_s,
            "last_reset_at": last_reset_at.isoformat() if last_reset_at else None,
            "signals": _state[name]["signal_names"],
        }

    status = "ok" if overall_ok else "degraded"
    uptime_seconds = round(time.monotonic() - _started_monotonic, 1)

    payload = jsonify(
        status=status,
        started_at=_started_at.isoformat(),
        uptime_seconds=uptime_seconds,
        uptime_human=format_duration(uptime_seconds),
        commit_sha=_commit_sha,
        sensors=sensors_health,
    )
    return payload if status == "ok" else (payload, 503)


def _reset_sensor(name, sensor):
    """Power-cycles one sensor and records when. No locking needed here
    -- LevelSensor implementations are responsible for their own
    hardware-access safety (see sensors/base.py's docstring); server.py
    just calls reset() and trusts it's safe to call concurrently with
    that sensor's own poll_sensor() thread."""
    sensor.reset()
    reset_at = datetime.now(timezone.utc)
    with _state_lock:
        _state[name]["last_reset_at"] = reset_at
    return reset_at


def _reset_response(name):
    sensor = _sensors.get(name)
    if sensor is None:
        return jsonify(error=f"unknown sensor '{name}'"), 404
    if not sensor.supports_reset:
        return jsonify(error="sensor does not support reset"), 501

    reset_at = _reset_sensor(name, sensor)
    return jsonify(status="reset", sensor=name, reset_at=reset_at.isoformat())


@app.route("/reset", methods=["POST"])
def reset():
    """Power-cycles every configured sensor that supports it, to force a
    hardware reset -- e.g. if one or more appear wedged/stuck and a
    serial buffer flush alone hasn't helped. Sensors that don't support
    it (checked via `supports_reset`) are reported as `"not_supported"`
    rather than failing the whole request -- one sensor lacking the
    capability shouldn't block resetting the others. See POST
    /sensors/<name>/reset to target exactly one sensor instead."""
    results = {}
    for name, sensor in _sensors.items():
        if not sensor.supports_reset:
            results[name] = {"status": "not_supported"}
            continue
        reset_at = _reset_sensor(name, sensor)
        results[name] = {"status": "reset", "reset_at": reset_at.isoformat()}

    return jsonify(sensors=results)


@app.route("/sensors/<name>/reset", methods=["POST"])
def sensor_reset(name):
    return _reset_response(name)


def _sensor_diag_signals(name):
    """Config + live output for every one of this sensor's configured
    signals that has a reading yet (empty dict if none do)."""
    signals = {}
    for sname in _state[name]["signal_names"]:
        result = _signal_result(name, sname)
        if result is None:
            continue
        unit = _state[name]["configs"][sname]["unit"]
        signals[sname] = {
            "config": _state[name]["configs"][sname],
            "output": _signal_output(result, unit),
        }
    return signals


@app.route("/diag")
def diag():
    """Diagnostic view for every configured sensor at once. See GET
    /sensors/<name>/diag to target one sensor individually."""
    return jsonify(sensors={name: _sensor_diag_signals(name) for name in _sensors})


@app.route("/sensors/<name>/diag")
def sensor_diag(name):
    if name not in _state:
        return jsonify(error=f"unknown sensor '{name}'"), 404
    signals = _sensor_diag_signals(name)
    if not signals:
        return jsonify(error="no readings yet"), 503
    return jsonify(signals=signals)


@app.route("/sensors")
def sensors_list():
    return jsonify(sensors=list(_sensors))


@app.route("/signals")
def signals_list():
    return jsonify(signals=list(_signal_owner))


@app.route("/signals/<name>")
def signal_detail(name):
    """A single signal's current value plus whatever else its own
    `extra_state()` reports (window_size, samples_in_window, alpha,
    sensor, ...) -- unlike /diag's per-signal `output`, this isn't
    nested under a `config`/`output` split, since there's exactly one
    signal here, not a whole sensor's worth. See GET /signals/<name>/diag
    for this signal's effective config alongside the same output."""
    sensor_name = _signal_owner.get(name)
    if sensor_name is None:
        return jsonify(error=f"unknown signal '{name}'"), 404

    result = _signal_result(sensor_name, name)
    if result is None:
        return jsonify(error="no readings yet"), 503

    unit = _state[sensor_name]["configs"][name]["unit"]
    payload = {"name": name, "sensor": sensor_name}
    payload.update(_signal_output(result, unit))
    return jsonify(payload)


@app.route("/signals/<name>/diag")
def signal_diag(name):
    sensor_name = _signal_owner.get(name)
    if sensor_name is None:
        return jsonify(error=f"unknown signal '{name}'"), 404

    result = _signal_result(sensor_name, name)
    if result is None:
        return jsonify(error="no readings yet"), 503

    config = _state[sensor_name]["configs"][name]
    return jsonify(
        name=name,
        sensor=sensor_name,
        config=config,
        output=_signal_output(result, config["unit"]),
    )


def main():
    parser = argparse.ArgumentParser(description="Multi-sensor water level HTTP server")
    parser.add_argument(
        "--sensors-config",
        type=Path,
        default=Path.cwd() / "config" / "sensors.yaml",
        help="path to the YAML file configuring sensors and their level-processing pipelines (default: config/sensors.yaml)",
    )
    parser.add_argument(
        "--polling-interval-ms",
        type=int,
        default=150,
        help="how often to check every sensor for a new reading, in milliseconds (default: 150)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="address to bind the HTTP server to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="port to bind the HTTP server to (default: 8080)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use synthetic sensor data instead of opening real hardware, for every configured sensor (for local development)",
    )
    args = parser.parse_args()

    sensor_configs = load_sensors(args.sensors_config, simulate=args.simulate)

    stop_event = threading.Event()
    for name, cfg in sensor_configs.items():
        _sensors[name] = cfg["driver"]

        _state[name] = _new_sensor_state()
        _state[name]["signal_names"] = list(cfg["signals"])
        _state[name]["configs"] = cfg["configs"]

        for signal_name, signal_obj in cfg["signals"].items():
            _signal_owner[signal_name] = name
            _signal_objects[signal_name] = signal_obj

        poll_thread = threading.Thread(
            target=poll_sensor,
            args=(
                name,
                cfg["driver"],
                cfg["signals"],
                cfg["configs"],
                stop_event,
                args.polling_interval_ms / 1000,
            ),
            daemon=True,
        )
        poll_thread.start()
        _poll_threads[name] = poll_thread

        for signal_name, signal_obj in cfg["signals"].items():
            if not signal_obj.owns_read_loop:
                continue
            input_name = cfg["configs"][signal_name]["input"]

            def get_raw_value(sensor_name=name, input_name=input_name):
                with _state_lock:
                    result = _state[sensor_name]["signals"].get(input_name)
                    return result["value"] if result else None

            polling_signal_thread = threading.Thread(
                target=signal_obj.run_loop,
                args=(stop_event, get_raw_value),
                daemon=True,
            )
            polling_signal_thread.start()
            _polling_signal_threads[signal_name] = polling_signal_thread

    try:
        app.run(host=args.host, port=args.port)
    finally:
        stop_event.set()
        for sensor in _sensors.values():
            sensor.close()


if __name__ == "__main__":
    main()
