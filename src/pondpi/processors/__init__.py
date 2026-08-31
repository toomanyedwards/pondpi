import importlib
import inspect
import pkgutil
import sys

from pondpi.processors.base import LevelProcessor

__all__ = ["LevelProcessor", "discover_processor_types"]


def discover_processor_types(package=None):
    """Returns {type_name: ProcessorClass} for every module in this
    package except base.py.

    type_name is the module's filename (e.g. processors/median.py ->
    type "median"). Each module must define exactly one LevelProcessor
    subclass -- zero or multiple is an error, not silently ignored. This
    is what lets a new processor be added by just dropping a new file in
    this folder, with nothing else to register.
    """
    if package is None:
        package = sys.modules[__name__]

    registry = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name == "base":
            continue

        module = importlib.import_module(f"{package.__name__}.{name}")
        found = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, LevelProcessor) and obj is not LevelProcessor and obj.__module__ == module.__name__
        ]

        if len(found) != 1:
            raise ValueError(f"{module.__name__}: expected exactly one LevelProcessor subclass, found {len(found)}")

        registry[name] = found[0]

    return registry
