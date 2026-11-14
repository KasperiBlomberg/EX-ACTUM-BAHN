"""Return-leg matcher: two single-car orders A->B and B->A that one driver can do as a round trip.

Pure functions over a small ``job`` dict, so the same checks serve an order we hold and a request
we are about to quote.  The policy decides what is an ordinary car (no private address, note,
licence, plates, charging or weather constraint); this module checks only the geometry, the
12-hour driving rule and whether the two cars fit one working day with enough lead.
"""
from __future__ import annotations

from typing import Optional

from .geo import P, hav, road

DAY = 1440
STEP = 15              # minutes between the start times tried
DAY_START = 6 * 60     # a round trip is planned inside one day, 06:00-22:00
DAY_END = 22 * 60
SITE_MARGIN = 30       # real drives vary around the map minutes: do not plan to the closing minute


def site_open(hours: Optional[dict], minute: int, margin: int = SITE_MARGIN) -> bool:
    """Whether a site's stated hours show it open at ``minute`` with ``margin`` to spare."""
    if not hours:
        return True
    day, tod = divmod(minute, DAY)
    weekday = day % 7
    if weekday in hours["weekdays"]:
        return hours["opens"] <= tod <= hours["closes"] - margin
    saturday = hours.get("saturday")
    if weekday == 5 and saturday:
        return saturday["opens"] <= tod <= saturday["closes"] - margin
    return False


def job(job_id: str, leg: dict, pickup_from: int, last_pickup: int, ref: float) -> dict:
    """One car to move.  ``last`` is the last pickup minute that still delivers it on time."""
    return {"id": job_id, "pickup": leg["pickup"]["placeId"], "delivery": leg["delivery"]["placeId"],
            "pickupHours": leg["pickup"].get("hours"), "deliveryHours": leg["delivery"].get("hours"),
            "from": pickup_from, "to": leg["pickup"]["window"]["to"], "last": last_pickup, "ref": ref}


def is_return_leg(a: dict, b: dict, max_km: float) -> bool:
    """Each car's delivery is close to the other's pickup, in a straight line."""
    return (hav(P[a["delivery"]], P[b["pickup"]]) <= max_km
            and hav(P[b["delivery"]], P[a["pickup"]]) <= max_km)


def _one_way(first: dict, second: dict, now: int, min_lead: int, max_wait: int, max_drive: int):
    r1 = road(first["pickup"], first["delivery"])["minutes"]
    link = road(first["delivery"], second["pickup"])["minutes"]
    r2 = road(second["pickup"], second["delivery"])["minutes"]
    drive = r1 + link + r2
    if drive > max_drive:
        return None, "driving_limit"
    lo = max(now, first["from"])
    hi = min(first["to"], first["last"], second["to"] - r1 - link, second["last"] - r1 - link)
    starts = []
    for start in range(lo, hi + 1, STEP):
        arrive = start + r1 + link
        second_pickup = max(arrive, second["from"])
        end = second_pickup + r2
        if (second_pickup - arrive <= max_wait and second_pickup <= min(second["to"], second["last"])
                and start // DAY == end // DAY and start % DAY >= DAY_START and end % DAY <= DAY_END
                and site_open(first["pickupHours"], start) and site_open(first["deliveryHours"], start + r1)
                and site_open(second["pickupHours"], second_pickup) and site_open(second["deliveryHours"], end)):
            starts.append(start)
    if not starts:
        return None, "windows"
    if starts[-1] - now < min_lead:
        return None, "near_deadline"
    return {"first": first["id"], "second": second["id"], "driveMinutes": drive,
            "earliest": starts[0], "latest": starts[-1]}, None


REASON_ORDER = ["near_deadline", "windows", "driving_limit", "distance"]


def plan_pair(a: dict, b: dict, now: int, max_km: float, min_lead: int, max_wait: int,
              max_drive: int = 12 * 60):
    """(plan, None) for the round trip with the most lead, or (None, reason).

    A plan needs: both ends within ``max_km``; at most ``max_drive`` road minutes from the first
    pickup to the last delivery (the 12-hour rule for a gig with several cars); a start at which
    every stop is inside its window and its site's stated hours, with at most ``max_wait`` minutes
    of waiting for the second car; and a latest such start at least ``min_lead`` minutes away.
    Reasons: ``distance``, ``driving_limit``, ``windows``, ``near_deadline``.
    """
    if not is_return_leg(a, b, max_km):
        return None, "distance"
    best, reasons = None, []
    for first, second in ((a, b), (b, a)):
        plan, reason = _one_way(first, second, now, min_lead, max_wait, max_drive)
        if plan and (best is None or plan["latest"] > best["latest"]):
            best = plan
        elif reason:
            reasons.append(reason)
    if best:
        return best, None
    return None, min(reasons, key=REASON_ORDER.index)
