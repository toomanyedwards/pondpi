import pytest

from pondpi.sensors.etape_sensor import ads1263


class FakePin:
    """Stand-in for a gpiozero DigitalOutputDevice/DigitalInputDevice --
    just records on()/off() calls and lets a test drive `.value`
    directly for the input case (drdy_pin)."""

    def __init__(self):
        self.calls = []
        self.value = 0
        self.closed = False
        self.close_calls = 0

    def on(self):
        self.calls.append("on")

    def off(self):
        self.calls.append("off")

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeSPI:
    """Stand-in for spidev.SpiDev(). Every write is recorded; reads are
    served from a caller-primed `responses` queue, one list of bytes per
    expected readbytes() call, in order -- a test controls what the
    "chip" says back by populating this queue up front, same spirit as
    FakeSerial's buffer in test_a02yyuw_sensor.py."""

    def __init__(self, responses=None):
        self.max_speed_hz = None
        self.mode = None
        self.writes = []
        self._responses = list(responses or [])
        self.closed = False
        self.close_calls = 0

    def writebytes(self, data):
        self.writes.append(list(data))

    def readbytes(self, n):
        if not self._responses:
            raise AssertionError("FakeSPI.readbytes() called with no primed response left")
        response = self._responses.pop(0)
        assert len(response) == n, f"primed response length {len(response)} != requested {n}"
        return response

    def close(self):
        self.closed = True
        self.close_calls += 1


def _checksum_byte(value):
    total = 0
    v = value
    while v:
        total += v & 0xFF
        v >>= 8
    total += 0x9B
    return total & 0xFF


def _data_response(raw_value):
    """The 6-byte sequence _read_adc1_data() expects back across its two
    readbytes() calls: a 1-byte status (0x40 set, "ready") from the
    RDATA1 polling loop, then the 4 data bytes + checksum byte."""
    data_bytes = [(raw_value >> 24) & 0xFF, (raw_value >> 16) & 0xFF, (raw_value >> 8) & 0xFF, raw_value & 0xFF]
    return [[0x40]], [*data_bytes, _checksum_byte(raw_value & 0xFFFFFFFF)]


def _make_adc(raw_value, reference_voltage=5.08, drdy_ready=True):
    """Builds an ADS1263 whose init sequence succeeds (chip ID + every
    register write-back matches) and whose one subsequent read_voltage()
    call returns `raw_value`."""
    status_responses, data_response = _data_response(raw_value)

    # Init sequence reads: chip ID, then one read-back per
    # _write_reg_verified() call (MODE2, REFMUX, MODE0, MODE1) -- each
    # echoing back exactly what was just written, in the config's own
    # register order.
    responses = [
        [0x01 << 5],  # chip ID read -- expected id 0x01 after >>5
        [0x80 | (0 << 4) | 0x7],  # REG_MODE2 echoed back (data_rate_sps=100 -> code 0x7)
        [0x24],  # REG_REFMUX echoed back
        [0x03],  # REG_MODE0 echoed back
        [0x84],  # REG_MODE1 echoed back
    ]
    if drdy_ready:
        responses += status_responses  # the RDATA1 status-polling loop's one "ready" byte
        responses += [data_response]
    spi = FakeSPI(responses=responses)
    reset_pin, cs_pin, drdy_pin = FakePin(), FakePin(), FakePin()
    drdy_pin.value = 1 if drdy_ready else 0  # see ads1263._wait_drdy()'s docstring for this inversion
    adc = ads1263.ADS1263(spi, reset_pin, cs_pin, drdy_pin, reference_voltage=reference_voltage)
    return adc, spi, reset_pin, cs_pin, drdy_pin


def test_init_pulses_reset_pin_high_low_high():
    _, _, reset_pin, _, _ = _make_adc(0)
    assert reset_pin.calls == ["on", "off", "on"]


def test_init_configures_spi_speed_and_mode():
    _, spi, _, _, _ = _make_adc(0)
    assert spi.max_speed_hz == 2_000_000
    assert spi.mode == 0b01


def test_init_raises_on_unexpected_chip_id():
    spi = FakeSPI(responses=[[0x02 << 5]])  # wrong chip ID (expected 0x01 after >>5)
    with pytest.raises(RuntimeError, match="chip ID"):
        ads1263.ADS1263(spi, FakePin(), FakePin(), FakePin())


def test_init_raises_when_a_register_write_does_not_read_back():
    spi = FakeSPI(
        responses=[
            [0x01 << 5],  # chip ID ok
            [0x00],  # MODE2 read-back doesn't match what was written
        ]
    )
    with pytest.raises(RuntimeError, match="register.*write failed"):
        ads1263.ADS1263(spi, FakePin(), FakePin(), FakePin())


def test_init_rejects_unsupported_data_rate():
    with pytest.raises(ValueError, match="unsupported data_rate_sps"):
        ads1263.ADS1263(FakeSPI(), FakePin(), FakePin(), FakePin(), data_rate_sps=12345)


def test_read_voltage_converts_positive_code():
    # raw = 0x7fffffff (max positive code) at REF=5.08V -> ~5.08V
    adc, *_ = _make_adc(0x7FFFFFFF, reference_voltage=5.08)
    assert adc.read_voltage(0) == pytest.approx(5.08, abs=1e-6)


def test_read_voltage_converts_zero_code():
    adc, *_ = _make_adc(0x00000000)
    assert adc.read_voltage(0) == pytest.approx(0.0, abs=1e-6)


def test_read_voltage_converts_negative_code():
    # raw = 0x80000000 (most negative code) at REF=5.08V -> -5.08V
    adc, *_ = _make_adc(0x80000000, reference_voltage=5.08)
    assert adc.read_voltage(0) == pytest.approx(-5.08, abs=1e-6)


def test_read_voltage_selects_channel_via_inpmux():
    adc, spi, *_ = _make_adc(0)
    spi.writes.clear()
    adc.read_voltage(3)
    # (channel << 4) | 0x0a for channel=3 -> 0x3a; write is [WREG|reg, 0x00, data]
    assert [0x40 | 6, 0x00, 0x3A] in spi.writes


def test_read_voltage_rejects_out_of_range_channel():
    adc, *_ = _make_adc(0)
    with pytest.raises(ValueError, match="channel must be 0-9"):
        adc.read_voltage(10)


def test_read_voltage_raises_on_checksum_mismatch():
    status, _ = _data_response(0x00000001)
    bad_data_response = [0x00, 0x00, 0x00, 0x01, 0x00]  # wrong checksum byte
    responses = [
        [0x01 << 5],
        [0x80 | 0x7],
        [0x24],
        [0x03],
        [0x84],
        *status,
        bad_data_response,
    ]
    spi = FakeSPI(responses=responses)
    drdy_pin = FakePin()
    drdy_pin.value = 1
    adc = ads1263.ADS1263(spi, FakePin(), FakePin(), drdy_pin)

    with pytest.raises(OSError, match="checksum mismatch"):
        adc.read_voltage(0)


def test_wait_drdy_times_out_when_never_ready(monkeypatch):
    monkeypatch.setattr(ads1263, "_WAIT_DRDY_TIMEOUT_S", 0.05)  # keep this test fast
    adc, *_ = _make_adc(0, drdy_ready=False)
    with pytest.raises(TimeoutError, match="DRDY"):
        adc._wait_drdy()


def test_close_closes_spi_and_every_pin():
    adc, spi, reset_pin, cs_pin, drdy_pin = _make_adc(0)
    adc.close()
    assert spi.closed
    assert reset_pin.closed
    assert cs_pin.closed
    assert drdy_pin.closed


def test_close_is_idempotent():
    # Multiple EtapeSensors can share one real ADS1263 (see
    # etape_sensor/__init__.py's _get_or_build_shared_adc()), each
    # calling close() independently on shutdown -- the second call must
    # not double-close the same spi/pin objects.
    adc, spi, reset_pin, cs_pin, drdy_pin = _make_adc(0)
    adc.close()
    adc.close()
    assert spi.close_calls == 1
    assert reset_pin.close_calls == 1
    assert cs_pin.close_calls == 1
    assert drdy_pin.close_calls == 1


def test_checksum_ok_matches_known_good_value():
    raw = 0x0012AB34
    good_checksum = ads1263._checksum_ok(raw, _checksum_byte(raw))
    assert good_checksum is True
    assert ads1263._checksum_ok(raw, _checksum_byte(raw) ^ 0xFF) is False


def test_simulated_ads1263_produces_non_negative_voltage():
    sim = ads1263.SimulatedADS1263(base_v=1.5, amplitude_v=0.5, noise_v=0.01)
    for _ in range(20):
        assert sim.read_voltage(0) >= 0.0


def test_simulated_ads1263_close_is_a_no_op():
    ads1263.SimulatedADS1263().close()
