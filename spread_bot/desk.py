"""The desk (port of src/desk.js): the starter's bookkeeping and event answers, with the decisions
behind a policy. A policy is any object with some of these methods; `None` plays the kit starter:

  policy.quote(desk, request)             -> {kind: cents} or None (None = the starter's list price)
  policy.fees(desk, order, cars, urgent)  -> [{at, feeCents}]     (None = the starter's fees)
  policy.on_event(desk, event, replay)    -> learning hook, called before the desk acts
  policy.on_unbooked(desk, event)         -> after a drop or a no-show
  policy.post_extras(desk, order, legs, retry) -> {manual, plates, pickupWindow, deliveryWindow}
  policy.init(desk), policy.tick(desk), policy.next_wake(desk), policy.wake_on
"""
from __future__ import annotations

import math
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .geo import road

HOUR, DAY = 60, 1440
INF = float("inf")
WAKE_ON = ["price_request", "order_won", "order_changed", "order_cancelled", "visit_failed", "gig_expired",
           "deadline_close", "gig_dropped", "no_show"]
NO_RETRY = {"pickupFrom": 0, "manualLicence": False, "plates": False}


def planned_drive_minutes(order: dict, legs: list) -> int:
    """Drive time for a combined gig, including transfers between cars."""
    total = 0
    previous_delivery = None
    for index in legs:
        leg = order["legs"][index]
        pickup, delivery = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
        if previous_delivery is not None:
            total += road(previous_delivery, pickup)["minutes"]
        total += road(pickup, delivery)["minutes"]
        previous_delivery = delivery
    return total


class Desk:
    def __init__(self, api, world_id: str, level: int, now: int, policy: Any):
        self.api, self.world_id, self.level, self.now, self.policy = api, world_id, level, now, policy
        self.outbox: list = []
        self.requests: dict = {}    # id -> PriceRequest
        self.orders: dict = {}      # id -> Order (won, not cancelled)
        self.leg_gigs: dict = {}    # order id -> {leg: gig id}
        self.booked: set = set()
        self.posts: dict = {}       # gig id -> post action
        self.urgent: set = set()
        self.gig_info: dict = {}    # gig id -> {orderId, postedAt, firstFee, cars}
        self.reposts: dict = {}     # order id -> minutes of reposts after expiry
        self.log: list = []

    def hook(self, name: str) -> Optional[Callable]:
        return getattr(self.policy, name, None)

    # ------------------------------------------------------------ rejoin (final / restart)
    def rejoin(self, snapshot: dict, history: list, replay_policy: bool = True) -> None:
        posted: dict = {}
        self.booked = {g["id"] for g in snapshot["gigs"] if g["status"] in ("booked", "working", "done")}
        for gig in snapshot["gigs"]:
            posted.setdefault(gig["stops"][0]["orderId"], []).append(gig)
            if gig["status"] in ("open", "booked", "working"):
                self.carry({"type": "post", "stops": gig["stops"], "audience": gig["audience"], "schedule": gig["schedule"]}, gig["id"])
        for e in history:
            if e["kind"] == "price_request":
                self.requests[e["request"]["id"]] = e["request"]
        for e in history:
            if e["kind"] == "deadline_close":
                self.urgent.add(e["orderId"])
        for o in snapshot["orders"]:
            if o["id"] in self.requests and o["status"] != "cancelled":
                self.orders[o["id"]] = o
        on_event = self.hook("on_event")
        if replay_policy and on_event:
            for e in history:
                on_event(self, e, True)
        for order in list(self.orders.values()):
            if order["status"] == "waiting" and not self.gigs_of(order["id"]):
                self.post(order, undelivered(order))
        for e in history:
            order = self.orders.get(e.get("orderId"))
            if not order:
                continue
            if e["kind"] == "visit_failed" and order["deliveredAt"][e["leg"]] is None:
                later = [g for g in posted.get(order["id"], []) if g["postedAt"] >= e["at"] and g["id"] != e["gigId"]]
                if not any(s["leg"] == e["leg"] for g in later for s in g["stops"]):
                    self.retry(order["id"], e["leg"], e["reason"])
        for r in snapshot["openRequests"]:
            self.quote(r)

    # ------------------------------------------------------------ events
    def handle(self, e: dict) -> None:
        on_event = self.hook("on_event")
        if on_event:
            on_event(self, e, False)
        kind = e["kind"]
        if kind == "price_request":
            self.quote(e["request"])
        elif kind == "order_won":
            self.win(e["order"])
        elif kind == "order_changed":
            self.change(e["order"])
        elif kind == "order_cancelled":
            self.orders.pop(e["orderId"], None)
            self.leg_gigs.pop(e["orderId"], None)
        elif kind == "visit_failed":
            self.retry(e["orderId"], e["leg"], e["reason"])
        elif kind == "gig_expired":
            self.repost(e["gigId"])
        elif kind == "deadline_close":
            self.raise_fees(e["orderId"])
        elif kind == "gig_booked":
            self.booked.add(e["gigId"])
        elif kind in ("gig_dropped", "no_show"):
            self.booked.discard(e["gigId"])
            on_unbooked = self.hook("on_unbooked")
            if on_unbooked:
                on_unbooked(self, e)
        elif kind == "delivered":
            lg = self.leg_gigs.get(e["orderId"])
            if lg is not None:
                lg.pop(e["leg"], None)
            if e["orderId"] in self.orders:
                self.orders[e["orderId"]]["deliveredAt"][e["leg"]] = e["at"]

    def quote(self, request: dict) -> None:
        self.requests[request["id"]] = request
        quote = self.hook("quote")
        prices = quote(self, request) if quote else None
        if not prices:
            first = request["options"][0]
            prices = {first["kind"]: first["listPriceCents"]}
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def win(self, order: dict) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:
            self.orders[order["id"]] = order
            self.post(order, undelivered(order))

    def change(self, order: dict) -> None:
        if order["id"] in self.orders:
            self.orders[order["id"]] = order
            if self.withdraw(order["id"]):
                self.post(order, undelivered(order))

    def repost(self, gig_id: str) -> None:
        action = self.posts.get(gig_id)
        if not action or not any(self.leg_gigs.get(s["orderId"], {}).get(s["leg"]) == gig_id for s in action["stops"]):
            return
        for s in action["stops"]:
            lg = self.leg_gigs.get(s["orderId"])
            if lg is not None:
                lg.pop(s["leg"], None)
        order = self.orders.get(action["stops"][0]["orderId"])
        if not order:
            return
        self.reposts.setdefault(order["id"], []).append(self.now)
        if self.hook("post_extras"):   # recompute the stop windows from now (note hours roll to the next open day)
            legs = [s["leg"] for s in action["stops"] if s["kind"] == "pickup" and order["deliveredAt"][s["leg"]] is None]
            if legs:
                self.post(order, legs, {"pickupFrom": self.now, "manualLicence": action["audience"]["manualLicence"],
                                        "plates": any(s.get("withPlates") for s in action["stops"])})
            return
        stops = without_windows(action["stops"], INF)
        self.outbox.append({**action, "stops": stops, "schedule": self.fees_for(order, pickups(action))})

    def raise_fees(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if not order:
            return
        self.urgent.add(order_id)
        for action in self.outbox:
            if action["type"] == "post" and any(s["orderId"] == order_id for s in action["stops"]):
                action["schedule"] = self.fees_for(order, pickups(action))
        for gig_id in self.gigs_of(order_id):
            if gig_id not in self.booked:
                self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": self.fees_for(order, pickups(self.posts[gig_id]))})

    def fees_for(self, order: dict, cars: int) -> list:
        urgent = order["id"] in self.urgent
        fees = self.hook("fees")
        custom = fees(self, order, cars, urgent) if fees else None
        if custom:
            return custom
        fee = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        return steep(self.now, fee, self.level) if urgent else rising(self.now, fee, self.level)

    def retry(self, order_id: str, leg: int, reason: str) -> None:
        order = self.orders.get(order_id)
        if not order:
            return
        lg = self.leg_gigs.get(order_id)
        if lg is not None:
            lg.pop(leg, None)
        not_there = reason in ("not_ready", "closed")
        pickup_from = retry_from(order, self.requests[order_id], leg, self.now) if not_there else 0
        self.post(order, [leg], {"pickupFrom": pickup_from, "manualLicence": reason == "licence", "plates": reason == "no_plates"})

    def post(self, order: dict, legs: list, retry: Optional[dict] = None) -> None:
        retry = retry or NO_RETRY
        post_extras = self.hook("post_extras")
        extra = post_extras(self, order, legs, retry) if post_extras else {}
        audience = {"experienced": any(l["car"]["premium"] for l in order["legs"]),
                    "manualLicence": retry["manualLicence"] or bool(extra.get("manual"))}
        r2 = {**retry, "plates": retry["plates"] or bool(extra.get("plates"))}
        # a stop window that ends within 2 hours cannot be met by a new booking: drop it, so the order's own
        # window applies (and, once that has ended, the stop runs to the end of the world)
        stops = without_windows(stops_for(order, legs, self.requests[order["id"]].get("note"), r2, extra), self.now + 120)
        churn = len([t for t in self.reposts.get(order["id"], []) if t > self.now - 120])
        if churn >= 3:
            stops = without_windows(stops, INF)   # the windows keep expiring: post without them
        if len(legs) > 1 and planned_drive_minutes(order, legs) > 12 * HOUR:
            # The server rejects combined gigs over 12 hours. Split them before submission so
            # the practice loop can keep every valid action in its next advance request.
            for leg in legs:
                self.post(order, [leg], retry)
            return
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": self.fees_for(order, len(legs))})

    def gigs_of(self, order_id: str) -> list:
        return list(dict.fromkeys(self.leg_gigs.get(order_id, {}).values()))

    def carry(self, action: dict, gig_id: str) -> None:
        for s in action["stops"]:
            self.leg_gigs.setdefault(s["orderId"], {})[s["leg"]] = gig_id
        self.posts[gig_id] = action
        if gig_id not in self.gig_info:
            self.gig_info[gig_id] = {"orderId": action["stops"][0]["orderId"], "postedAt": self.now,
                                     "firstFee": action["schedule"][0]["feeCents"], "cars": pickups(action)}

    def withdraw(self, order_id: str) -> bool:
        gigs = self.gigs_of(order_id)
        if any(g in self.booked for g in gigs):
            return False
        self.discard_pending_posts(order_id)
        self.leg_gigs.pop(order_id, None)
        for g in gigs:
            self.outbox.append({"type": "withdraw", "gigId": g})
        return True

    def discard_pending_posts(self, order_id: str) -> None:
        """Drop not-yet-sent posts before replacing an order's plan."""
        self.outbox = [a for a in self.outbox if not (
            a["type"] == "post" and any(s["orderId"] == order_id for s in a["stops"]))]

    def send(self) -> None:
        actions, self.outbox = self.outbox, []
        for i in range(0, len(actions), 50):
            chunk = actions[i:i + 50]
            self.read_results(chunk, self.api.act(self.world_id, chunk))

    def read_results(self, actions: list, results: list) -> None:
        for a, r in zip(actions, results):
            if a["type"] == "post" and r.get("ok") is True:
                self.carry(a, r["id"])
            if r.get("ok") is False:
                self.log.append((self.now, a["type"], r.get("reason")))


# ------------------------------------------------------------ helpers (ported from the starter)
def retry_from(order: dict, request: dict, leg: int, now: int) -> int:
    promised = next((o["deadline"] for o in request["options"] if o["kind"] == order["option"]), order["deadline"])
    delay = max(0, order["deadline"] - promised)
    return max(now + 2 * HOUR, order["legs"][leg]["pickup"]["window"]["from"] + delay)


def undelivered(order: dict) -> list:
    return [i for i in range(len(order["legs"])) if order["deliveredAt"][i] is None]


def pickups(action: dict) -> int:
    return sum(1 for s in action["stops"] if s["kind"] == "pickup")


def without_windows(stops: list, before: float) -> list:
    return [{k: v for k, v in s.items() if k != "window"} if "window" in s and s["window"]["to"] < before else s for s in stops]


def round_half_up(x: float) -> int:
    """JavaScript's Math.round: halves go up."""
    return math.floor(x + 0.5)


def rising(now: int, fee: float, level: int) -> list:
    n = 1 if level == 1 else 6
    return [{"at": now + k * HOUR, "feeCents": max(1, round_half_up(fee * (1 + k / 10)))} for k in range(n)]


def steep(now: int, fee: float, level: int) -> list:
    n = 1 if level == 1 else 5
    return [{"at": now + k * 30, "feeCents": max(1, round_half_up(fee * (1.5 + k / 4)))} for k in range(n)]


def stops_for(order: dict, legs: list, note: Optional[dict], retry: dict, extra: Optional[dict] = None) -> list:
    extra = extra or {}
    stops = []
    for leg in legs:
        car, pickup, delivery = order["legs"][leg]["car"], order["legs"][leg]["pickup"], order["legs"][leg]["delivery"]
        noted = window_from_note(note, pickup["window"], "pickup")
        if extra.get("pickupWindow"):
            noted = extra["pickupWindow"](leg, noted, pickup["window"])
        start = max(noted["from"], retry["pickupFrom"])
        pickup_window = {"from": start, "to": max(noted["to"], start)}
        delivery_base = {"from": delivery["window"]["from"], "to": max(delivery["window"]["from"], order["deadline"])}
        delivery_window = window_from_note(note, delivery_base, "delivery")
        if extra.get("deliveryWindow"):
            delivery_window = extra["deliveryWindow"](leg, delivery_window, delivery_base)
        low_battery = bool(car["electric"] and car.get("chargePercent") is not None and car["chargePercent"] < 30)
        first = {"orderId": order["id"], "leg": leg, "kind": "pickup", "withPlates": retry["plates"], "withCharging": low_battery}
        second = {"orderId": order["id"], "leg": leg, "kind": "delivery"}
        stops += [narrower(first, pickup_window, pickup["window"]), narrower(second, delivery_window, delivery_base)]
    return stops


def narrower(stop: dict, w: dict, base: dict) -> dict:
    return {**stop, "window": w} if (w["from"] > base["from"] or w["to"] < base["to"]) else stop


# The starter's keyword reader; the JS regexes use ASCII \b and \d, so re.ASCII here.
AFTER = re.compile(r"jälkeen|alkaen|aikaisintaan|\bafter\b|\bfrom\b|earliest|not before|ei ennen|valmis|\bready\b|efter|tidigast|inte före|klar", re.ASCII)
BEFORE = re.compile(r"ennen|mennessä|viim|\bbefore\b|\bby\b|latest|före|senast", re.ASCII)
DELIVERY = re.compile(r"toimit|perillä|perille|vastaanot|kohde|deliver|arrive|destination|receiv|leverera|leverans|framme|mottag", re.ASCII)
CLOCK = re.compile(r"(?:klo|kl\.?)\s*(\d{1,2})(?:[.:](\d{2}))?|\b(\d{1,2})[.:](\d{2})\b", re.ASCII)
RANGE = re.compile(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*[-–]\s*(\d{1,2})(?:[.:](\d{2}))?\b", re.ASCII)


def window_from_note(note: Optional[dict], window: dict, stop: str) -> dict:
    if not note:
        return window
    text = note["text"].lower()
    if bool(DELIVERY.search(text)) != (stop == "delivery"):
        return window

    def minutes(h: int, m: int) -> Optional[int]:
        return None if (h > 24 or m > 59) else h * HOUR + m

    start, end = window["from"], window["to"]
    for m in RANGE.finditer(text):
        opens, closes = minutes(int(m[1]), int(m[2] or 0)), minutes(int(m[3]), int(m[4] or 0))
        if opens is None or closes is None or opens >= closes:
            continue
        start = max(start, start // DAY * DAY + opens)
        end = min(end, end // DAY * DAY + closes)
    text = RANGE.sub(" ", text)
    for m in CLOCK.finditer(text):
        mm = minutes(int(m[1] or m[3]), int(m[2] or m[4] or 0))
        if mm is None:
            continue
        time = window["from"] // DAY * DAY + mm
        around = text[max(0, m.start() - 16): m.end() + 16]
        if AFTER.search(around):
            start = max(start, time)
        elif BEFORE.search(around):
            end = min(end, time)
    return {"from": start, "to": end} if start <= end else window


# ------------------------------------------------------------ practice loop
def play_practice(api, world_id: str, level: int, policy: Any, keep_events: bool = False,
                  resume: bool = False, state_path: Optional[str] = None):
    """Play a practice world, optionally rebuilding policy state after an interruption.

    ``resume`` is intentionally explicit: a new world starts with a blank policy, whereas an
    interrupted world must replay its server history before it can safely make another decision.
    """
    saved = None
    if resume and state_path:
        try:
            saved = json.loads(Path(state_path).read_text())
            if saved.get("worldId") != world_id:
                saved = None
        except (OSError, ValueError):
            pass
    snap = api.snapshot(world_id)
    desk = Desk(api, world_id, level, snap["now"], policy)
    init = desk.hook("init")
    if init:
        init(desk)
    after = -1
    if resume:
        history = api.events(world_id, -1)
        if saved and hasattr(policy, "set_state"):
            policy.set_state(saved["policy"])
            after = saved.get("after", -1)
            # Desk state always comes from the authoritative snapshot.  Only replay policy
            # events that happened after the checkpoint; replaying all history after every
            # interrupted terminal slice is both slow and would double-count observations.
            desk.rejoin(snap, history, False)
            on_event = desk.hook("on_event")
            if on_event:
                for event in history:
                    if event["seq"] > after:
                        on_event(desk, event, False)
            after = max([after, *(event["seq"] for event in history)])
        else:
            desk.rejoin(snap, history, True)
            after = max((event["seq"] for event in history), default=-1)

    def checkpoint() -> None:
        if state_path and hasattr(policy, "get_state"):
            Path(state_path).write_text(json.dumps({"worldId": world_id, "after": after,
                                                     "policy": policy.get_state()}))
    ends_at = snap["endsAt"]
    events: list = []
    while desk.now < ends_at:
        if len(desk.outbox) > 50:   # the advance carries at most 50 actions: send the earlier ones first
            keep, desk.outbox = desk.outbox[-50:], desk.outbox[:-50]
            desk.send()
            desk.outbox = keep
        actions, desk.outbox = desk.outbox, []
        next_wake = desk.hook("next_wake")
        wake = next_wake(desk) if next_wake else INF
        body: dict = {"untilEvent": getattr(policy, "wake_on", None) or WAKE_ON,
                      "max": int(min(ends_at, max(desk.now + 1, wake)))}
        if actions:
            body["actions"] = actions
        step = api.advance(world_id, body)
        if "actions" in body:
            desk.read_results(body["actions"], step["results"])
        desk.now = step["now"]
        for e in step["events"]:
            after = max(after, e["seq"])
            if keep_events:
                events.append(e)
            desk.handle(e)
        tick = desk.hook("tick")
        if tick:
            tick(desk)
        checkpoint()
    score = api.score(world_id)
    checkpoint()
    return desk, score, events
