"""Player-name normalization so Yahoo, Sleeper, and nflverse names line up."""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize(name: str) -> str:
    """'J.K. Dobbins' -> 'jk dobbins'; 'Mike Washington Jr.' -> 'mike washington'."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[.'’]", "", name.lower())
    parts = [p for p in re.split(r"[\s\-]+", name) if p and p not in _SUFFIXES]
    return " ".join(parts)
