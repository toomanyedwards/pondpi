import pytest

from pondpi.sensor_config import _build_on_reading, load_sensors
from pondpi.sensors.a02yyuw_sensor import A02YYUWSensor


def write_yaml(tmp_path, content):
    path = tmp_path / "sensors.yaml"
    path.write_text(content)
    return path


class _FakeSignal:
    owns_read_loop = False

    def __init__(self, transform=lambda value: value):
        self._transform = transform
        self.fed_values = []

    def feed(self, value):
        result = self._transform(value)
        self.fed_values.append(result)
        return result


class _FakePollingSignal:
    """Stand-in for an owns_read_loop signal -- _build_on_reading()
    must never call feed() on this kind; it's fed by its own thread
    instead (see LevelSignal)."""

    owns_read_loop = True

    def __init__(self):
        self.fed_values = []

    def feed(self, value):
        self.fed_values.append(value)


def test_loads_valid_config(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params: {}
        signals:
          - name: raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: instantaneous_raw
            type: rolling_average
            input: raw
            params:
              window_size: 5
              poll_interval_ms: 1000
        """,
    )

    sensors = load_sensors(path, simulate=True)

    assert set(sensors) == {"pond_main"}
    assert isinstance(sensors["pond_main"]["driver"], A02YYUWSensor)
    assert set(sensors["pond_main"]["signals"]) == {"raw", "instantaneous_raw"}


def test_loads_multiple_sensors(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params: {}
          - name: rain_barrel
            type: a02yyuw
            params: {}
        signals:
          - name: pond_raw_sensor
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: pond_raw
            type: rolling_average
            input: pond_raw_sensor
            params:
              window_size: 5
              poll_interval_ms: 1000
          - name: barrel_raw_sensor
            type: sensor
            params:
              sensor: rain_barrel
              unit: cm
          - name: barrel_raw
            type: rolling_average
            input: barrel_raw_sensor
            params:
              window_size: 5
              poll_interval_ms: 1000
        """,
    )

    sensors = load_sensors(path, simulate=True)

    assert set(sensors) == {"pond_main", "rain_barrel"}
    # Distinct instances -- not the same driver object reused.
    assert sensors["pond_main"]["driver"] is not sensors["rain_barrel"]["driver"]
    assert set(sensors["pond_main"]["signals"]) == {"pond_raw_sensor", "pond_raw"}
    assert set(sensors["rain_barrel"]["signals"]) == {"barrel_raw_sensor", "barrel_raw"}


def test_simulate_true_ignores_hardware_params(tmp_path):
    # mode_select_pin/power_pin would try to drive real GPIO if honored
    # under --simulate -- confirm construction succeeds regardless (no
    # GPIO hardware available in a test environment).
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params:
              serial_port: /dev/does_not_exist
              mode_select_pin: 99
              power_pin: 98
        signals:
          - name: raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: instantaneous_raw
            type: rolling_average
            input: raw
            params:
              window_size: 5
              poll_interval_ms: 1000
        """,
    )

    sensors = load_sensors(path, simulate=True)

    assert isinstance(sensors["pond_main"]["driver"], A02YYUWSensor)


def test_unknown_sensor_type_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: not_a_real_sensor
            params: {}
        """,
    )

    with pytest.raises(ValueError, match="unknown type 'not_a_real_sensor'"):
        load_sensors(path, simulate=True)


def test_duplicate_sensor_name_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params: {}
          - name: pond_main
            type: a02yyuw
            params: {}
        """,
    )

    with pytest.raises(ValueError, match="duplicate sensor name 'pond_main'"):
        load_sensors(path, simulate=True)


def test_missing_name_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - type: a02yyuw
            params: {}
        """,
    )

    with pytest.raises(ValueError, match="missing 'name'"):
        load_sensors(path, simulate=True)


def test_empty_signals_list_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params: {}
        signals: []
        """,
    )

    with pytest.raises(ValueError, match="'signals' must be a non-empty list"):
        load_sensors(path, simulate=True)


def test_sensor_with_no_matching_signal_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        sensors:
          - name: pond_main
            type: a02yyuw
            params: {}
          - name: rain_barrel
            type: a02yyuw
            params: {}
        signals:
          - name: pond_raw_sensor
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: pond_raw
            type: rolling_average
            input: pond_raw_sensor
            params:
              window_size: 5
              poll_interval_ms: 1000
        """,
    )

    with pytest.raises(ValueError, match="sensor 'rain_barrel' has no signals rooted at it"):
        load_sensors(path, simulate=True)


def test_empty_sensors_list_raises(tmp_path):
    path = write_yaml(tmp_path, "sensors: []\n")

    with pytest.raises(ValueError, match="non-empty list"):
        load_sensors(path, simulate=True)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sensors(tmp_path / "does_not_exist.yaml", simulate=True)


def test_build_on_reading_feeds_matching_signals():
    signals = {"instantaneous_raw": _FakeSignal()}
    configs = {"instantaneous_raw": {"mode": "raw"}}
    on_reading = _build_on_reading(signals, configs)

    on_reading("raw", 100)

    assert signals["instantaneous_raw"].fed_values == [100]


def test_build_on_reading_downstream_signal_receives_upstream_signals_output():
    # "downstream" doubles whatever it's fed. If it wrongly received the
    # raw sensor reading directly instead of "root"'s own (already
    # doubled) output, it'd land on 200 instead of 400.
    signals = {
        "root": _FakeSignal(transform=lambda v: v * 2),
        "downstream": _FakeSignal(transform=lambda v: v * 2),
    }
    configs = {"root": {"mode": "raw"}, "downstream": {"mode": "raw", "input": "root"}}
    on_reading = _build_on_reading(signals, configs)

    on_reading("raw", 100)

    assert signals["downstream"].fed_values == [400]


def test_build_on_reading_skips_signals_that_own_their_own_read_loop():
    signals = {"instantaneous_raw": _FakeSignal(), "avg": _FakePollingSignal()}
    configs = {"instantaneous_raw": {"mode": "raw"}, "avg": {"mode": "raw", "input": "instantaneous_raw"}}
    on_reading = _build_on_reading(signals, configs)

    on_reading("raw", 100)

    assert signals["instantaneous_raw"].fed_values == [100]
    assert signals["avg"].fed_values == []


def test_build_on_reading_routes_each_reading_to_signals_rooted_at_its_own_mode():
    # A raw-rooted and a processed-rooted signal on the same sensor:
    # each should only be fed when its own reading key shows up,
    # independent of the other.
    signals = {"raw_sig": _FakeSignal(), "proc_sig": _FakeSignal()}
    configs = {"raw_sig": {"mode": "raw"}, "proc_sig": {"mode": "processed"}}
    on_reading = _build_on_reading(signals, configs)

    on_reading("raw", 100)
    on_reading("processed", 50)

    assert signals["raw_sig"].fed_values == [100]
    assert signals["proc_sig"].fed_values == [50]
