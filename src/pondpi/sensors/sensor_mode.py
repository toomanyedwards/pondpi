"""Controls the A02YYUW's RX pin, which selects the sensor's hardware
output mode -- driven low selects real-time ("raw") mode, driven high
(or left floating) selects the sensor's own internally-smoothed
("processed") mode. See README's "Sensor notes" section for the
research behind this; this module only concerns itself with driving
the pin.

`GpioModeController` is the real implementation (used unless
--simulate is passed); `NullModeController` is a no-op stand-in for
--simulate and for tests, which never touch real GPIO hardware.
"""

RAW = "raw"
PROCESSED = "processed"


class GpioModeController:
    """Drives the sensor's mode-select pin via gpiozero. The gpiozero
    import is deferred to __init__ rather than module level so that
    merely importing this module (e.g. during test collection) never
    requires a pin factory to be available."""

    def __init__(self, pin_bcm):
        from gpiozero import DigitalOutputDevice

        self._pin = DigitalOutputDevice(pin_bcm)

    def set_mode(self, mode):
        if mode == RAW:
            self._pin.off()
        else:
            self._pin.on()

    def close(self):
        self._pin.close()


class NullModeController:
    """Stand-in with no real pin to drive -- --simulate and tests."""

    def set_mode(self, mode):
        pass

    def close(self):
        pass
