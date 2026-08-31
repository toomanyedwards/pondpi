class LevelProcessor:
    """Base class for a level signal processor.

    Subclasses take raw sensor readings (mm) one at a time via `add()` and
    return this processor's current output (mm). `extra_state()` surfaces
    any processor-specific metadata (window sizes, sample counts, ...) for
    the /level response.
    """

    def add(self, raw_mm):
        raise NotImplementedError

    def extra_state(self):
        return {}
