import pytest

from pondpi.sensor_config import load_sensors
from pondpi.sensors.a02yyuw_sensor import A02YYUWSensor


def write_yaml(tmp_path, content):
    path = tmp_path / "sensors.yaml"
    path.write_text(content)
    return path


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
