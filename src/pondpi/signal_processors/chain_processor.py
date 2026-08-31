from pondpi.signal_processors.base import LevelSignalProcessor


class ChainSignalProcessor(LevelSignalProcessor):
    """Runs a value through a sequence of other signal processors in
    order, feeding each stage's output as the next stage's input.

    `steps` is a list of (label, LevelSignalProcessor instance) pairs,
    resolved by signal_processor_config.py from the YAML config. Each
    step always gets its own independent instance -- referencing another
    processor by name (`ref:`) reuses its type/params to build a fresh
    instance, never the literal same object, so state is never shared
    across processors. See config/processors.yaml for examples.
    """

    def __init__(self, steps):
        self._steps = steps

    def add(self, raw_value):
        value = raw_value
        for _, processor in self._steps:
            value = processor.add(value)
        return value

    def extra_state(self):
        return {"steps": [{"processor": label, **processor.extra_state()} for label, processor in self._steps]}
