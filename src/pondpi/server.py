import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

from pondpi.commit_sha import read_commit_sha
from pondpi.duration import format_duration
from pondpi.sensor_config import load_sensors

app = Flask(__name__)

_state = {}  # dict[sensor_name -> per-sensor state, see _new_sensor_state()]

_sensors = {}  # dict[sensor_name -> Sensor driver instance]
_signal_objects = {}  # dict[signal_name -> Signal instance], global since signal names are unique file-wide
_signal_owner = {}  # dict[signal_name -> sensor_name], global since signal names are unique file-wide
_commit_sha = read_commit_sha(Path.cwd())
_started_at = datetime.now(timezone.utc)
_started_monotonic = time.monotonic()


def _new_sensor_state():
    return {
        "signal_names": [],
        "configs": {},
    }


def _signal_result(signal_name):
    """A signal's current {"value", "at", ...} result (see
    Signal.read()), or None if there's no reading yet."""
    return _signal_objects[signal_name].read()


def _signal_output(result, unit):
    """Converts one signal's result ({"value": ..., "at": ...,
    **extra_state}) into its /diag and /signals/<name> output shape
    ({"value": ..., "unit": ..., "at": ..., **extra_state}). `result["value"]`
    is already in the signal's own unit -- SensorSignal converts a
    sensor's canonical millimeter reading into it once, at the boundary
    (see signals/sensor_signal.py) -- so server.py only rounds for
    display, it does no unit-specific math of its own. `unit` is that
    signal's own configured/derived unit (see signal_config.py's
    `_config_summary()`) -- static per-signal metadata, not something
    recomputed every cycle, so it's passed in rather than read off
    `result`."""
    extra_state = {k: v for k, v in result.items() if k not in ("value", "at")}
    return {"value": round(result["value"], 1), "unit": unit, "at": result["at"], **extra_state}


@app.route("/health")
def health():
    sensors_health = {}
    overall_ok = True

    for name, sensor in _sensors.items():
        last_reading_monotonic = sensor.last_reading_monotonic()
        last_reading_age_s = (
            round(time.monotonic() - last_reading_monotonic, 1) if last_reading_monotonic is not None else None
        )

        # The ok/degraded verdict itself is entirely the sensor's own
        # call (is_healthy(), see sensors/base.py) -- server.py holds no
        # staleness threshold and makes no judgment of its own about
        # what counts as stale for a given sensor type.
        status = "ok" if sensor.is_healthy() else "degraded"
        overall_ok = overall_ok and status == "ok"

        last_reset_at = sensor.last_reset_at()

        sensors_health[name] = {
            "status": status,
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
    """Resets one sensor -- whatever that means for its own driver (see
    sensors/base.py's `reset()`/`reset_hardware()`; server.py has no
    notion of the specifics) -- then cascades: resets every signal
    rooted at this sensor too, since accumulated signal state (a rolling
    window, an average) built from readings around the time something
    looked wrong enough to warrant a reset is suspect too."""
    sensor.reset()
    for signal_name in _state[name]["signal_names"]:
        _signal_objects[signal_name].reset()
    return sensor.last_reset_at()


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
    """Resets every configured sensor that supports it -- e.g. if one or
    more appear wedged/stuck. Sensors that don't support it (checked via
    `supports_reset`) are reported as `"not_supported"` rather than
    failing the whole request -- one sensor lacking the capability
    shouldn't block resetting the others. See POST /sensors/<name>/reset
    to target exactly one sensor instead."""
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
        result = _signal_result(sname)
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

    result = _signal_result(name)
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

    result = _signal_result(name)
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
    parser.add_argument("--host", default="0.0.0.0", help="address to bind the HTTP server to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="port to bind the HTTP server to (default: 8080)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use synthetic sensor data instead of opening real hardware, for every configured sensor (for local development)",
    )
    args = parser.parse_args()

    # load_sensors() fully constructs and wires up every sensor and
    # signal -- each one's own background read thread is already
    # running by the time this returns. Nothing here touches threads at
    # all; see sensors/base.py's and signals/base.py's __init__()s.
    sensor_configs = load_sensors(args.sensors_config, simulate=args.simulate)

    for name, cfg in sensor_configs.items():
        _sensors[name] = cfg["driver"]

        _state[name] = _new_sensor_state()
        _state[name]["signal_names"] = list(cfg["signals"])
        _state[name]["configs"] = cfg["configs"]

        for signal_name, signal_obj in cfg["signals"].items():
            _signal_owner[signal_name] = name
            _signal_objects[signal_name] = signal_obj

    try:
        app.run(host=args.host, port=args.port)
    finally:
        for sensor in _sensors.values():
            sensor.close()


if __name__ == "__main__":
    main()
