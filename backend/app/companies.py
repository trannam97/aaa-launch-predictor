"""Normalizing company names out of Steam's publisher/developer strings.

Steam lists whoever holds the rights in a given territory or on a given
platform, so one company arrives under many spellings: "SEGA" and
"SEGA (Japan)", "Activision" and "Activision (Excluding Japan and Asia)",
"Warner Bros. Games" and "Warner Bros. Interactive Entertainment". Clustering
those as separate companies would split a single publisher's catalog across
several rows and understate every one of them.
"""

from __future__ import annotations

import re

# Territory and platform qualifiers Steam appends to a rights-holder.
_QUALIFIER = re.compile(r"\s*\((?:[^)]*)\)\s*$")

# Corporate suffixes that carry no signal.
_SUFFIX = re.compile(
    r",?\s*(?:inc\.?|incorporated|co\.,?\s*ltd\.?|ltd\.?|llc|l\.l\.c\.|gmbh|"
    r"s\.a\.?|s\.r\.o\.?|ab|oyj?|a/s|pty|plc)\.?$",
    re.IGNORECASE,
)

# Same company, renamed or restyled over the corpus's time span.
ALIASES = {
    "warner bros. interactive entertainment": "Warner Bros. Games",
    "wb games": "Warner Bros. Games",
    "ubisoft entertainment": "Ubisoft",
    "bandai namco entertainment": "Bandai Namco Entertainment",
    "koei tecmo games": "KOEI TECMO GAMES",
    "square enix": "Square Enix",
}

# Porting and localization houses. They appear in `publishers` because they
# hold the rights to a Mac or Linux build, but they neither fund development
# nor decide a title's budget tier — treating them as publishers would invent
# a company whose "catalog" is other studios' ports.
PORTING_HOUSES = {
    "feral interactive",
    "aspyr",
    "aspyr media",
    "virtual programming",
    "nixxes software",
}


def normalize(name: str) -> str | None:
    """Canonical display name for a company, or None if it should be dropped."""
    cleaned = name.strip()
    if not cleaned:
        return None

    # Strip qualifiers repeatedly: "Feral Interactive (Linux/Mac)" and the
    # occasional doubled "(Japan) (Mac)".
    while True:
        stripped = _QUALIFIER.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    if not cleaned:
        return None

    cleaned = _SUFFIX.sub("", cleaned).strip().rstrip(",").strip()
    if not cleaned:
        return None

    key = cleaned.lower()
    if key in PORTING_HOUSES:
        return None
    return ALIASES.get(key, cleaned)


def normalize_all(raw: str | None) -> list[str]:
    """Normalize a newline-separated publisher/developer field."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in raw.split("\n"):
        name = normalize(part)
        if name is not None:
            seen.setdefault(name, None)
    return list(seen)
