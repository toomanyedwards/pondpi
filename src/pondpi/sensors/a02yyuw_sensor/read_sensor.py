import math
import random
import time

# read_frame()'s outcome values -- see its own docstring for what each
# means. Plain string constants, same style as sensor_mode.py's
# RAW/PROCESSED, so a caller (A02YYUWSensor, to tally frame_stats()) can
# compare against them without importing anything heavier.
OK = "ok"
NO_DATA = "no_data"
MISALIGNED = "misaligned"
CHECKSUM_FAILED = "checksum_failed"
INCOMPLETE = "incomplete"


def calculate_checksum(data_h, data_l):
    return (0xFF + data_h + data_l) & 0xFF


def parse_distance_mm(data_h, data_l):
    return (data_h << 8) + data_l


def is_valid_reading(distance_mm):
    # Filter out invalid dead-zone readings (usually 0 or out of bounds)
    return distance_mm > 30


def read_frame(ser):
    """Try to read one A02YYUW frame. Returns `(distance_mm, outcome)`
    -- `distance_mm` is None unless `outcome` is `OK`. `outcome` is one
    of this module's own constants (above):
    - `OK`: a valid, checksummed frame was read.
    - `NO_DATA`: fewer than 4 bytes were waiting -- nothing to read yet,
      the normal case when polling faster than the sensor's own frame
      rate.
    - `MISALIGNED`: bytes were waiting but the first one wasn't the
      0xFF header -- this call consumed exactly one byte hunting for
      resync (see A02YYUWSensor's own docstring for why); a real
      diagnostic signal if this keeps happening rather than being rare.
    - `CHECKSUM_FAILED`: a 0xFF header was found but the following 3
      bytes didn't check out -- the input buffer has already been
      flushed to force a fresh resync.
    - `INCOMPLETE`: a 0xFF header was found but fewer than 3 more bytes
      were available right away (e.g. read mid-transmission) -- rare,
      treated like `NO_DATA` by the caller but tracked separately since
      it means something *was* on the wire, just not fully yet.
    """
    # Check if we have enough bytes in the buffer to make a full 4-byte frame,
    # and read a single byte looking for the 0xFF header
    if ser.in_waiting < 4:
        return None, NO_DATA

    if ser.read(1) != b'\xff':
        return None, MISALIGNED

    # Grab the remaining 3 bytes of this frame immediately
    data = ser.read(3)
    if len(data) != 3:
        return None, INCOMPLETE

    data_h, data_l, checksum = data[0], data[1], data[2]

    if calculate_checksum(data_h, data_l) == checksum:
        return parse_distance_mm(data_h, data_l), OK

    # If checksum fails, we likely misaligned; flush buffer to reset
    ser.reset_input_buffer()
    return None, CHECKSUM_FAILED


class SimulatedSerial:
    """Fake serial source that streams synthetic A02YYUW frames, for local
    development without real sensor hardware. The distance wanders slowly
    around `base_mm` on a sine wave, plus a bit of random noise."""

    def __init__(self, base_mm=800, amplitude_mm=200, period_s=30, noise_mm=5):
        self._buf = b""
        self._base_mm = base_mm
        self._amplitude_mm = amplitude_mm
        self._period_s = period_s
        self._noise_mm = noise_mm
        self._start = time.monotonic()

    @property
    def in_waiting(self):
        if not self._buf:
            self._buf = self._next_frame()
        return len(self._buf)

    def read(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def reset_input_buffer(self):
        self._buf = b""

    def close(self):
        pass

    def _next_frame(self):
        elapsed = time.monotonic() - self._start
        wave = math.sin(2 * math.pi * elapsed / self._period_s)
        noise = random.uniform(-self._noise_mm, self._noise_mm)
        distance_mm = int(self._base_mm + self._amplitude_mm * wave + noise)
        distance_mm = max(0, min(distance_mm, 4500))  # sensor's rated range

        data_h, data_l = (distance_mm >> 8) & 0xFF, distance_mm & 0xFF
        checksum = calculate_checksum(data_h, data_l)
        return bytes([0xFF, data_h, data_l, checksum])
