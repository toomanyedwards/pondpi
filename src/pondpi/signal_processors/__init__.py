import importlib
import inspect
import pkgutil
import sys

from pondpi.signal_processors.base import LevelSignalProcessor

__all__ = ["LevelSignalProcessor", "discover_signal_processor_types"]

_TYPE_SUFFIX = "_processor"


def discover_signal_processor_types(package=None):
    """Returns {type_name: SignalProcessorClass} for every module in this
    package whose filename ends in "_processor".

    type_name is the module's filename with that suffix stripped (e.g.
    signal_processors/rolling_median_processor.py -> type "rolling_median").
    Each such module must define exactly one LevelSignalProcessor
    subclass -- zero or multiple is an error, not silently ignored. Files
    that don't end in "_processor" (base.py, the utils/ subpackage, or
    any future non-processor helper module) are ignored automatically,
    with no hardcoded skip-list to maintain.
    This is what lets a new signal processor be added by just dropping a
    new <name>_processor.py file in this folder, with nothing else to
    register.
    """
    if package is None:
        package = sys.modules[__name__]

    registry = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if not name.endswith(_TYPE_SUFFIX):
            continue

        module = importlib.import_module(f"{package.__name__}.{name}")
        found = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, LevelSignalProcessor)
            and obj is not LevelSignalProcessor
            and obj.__module__ == module.__name__
        ]

        if len(found) != 1:
            raise ValueError(
                f"{module.__name__}: expected exactly one LevelSignalProcessor subclass, found {len(found)}"
            )

        type_name = name.removesuffix(_TYPE_SUFFIX)
        registry[type_name] = found[0]

    return registry
