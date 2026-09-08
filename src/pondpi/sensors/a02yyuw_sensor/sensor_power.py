"""Controls the A02YYUW's power supply pin, allowing the sensor to be
power-cycled in software to force a hardware reset (e.g. if it appears
wedged/stuck and a serial buffer flush alone hasn't helped). This pin is
also driven high at boot via `/boot/firmware/config.txt`'s
`gpio=<pin>=op,dh` directive, so the sensor already has power before this
service starts -- `GpioPowerController` just takes over from there for
runtime-triggered resets.

`GpioPowerController` is the real implementation (used unless --simulate
is passed); `NullPowerController` is a no-op stand-in for --simulate and
for tests, which never touch real GPIO hardware.
"""

import time

# How long to hold the sensor's power pin low during a reset -- long
# enough for it to fully discharge and power-on-reset cleanly once power
# is restored. Was 1.0 originally; bumped to 5.0 after a real incident
# (2026-09-07/08) where the sensor got wedged reporting a stuck,
# zero-jitter reading and a 1s sensor-level reset didn't clear it
# (an HA automation retried this reset hourly with no effect), but a
# full Pi power cycle -- which leaves the sensor unpowered far longer
# than 1s -- did. 5s gives real hold-up capacitance more room to
# discharge while staying under the 10s default timeout HA's
# `rest_command` integration uses (see README's "POST /reset" section
# for the full per-request latency this adds).
RESET_OFF_DURATION_S = 5.0


class GpioPowerController:
    """Drives the sensor's power pin via gpiozero. The gpiozero import is
    deferred to __init__ rather than module level so that merely
    importing this module (e.g. during test collection) never requires a
    pin factory to be available."""

    def __init__(self, pin_bcm):
        from gpiozero import DigitalOutputDevice

        # initial_value=True: the pin is already driven high at boot (see
        # module docstring) -- constructing this without it would default
        # to driving the pin low immediately, cutting sensor power the
        # moment this service starts.
        self._pin = DigitalOutputDevice(pin_bcm, initial_value=True)

    def reset(self, off_duration_s=RESET_OFF_DURATION_S):
        self._pin.off()
        time.sleep(off_duration_s)
        self._pin.on()

    def close(self):
        self._pin.close()


class NullPowerController:
    """Stand-in with no real pin to drive -- --simulate and tests."""

    def reset(self, off_duration_s=RESET_OFF_DURATION_S):
        pass

    def close(self):
        pass
