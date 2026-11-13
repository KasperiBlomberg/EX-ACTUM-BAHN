"""The map: places, roads and the straight-line distance (the JS bot's globals P, ROAD and hav)."""
from __future__ import annotations

import math

P: dict = {}      # place id -> {id, name, lat, lon}
ROAD: dict = {}   # (from, to) -> {from, to, km, minutes}


def load(api) -> None:
    m = api.map()
    P.clear()
    P.update({p["id"]: p for p in m["places"]})
    ROAD.clear()
    for r in m["roads"]:
        ROAD[(r["from"], r["to"])] = r


def road(a: str, b: str) -> dict:
    return ROAD[(a, b)]


def hav(a: dict, b: dict) -> float:
    """Straight-line km between two places."""
    R, t = 6371, math.pi / 180
    dl, dn = (b["lat"] - a["lat"]) * t, (b["lon"] - a["lon"]) * t
    x = math.sin(dl / 2) ** 2 + math.cos(a["lat"] * t) * math.cos(b["lat"] * t) * math.sin(dn / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))
