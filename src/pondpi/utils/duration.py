def format_duration(seconds):
    """Formats a duration in seconds as a human-readable "1d 2h 3m 4s" string.

    Once a larger unit is non-zero, all smaller units down to seconds are
    included (even if zero), so e.g. exactly one day is "1d 0h 0m 0s"
    rather than just "1d".
    """
    total_seconds = int(seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")

    return " ".join(parts)
