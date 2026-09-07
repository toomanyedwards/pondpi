from pondpi.sensors.a02yyuw_sensor import read_sensor


class FakeSerial:
    def __init__(self, data=b""):
        self._data = data
        self.reset_count = 0

    @property
    def in_waiting(self):
        return len(self._data)

    def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def reset_input_buffer(self):
        self.reset_count += 1
        self._data = b""


def _frame(data_h, data_l):
    checksum = read_sensor.calculate_checksum(data_h, data_l)
    return bytes([0xFF, data_h, data_l, checksum])


def test_calculate_checksum_known_value():
    assert read_sensor.calculate_checksum(0x01, 0x2C) == (0xFF + 0x01 + 0x2C) & 0xFF


def test_parse_distance_mm():
    assert read_sensor.parse_distance_mm(0x01, 0x2C) == 0x012C


def test_is_valid_reading_rejects_dead_zone():
    assert not read_sensor.is_valid_reading(0)
    assert not read_sensor.is_valid_reading(30)


def test_is_valid_reading_accepts_normal_range():
    assert read_sensor.is_valid_reading(300)


def test_read_frame_returns_distance_for_valid_frame():
    fake = FakeSerial(_frame(0x01, 0x2C))
    assert read_sensor.read_frame(fake) == 0x012C


def test_read_frame_returns_none_on_bad_checksum():
    bad = bytes([0xFF, 0x01, 0x2C, 0x00])
    fake = FakeSerial(bad)
    assert read_sensor.read_frame(fake) is None


def test_read_frame_resets_buffer_on_bad_checksum():
    bad = bytes([0xFF, 0x01, 0x2C, 0x00])
    fake = FakeSerial(bad)
    read_sensor.read_frame(fake)
    assert fake.reset_count == 1


def test_read_frame_returns_none_when_not_enough_bytes():
    fake = FakeSerial(b"\xff\x01")
    assert read_sensor.read_frame(fake) is None


def test_read_frame_returns_none_when_header_missing():
    fake = FakeSerial(bytes([0x00, 0x01, 0x2C, 0x2D]))
    assert read_sensor.read_frame(fake) is None


def test_simulated_serial_produces_readable_frames():
    sim = read_sensor.SimulatedSerial(base_mm=800, amplitude_mm=0, noise_mm=0)
    assert read_sensor.read_frame(sim) == 800


def test_simulated_serial_stays_within_sensor_range():
    sim = read_sensor.SimulatedSerial(base_mm=800, amplitude_mm=200, noise_mm=5)
    for _ in range(20):
        distance_mm = read_sensor.read_frame(sim)
        assert distance_mm is not None
        assert 0 <= distance_mm <= 4500
