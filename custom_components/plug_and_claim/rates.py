"""Rate history: which rate applies to which month.

Stored in the claim's options as
    "rate_history": [{"from": "2000-01", "rate": 0.407}, {"from": "2027-01", "rate": 0.42}]
sorted by "from". A month uses the latest entry whose "from" is on or before
it. Without a history (the rate was never changed) the plain "rate" option
applies to every month.
"""

from collections.abc import Mapping
from typing import Any

from .const import CONF_RATE, CONF_RATE_HISTORY, DEFAULT_RATE

# "From the beginning": used for the rate that applied before the first change.
EARLIEST_MONTH = "2000-01"


def rate_for_month(options: Mapping[str, Any], month: str) -> float:
    history = options.get(CONF_RATE_HISTORY) or []
    applicable = [entry for entry in history if entry["from"] <= month]
    if applicable:
        return max(applicable, key=lambda entry: entry["from"])["rate"]
    return options.get(CONF_RATE, DEFAULT_RATE)


def add_rate(
    history: list[dict[str, Any]] | None, previous_rate: float, new_rate: float, from_month: str
) -> list[dict[str, Any]]:
    """Return a new history with new_rate applying from from_month.

    The first change also records the previous rate as applying from the
    beginning, so earlier months keep it. Entering a rate for a month that
    already has one replaces it (to correct a mistake).
    """
    entries = list(history or [{"from": EARLIEST_MONTH, "rate": previous_rate}])
    entries = [entry for entry in entries if entry["from"] != from_month]
    entries.append({"from": from_month, "rate": new_rate})
    return sorted(entries, key=lambda entry: entry["from"])


def describe(history: list[dict[str, Any]] | None, current_rate: float) -> str:
    """One line per rate, for the settings form."""
    if not history:
        return f"${current_rate:.6f} for all months"
    lines = []
    for entry in history:
        start = "the beginning" if entry["from"] == EARLIEST_MONTH else entry["from"]
        lines.append(f"${entry['rate']:.6f} from {start}")
    return "; ".join(lines)
