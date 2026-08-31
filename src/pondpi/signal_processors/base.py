class LevelSignalProcessor:
    """Base class for a level signal processor.

    Subclasses take raw signal readings one at a time via `add()` and
    return this signal processor's current output. `extra_state()`
    surfaces any processor-specific metadata (window sizes, sample
    counts, ...) for the /level response. Signal processors are
    unit-agnostic — they don't know or care whether the values they're
    passed are mm, cm, or anything else; unit conversion happens at the
    HTTP layer in server.py.
    """

    def add(self, raw_value):
        raise NotImplementedError

    def extra_state(self):
        return {}
