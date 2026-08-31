import yaml

from level_strategies import STRATEGY_TYPES


def load_strategies(path):
    """Loads named LevelStrategy instances from a YAML config file.

    Returns (dict[name -> LevelStrategy instance], primary_name). Exactly
    one entry must be marked `primary: true` — its output backfills the
    legacy top-level rolling_avg_distance_cm field in /level.
    """
    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("strategies")
    if not entries:
        raise ValueError(f"{path}: 'strategies' must be a non-empty list")

    strategies = {}
    primary_name = None

    for entry in entries:
        name = entry.get("name")
        strategy_type = entry.get("type")

        if not name:
            raise ValueError(f"{path}: strategy entry is missing 'name': {entry}")
        if name in strategies:
            raise ValueError(f"{path}: duplicate strategy name '{name}'")
        if strategy_type not in STRATEGY_TYPES:
            raise ValueError(
                f"{path}: strategy '{name}' has unknown type '{strategy_type}' "
                f"(expected one of {sorted(STRATEGY_TYPES)})"
            )

        params = entry.get("params") or {}
        try:
            strategies[name] = STRATEGY_TYPES[strategy_type](**params)
        except TypeError as e:
            raise ValueError(f"{path}: strategy '{name}' has invalid params for type '{strategy_type}': {e}") from e

        if entry.get("primary", False):
            if primary_name is not None:
                raise ValueError(f"{path}: multiple strategies marked primary ('{primary_name}' and '{name}')")
            primary_name = name

    if primary_name is None:
        raise ValueError(f"{path}: exactly one strategy must be marked 'primary: true'")

    return strategies, primary_name
