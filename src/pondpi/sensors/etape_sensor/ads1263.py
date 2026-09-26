"""Minimal ADS1263 register-level protocol driver for the Waveshare
High-Precision AD HAT -- single-ended channel voltage reads only.
Ported from Waveshare's own demo (ADS1263.py/config.py/main.py,
https://github.com/waveshare/High-Pricision_AD_HAT), keeping only the
single-ended ADC1 read path EtapeSensor actually needs -- no ADC2,
differential channels, or RTD/DAC test modes.

`spi`, `reset_pin`, `cs_pin`, `drdy_pin` are injected (duck-typed, not
constructed here) so this module stays fully unit-testable without real
hardware -- `spi` needs `.max_speed_hz`, `.mode`, `.writebytes(list)`,
`.readbytes(n)`; each pin needs `.on()`/`.off()` (drive high/low; `cs_pin`
and `reset_pin` are outputs) or `.value` (`drdy_pin`, an input -- see
`_wait_drdy()`'s docstring for what `.value` means here specifically).
Real construction (spidev.SpiDev + gpiozero DigitalOutputDevice/
DigitalInputDevice against this HAT's fixed RST/CS/DRDY pins) happens in
this package's own `create()`, not here -- same split as
`a02yyuw_sensor/read_sensor.py` (pure protocol, hardware injected) vs.
`sensor_mode.py`/`sensor_power.py` (own the real GPIO construction).
"""

import math
import random
import time

# BCM GPIO pins wired on the Waveshare High-Precision AD HAT -- fixed by
# the HAT's own PCB, not configurable per-install. Confirmed against
# Waveshare's own wiki and demo code. CS is software-driven off GPIO22,
# not the Pi's hardware CE0/CE1 -- this HAT ties its own chip-select
# line there instead.
RST_PIN = 18
CS_PIN = 22
DRDY_PIN = 17

# The ADC's own AVDD/AVSS supply-rail voltage, used as its conversion
# reference in this HAT's default single-ended jumper mode (REFMUX below
# selects VDD/VSS as reference rather than the internal 2.5V one) --
# not a nominal 5.0V; this board's rail measured 5.08V, which is also
# what Waveshare's own demo script's REF constant used. This project's
# eTape bench calibration (see EtapeSensor) was fit against voltages
# this exact value produces -- changing it later would invalidate that
# calibration for any sensor calibrated against the old value.
DEFAULT_REFERENCE_VOLTAGE = 5.08

_REG_ID = 0
_REG_MODE0 = 3
_REG_MODE1 = 4
_REG_MODE2 = 5
_REG_INPMUX = 6
_REG_REFMUX = 15

_CMD_START1 = 0x08
_CMD_STOP1 = 0x0A
_CMD_RDATA1 = 0x12
_CMD_RREG = 0x20
_CMD_WREG = 0x40

_MODE0_DELAY_35US = 3  # ADS1263_DELAY_35us in the original demo
_MODE1_FIR_FILTER = 0x84  # digital filter select: FIR (lowest noise of the available options)
_MODE2_PGA_BYPASSED = 0x80
_REFMUX_VDD_VSS = 0x24  # AVDD/AVSS as reference (see DEFAULT_REFERENCE_VOLTAGE above)
_GAIN_1 = 0

# data rate (samples/sec) -> REG_MODE2 data-rate code. Only a subset of
# what the chip supports -- ported in full from the original demo's own
# table anyway since it costs nothing and documents the encoding, even
# though EtapeSensor only ever configures one of these.
_DATA_RATE_CODES = {
    2.5: 0x0,
    5: 0x1,
    10: 0x2,
    16.6: 0x3,
    20: 0x4,
    50: 0x5,
    60: 0x6,
    100: 0x7,
    400: 0x8,
    1200: 0x9,
    2400: 0xA,
    4800: 0xB,
    7200: 0xC,
    14400: 0xD,
    19200: 0xE,
    38400: 0xF,
}

DEFAULT_DATA_RATE_SPS = 100

_RESET_PULSE_S = 0.2  # matches the original demo's own reset timing exactly
_WAIT_DRDY_TIMEOUT_S = 2.0  # comfortably above one conversion period at any supported data rate


class ADS1263:
    """One ADS1263 chip, configured for single-ended reads on AIN0-AIN9
    against the board's own grounded COM reference -- the mode this HAT
    ships jumpered for by default (see README's "Sensor notes" section).
    Owns the whole chip: RST/CS/DRDY are fixed HAT pins, so only one
    instance should exist per physical HAT.
    """

    def __init__(
        self,
        spi,
        reset_pin,
        cs_pin,
        drdy_pin,
        reference_voltage=DEFAULT_REFERENCE_VOLTAGE,
        data_rate_sps=DEFAULT_DATA_RATE_SPS,
    ):
        if data_rate_sps not in _DATA_RATE_CODES:
            raise ValueError(f"ads1263: unsupported data_rate_sps {data_rate_sps} (expected one of {sorted(_DATA_RATE_CODES)})")

        self._spi = spi
        self._reset_pin = reset_pin
        self._cs_pin = cs_pin
        self._drdy_pin = drdy_pin
        self._reference_voltage = reference_voltage

        self._spi.max_speed_hz = 2_000_000
        self._spi.mode = 0b01

        self._hardware_reset()

        chip_id = self._read_reg(_REG_ID) >> 5
        if chip_id != 0x01:
            raise RuntimeError(f"ads1263: unexpected chip ID 0x{chip_id:02x} (expected 0x01) -- check wiring/power")

        self._write_cmd(_CMD_STOP1)
        mode2 = _MODE2_PGA_BYPASSED | (_GAIN_1 << 4) | _DATA_RATE_CODES[data_rate_sps]
        self._write_reg_verified(_REG_MODE2, mode2)
        self._write_reg_verified(_REG_REFMUX, _REFMUX_VDD_VSS)
        self._write_reg_verified(_REG_MODE0, _MODE0_DELAY_35US)
        self._write_reg_verified(_REG_MODE1, _MODE1_FIR_FILTER)
        self._write_cmd(_CMD_START1)

    def read_voltage(self, channel):
        """One single-ended voltage reading (volts) on AIN<channel>
        (0-9) against COM. Blocks until the chip's DRDY line signals a
        fresh conversion is ready (bounded by `_WAIT_DRDY_TIMEOUT_S`) --
        callers that must never block (see `Sensor.read()`'s contract)
        should call this from their own background thread, same as
        `A02YYUWSensor` does for its UART reads."""
        if not 0 <= channel <= 9:
            raise ValueError(f"ads1263: channel must be 0-9, got {channel}")

        self._select_channel(channel)
        self._wait_drdy()
        raw = self._read_adc1_data()
        return self._raw_to_voltage(raw)

    def close(self):
        self._spi.close()
        self._reset_pin.close()
        self._cs_pin.close()
        self._drdy_pin.close()

    def _hardware_reset(self):
        self._reset_pin.on()
        time.sleep(_RESET_PULSE_S)
        self._reset_pin.off()
        time.sleep(_RESET_PULSE_S)
        self._reset_pin.on()
        time.sleep(_RESET_PULSE_S)

    def _select(self):
        self._cs_pin.off()  # active low

    def _deselect(self):
        self._cs_pin.on()

    def _write_cmd(self, cmd):
        self._select()
        self._spi.writebytes([cmd])
        self._deselect()

    def _write_reg(self, reg, data):
        self._select()
        self._spi.writebytes([_CMD_WREG | reg, 0x00, data])
        self._deselect()

    def _read_reg(self, reg):
        self._select()
        self._spi.writebytes([_CMD_RREG | reg, 0x00])
        data = self._spi.readbytes(1)
        self._deselect()
        return data[0]

    def _write_reg_verified(self, reg, data):
        """Only used at init/config time -- see `read_voltage()`'s own
        channel select, which skips this per-sample read-back to keep
        the polling loop light; the data read's own checksum (see
        `_read_adc1_data()`) is the correctness check that matters on
        every sample."""
        self._write_reg(reg, data)
        actual = self._read_reg(reg)
        if actual != data:
            raise RuntimeError(f"ads1263: register 0x{reg:02x} write failed (wrote 0x{data:02x}, read back 0x{actual:02x})")

    def _select_channel(self, channel):
        # Single-ended: measures AIN<channel> against AINCOM (tied to
        # this HAT's own ground in its default single-ended jumper mode
        # -- see this module's docstring). INPMUX's low nibble 0x0A
        # selects AINCOM as the negative input; only the high nibble
        # (positive input channel) varies per read.
        inpmux = (channel << 4) | 0x0A
        self._write_reg(_REG_INPMUX, inpmux)

    def _wait_drdy(self):
        # `drdy_pin` is a gpiozero DigitalInputDevice constructed with
        # pull_up=True (see create()) -- under gpiozero's own pull-up
        # convention, `.value` reads 1 exactly when the pin is driven
        # LOW externally, not when it's electrically high. DRDY itself
        # idles high and pulses low when a fresh conversion is ready
        # (per the ADS1263 datasheet), so waiting for `.value` to go
        # truthy is correct here even though it reads backwards from
        # the raw electrical level -- don't "fix" this to check for 0.
        deadline = time.monotonic() + _WAIT_DRDY_TIMEOUT_S
        while not self._drdy_pin.value:
            if time.monotonic() > deadline:
                raise TimeoutError("ads1263: timed out waiting for DRDY")

    def _read_adc1_data(self):
        self._select()
        while True:
            self._spi.writebytes([_CMD_RDATA1])
            status = self._spi.readbytes(1)[0]
            if status & 0x40:
                break
        buf = self._spi.readbytes(5)
        self._deselect()

        raw = ((buf[0] << 24) | (buf[1] << 16) | (buf[2] << 8) | buf[3]) & 0xFFFFFFFF
        checksum_byte = buf[4]
        if not _checksum_ok(raw, checksum_byte):
            raise OSError("ads1263: checksum mismatch reading ADC1 data")
        return raw

    def _raw_to_voltage(self, raw):
        # `raw` is a 32-bit two's-complement ADC code. Converted to
        # volts exactly as Waveshare's own demo script (main.py) does,
        # denominator asymmetry (0x7fffffff positive, 0x80000000
        # negative) included -- this project's eTape bench calibration
        # was fit against that script's own printed voltages, so this
        # must stay bit-for-bit identical to it or the calibration
        # silently goes wrong.
        if raw & 0x80000000:
            signed = raw - 0x100000000
            return signed * self._reference_voltage / 0x80000000
        return raw * self._reference_voltage / 0x7FFFFFFF


def _checksum_ok(value, checksum_byte):
    total = 0
    v = value
    while v:
        total += v & 0xFF
        v >>= 8
    total += 0x9B
    return ((total & 0xFF) ^ checksum_byte) == 0


class SimulatedADS1263:
    """Fake ADC that produces a synthetic, slowly-wandering voltage, for
    local development/`--simulate` without a real Waveshare ADS1263 HAT.
    Mirrors `read_sensor.SimulatedSerial`'s approach (sine wave + noise),
    just at the voltage level, upstream of EtapeSensor's own
    calibration."""

    def __init__(self, base_v=1.5, amplitude_v=0.5, period_s=30, noise_v=0.01):
        self._base_v = base_v
        self._amplitude_v = amplitude_v
        self._period_s = period_s
        self._noise_v = noise_v
        self._start = time.monotonic()

    def read_voltage(self, channel):
        elapsed = time.monotonic() - self._start
        wave = math.sin(2 * math.pi * elapsed / self._period_s)
        noise = random.uniform(-self._noise_v, self._noise_v)
        return max(0.0, self._base_v + self._amplitude_v * wave + noise)

    def close(self):
        pass
