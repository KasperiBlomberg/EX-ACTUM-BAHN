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
import re
from typing import Any, Callable, Optional

HOUR, DAY = 60, 1440
INF = float("inf")
WAKE_ON = ["price_request", "order_won", "order_changed", "order_cancelled", "visit_failed", "gig_expired", "deadline_close"]
NO_RETRY = {"pickupFrom": 0, "manualLicence": False, "plates": False}


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
        self.driver_positions: dict = {}  # driver id -> current place, for targeted direct offers
        self.bundle_fallbacks: dict = {}  # id(post action) -> [(order id, legs, retry)] if a chain is refused
        self.reposts: dict = {}     # order id -> minutes of reposts after expiry
        self.log: list = []

    def hook(self, name: str) -> Optional[Callable]:
        return getattr(self.policy, name, None)

    # ------------------------------------------------------------ rejoin (final / restart)
    def rejoin(self, snapshot: dict, history: list, replay_policy: bool = True) -> None:
        self.set_driver_positions(snapshot.get("driverPositions") or [])
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
            self.cancel(e)
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
        tick = self.hook("tick")
        if tick:
            tick(self)

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
            queue = self.hook("queue_order")
            if not queue or not queue(self, order):
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
        by_order: dict = {}
        for s in action["stops"]:
            # A chain may hold legs from several orders.  Its replacement is deliberately split:
            # re-evaluate constraints/window notes instead of assuming the old chain still fits.
            if s["kind"] == "pickup" and self.leg_gigs.get(s["orderId"], {}).get(s["leg"]) == gig_id:
                by_order.setdefault(s["orderId"], []).append(s["leg"])
            lg = self.leg_gigs.get(s["orderId"])
            if lg is not None and lg.get(s["leg"]) == gig_id:
                lg.pop(s["leg"], None)
        for order_id, legs in by_order.items():
            order = self.orders.get(order_id)
            if not order:
                continue
            self.reposts.setdefault(order_id, []).append(self.now)
            retry = {"pickupFrom": self.now, "manualLicence": action["audience"]["manualLicence"],
                     "plates": any(s.get("withPlates") for s in action["stops"] if s["orderId"] == order_id)}
            self.post(order, [leg for leg in legs if order["deliveredAt"][leg] is None], retry)

    def raise_fees(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if not order:
            return
        self.urgent.add(order_id)
        self.send_post_of(order_id)
        for gig_id in self.gigs_of(order_id):
            if gig_id not in self.booked:
                self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": self.fees_for_stops(self.posts[gig_id]["stops"])})

    def fees_for(self, order: dict, cars: int) -> list:
        urgent = order["id"] in self.urgent
        fees = self.hook("fees")
        custom = fees(self, order, cars, urgent) if fees else None
        if custom:
            return custom
        fee = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        return steep(self.now, fee, self.level) if urgent else rising(self.now, fee, self.level)

    def fee_items(self, stops: list) -> list:
        """Return the undelivered pickup legs in a post, grouped by order."""
        found: dict = {}
        for s in stops:
            if s["kind"] == "pickup" and s["orderId"] in self.orders:
                found.setdefault(s["orderId"], []).append(s["leg"])
        return [(self.orders[order_id], legs) for order_id, legs in found.items()]

    def fees_for_stops(self, stops: list) -> list:
        items = self.fee_items(stops)
        if not items:
            return []
        urgent = any(order["id"] in self.urgent for order, _ in items)
        bundled = self.hook("fees_bundle")
        if bundled:
            custom = bundled(self, items, urgent)
            if custom:
                return custom
        if len(items) == 1:
            order, legs = items[0]
            return self.fees_for(order, len(legs))
        fee = sum(self.requests[order["id"]]["referenceFeeCents"] * len(legs) / len(order["legs"])
                  for order, legs in items)
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
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": self.fees_for(order, len(legs))})
        if len(legs) == 1:
            return
        self.send()
        if not self.gigs_of(order["id"]):
            for leg in legs:
                self.post(order, [leg], retry)

    def post_bundle(self, items: list, retry: Optional[dict] = None) -> bool:
        """Post one valid chain for legs from several orders.

        The policy chooses the route and has already checked its 12-hour road budget.  Desk still
        owns notes, retry flags, the normal window-expiry safeguards, and all gig bookkeeping.
        """
        retry = retry or NO_RETRY
        if len(items) < 2:
            return False
        stops, experienced, manual, plates = [], False, retry["manualLicence"], retry["plates"]
        for order, legs in items:
            if not legs or any(leg in self.leg_gigs.get(order["id"], {}) for leg in legs):
                return False
            extra_hook = self.hook("post_extras")
            extra = extra_hook(self, order, legs, retry) if extra_hook else {}
            experienced = experienced or any(order["legs"][leg]["car"]["premium"] for leg in legs)
            manual = manual or bool(extra.get("manual"))
            plates = plates or bool(extra.get("plates"))
            r2 = {**retry, "plates": plates}
            stops.extend(stops_for(order, legs, self.requests[order["id"]].get("note"), r2, extra))
        stops = without_windows(stops, self.now + 120)
        if not stops:
            return False
        action = {"type": "post", "stops": stops,
                  "audience": {"experienced": experienced, "manualLicence": manual},
                  "schedule": self.fees_for_stops(stops)}
        if not action["schedule"]:
            return False
        self.outbox.append(action)
        self.bundle_fallbacks[id(action)] = [(order["id"], list(legs), dict(retry)) for order, legs in items]
        return True

    def gigs_of(self, order_id: str) -> list:
        return list(dict.fromkeys(self.leg_gigs.get(order_id, {}).values()))

    def carry(self, action: dict, gig_id: str) -> None:
        for s in action["stops"]:
            self.leg_gigs.setdefault(s["orderId"], {})[s["leg"]] = gig_id
        self.posts[gig_id] = action
        if gig_id not in self.gig_info:
            order_ids = list(dict.fromkeys(s["orderId"] for s in action["stops"] if s["kind"] == "pickup"))
            self.gig_info[gig_id] = {"orderId": action["stops"][0]["orderId"], "orderIds": order_ids, "postedAt": self.now,
                                     "firstFee": action["schedule"][0]["feeCents"], "cars": pickups(action)}

    def withdraw(self, order_id: str) -> bool:
        self.send_post_of(order_id)
        gigs = self.gigs_of(order_id)
        if any(g in self.booked for g in gigs):
            return False
        repost: dict = {}
        for g in gigs:
            # Withdrawing one changed order removes an open chain from the app.  Put the other
            # orders back immediately after the withdrawal rather than leaving stale gig links.
            for s in self.posts.get(g, {}).get("stops", []):
                if s["kind"] != "pickup" or s["orderId"] == order_id:
                    continue
                lg = self.leg_gigs.get(s["orderId"])
                if lg is not None and lg.get(s["leg"]) == g:
                    lg.pop(s["leg"], None)
                    repost.setdefault(s["orderId"], []).append(s["leg"])
        self.leg_gigs.pop(order_id, None)
        for g in gigs:
            self.outbox.append({"type": "withdraw", "gigId": g})
        for other_id, legs in repost.items():
            other = self.orders.get(other_id)
            if other:
                self.post(other, [leg for leg in legs if other["deliveredAt"][leg] is None])
        return True

    def cancel(self, event: dict) -> None:
        """Repost live siblings when the server withdraws their open chain on a cancellation."""
        order_id = event["orderId"]
        self.orders.pop(order_id, None)
        self.leg_gigs.pop(order_id, None)
        for changed in event.get("gigs") or []:
            if changed["outcome"] != "withdrawn":
                continue
            action = self.posts.get(changed["gigId"])
            if not action:
                continue
            by_order: dict = {}
            for s in action["stops"]:
                if s["kind"] != "pickup" or s["orderId"] == order_id:
                    continue
                lg = self.leg_gigs.get(s["orderId"])
                if lg is not None and lg.get(s["leg"]) == changed["gigId"]:
                    lg.pop(s["leg"], None)
                    by_order.setdefault(s["orderId"], []).append(s["leg"])
            for other_id, legs in by_order.items():
                other = self.orders.get(other_id)
                if other:
                    self.post(other, [leg for leg in legs if other["deliveredAt"][leg] is None])

    def send_post_of(self, order_id: str) -> None:
        if any(a["type"] == "post" and any(s["orderId"] == order_id for s in a["stops"]) for a in self.outbox):
            self.send()

    def send(self) -> None:
        actions, self.outbox = self.outbox, []
        for i in range(0, len(actions), 50):
            chunk = actions[i:i + 50]
            self.read_results(chunk, self.api.act(self.world_id, chunk))

    def read_results(self, actions: list, results: list) -> None:
        for a, r in zip(actions, results):
            if a["type"] == "post" and r.get("ok") is True:
                self.carry(a, r["id"])
                self.bundle_fallbacks.pop(id(a), None)
            if r.get("ok") is False:
                self.log.append((self.now, a["type"], r.get("reason")))
                # A valid 12-hour chain can still be rejected when a particular order's live
                # window is too tight.  Recover each leg rather than silently abandoning it.
                for order_id, legs, retry in self.bundle_fallbacks.pop(id(a), []):
                    order = self.orders.get(order_id)
                    if order and not self.gigs_of(order_id):
                        self.post(order, [leg for leg in legs if order["deliveredAt"][leg] is None], retry)

    def set_driver_positions(self, positions: list) -> None:
        self.driver_positions = {p["driverId"]: p["placeId"] for p in positions}

    def refresh_driver_positions(self) -> dict:
        """Read the small live-position view only when a policy is about to make direct offers."""
        snap = self.api.snapshot(self.world_id)
        self.set_driver_positions(snap.get("driverPositions") or [])
        return self.driver_positions


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
def play_practice(api, world_id: str, level: int, policy: Any, keep_events: bool = False):
    snap = api.snapshot(world_id)
    desk = Desk(api, world_id, level, snap["now"], policy)
    desk.set_driver_positions(snap.get("driverPositions") or [])
    init = desk.hook("init")
    if init:
        init(desk)
    ends_at = snap["endsAt"]
    events: list = []
    while desk.now < ends_at:
        midnight = (desk.now // DAY + 1) * DAY
        if len(desk.outbox) > 50:   # the advance carries at most 50 actions: send the earlier ones first
            keep, desk.outbox = desk.outbox[-50:], desk.outbox[:-50]
            desk.send()
            desk.outbox = keep
        actions, desk.outbox = desk.outbox, []
        next_wake = desk.hook("next_wake")
        wake = next_wake(desk) if next_wake else INF
        body: dict = {"untilEvent": getattr(policy, "wake_on", None) or WAKE_ON, "max": int(min(ends_at, midnight, max(desk.now + 1, wake)))}
        if actions:
            body["actions"] = actions
        step = api.advance(world_id, body)
        if "actions" in body:
            desk.read_results(body["actions"], step["results"])
        desk.now = step["now"]
        for e in step["events"]:
            if keep_events:
                events.append(e)
            desk.handle(e)
        tick = desk.hook("tick")
        if tick:
            tick(desk)
    score = api.score(world_id)
    return desk, score, events
