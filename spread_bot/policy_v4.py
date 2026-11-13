"""v4 policy (port of src/policy_v4.js) = v2 + per-pickup-town booking success (learnt live, prior from
the roster) priced into quotes and opening fees + a global lateness shadow price + earlier two-step
escalation. This is the policy the bot plays."""
from __future__ import annotations

import math
from typing import Optional

from .desk import round_half_up, steep
from .geo import P, hav, road
from .notes import parse_note
from .policy_v2 import CompModel, eff_deadline, leg_minutes, make_grid


class PolicyV4:
    DEFAULTS = {
        "maxKm": 450, "minSlack": 360, "minSlackPriv": 600, "tradeinPriv": False,
        "f0": 0.92, "f0Risk": 0.30,          # opening fee = (f0 + f0Risk * pFail_town) * ref
        "f1": 1.25, "f1Lead": 420, "f2": 1.5, "f2Lead": 270,
        "costMult": 1.0,                     # fee / ref paid on a smooth booking
        "failRef": 1.0, "failPrice": 0.15,   # extra cost when a car gets no driver before the warning
        "priorW": 4,
        "targetLate": 0.06, "shadowK": 25,
        # unbooked for 4 h counts as a failure; own unbooked gigs in a town raise its risk
        "earlyFail": 240, "backlogB": 10, "warnW": 0.4, "pendW": 0.15, "hardLate": 0.075, "hardPFail": 0.15,
        # structural risk by slack and distance
        "riskSlack": [[480, 0.20], [720, 0.07], [1e9, 0.02]], "riskKm": [[200, 0], [300, 0.04], [1e9, 0.14]], "optRiskGap": 0.08,
        "uMin": 1.0, "uMax": 2.3, "minEv": 0, "k": 11, "war": 0.15, "daySd": 0.12, "declineMult": 5,
    }

    def __init__(self, params: Optional[dict] = None):
        p = self.params = {**self.DEFAULTS, **(params or {})}
        self.comp = CompModel(k=p["k"], war=p["war"], daySd=p["daySd"])
        self.trust: dict = {}        # dealer -> on-time count (0..25)
        self.quoted: dict = {}       # request id -> x (log price/list, trust-adjusted)
        self.town: dict = {}         # pickup place -> {ok, fail}
        self.order_town: dict = {}   # order -> pickup place
        self.resolved: dict = {}     # order -> 'ok' | 'fail'
        self.homes: Optional[dict] = None
        self.late_orders = self.done_orders = self.last_now = 0
        self.open_warned: set = set()
        self.open_orders: set = set()
        self.pending: dict = {}      # order id -> minute its first gig was posted (until booked)
        self.unbooked: dict = {}     # order id -> minute since it has had no driver
        self.notes: dict = {}        # request id -> parsed note
        self.grid = make_grid(p["uMin"], p["uMax"])
        self.stats = {"quoted": 0, "declined": 0, "evNeg": 0}

    # ------------------------------------------------------------ notes and slack
    def note_of(self, req: dict) -> dict:
        if req["id"] not in self.notes:
            note = req.get("note")
            self.notes[req["id"]] = parse_note(note["text"] if note else None, req["arrivedAt"])
        return self.notes[req["id"]]

    def slack_of(self, req: dict, opt: dict) -> int:
        f = self.note_of(req)
        stated = req["legs"][0]["pickup"]["window"]["from"]
        earliest = max(stated, f["ready_at"] or 0, f["release_at"] or 0)
        shift = max(0, earliest // 1440 - stated // 1440) * 1440   # a later ready day moves the deadline
        deadline = eff_deadline(req, opt["deadline"] + shift)
        if f["deadline_at"] is not None:
            deadline = min(deadline, f["deadline_at"])
        if f["delivery_close"] is not None:
            deadline = min(deadline, deadline // 1440 * 1440 + f["delivery_close"])
        start = max(req["respondBy"], earliest)
        last = deadline - leg_minutes(req) - 30
        if f["pickup_close"] is not None:
            day_close = last // 1440 * 1440 + f["pickup_close"]
            if day_close < last:
                last = day_close
        s = last - start
        pw = req["legs"][0]["pickup"]
        if pw["privateAddress"]:
            s = min(s, pw["window"]["to"] - start)
        return s

    # stop windows from the note, inside the order's windows; one day of pickup hours at a time
    def pickup_window_for(self, req: dict, base: dict, retry_from: int) -> dict:
        f = self.note_of(req)
        frm = max(base["from"], f["ready_at"] or 0, f["release_at"] or 0, retry_from or 0)
        to = base["to"]
        if f["pickup_close"] is not None:
            day = frm // 1440
            if f["pickup_open"] is not None:
                frm = max(frm, day * 1440 + f["pickup_open"])
            close = day * 1440 + f["pickup_close"]
            if close <= frm + 30:   # closed for today: the next business day's hours
                nd = day + 1 + (7 - (day + 1) % 7 if (day + 1) % 7 >= 5 else 0)
                frm = max(frm, nd * 1440 + (f["pickup_open"] if f["pickup_open"] is not None else 8 * 60))
                close = nd * 1440 + f["pickup_close"]
            to = min(to, close)
        return {"from": frm, "to": to} if to > frm else base

    def delivery_window_for(self, req: dict, base: dict) -> dict:
        f = self.note_of(req)
        to = base["to"]
        if f["deadline_at"] is not None and f["deadline_at"] > base["from"]:
            to = min(to, f["deadline_at"])
        if f["delivery_close"] is not None:
            c = to // 1440 * 1440 + f["delivery_close"]
            if base["from"] < c < to:
                to = c
        return {"from": base["from"], "to": to}

    # ------------------------------------------------------------ the risk of no driver, per town
    def prior_ok(self, place: str) -> float:
        if self.homes is None:
            return 0.8
        n = sum(c for h, c in self.homes.items() if hav(P[place], P[h]) <= 30)
        return 0.55 + 0.40 * (1 - math.exp(-n / 15))

    def backlog(self, place: str) -> int:
        return sum(1 for oid in self.pending if self.order_town.get(oid) == place)

    def p_fail(self, place: str, with_backlog: bool = True) -> float:
        p = self.params
        t = self.town.get(place, {"ok": 0, "fail": 0})
        pf = 1 - (t["ok"] + p["priorW"] * self.prior_ok(place)) / (t["ok"] + t["fail"] + p["priorW"])
        return 1 - (1 - pf) * math.exp(-self.backlog(place) / p["backlogB"]) if with_backlog else pf

    def sweep_pending(self, now: int) -> None:
        for oid, t0 in list(self.pending.items()):
            h = (now % 1440) / 60
            if now - t0 > self.params["earlyFail"] and 9 <= h <= 20:
                self.resolve(oid, "fail")
                del self.pending[oid]

    def late_share(self) -> float:
        """Projected late share: known late + open orders already warned + open orders unbooked for hours."""
        n = self.done_orders + len(self.open_orders)
        if n < 15:
            return 0
        now = self.last_now
        old_pending = sum(1 for t0 in self.unbooked.values() if now - t0 > 180)
        return (self.late_orders + self.params["warnW"] * len(self.open_warned) + self.params["pendW"] * old_pending) / n

    def shadow(self) -> float:
        return 1 + self.params["shadowK"] * max(0, self.late_share() - self.params["targetLate"]) * 10

    def resolve(self, oid: str, how: str) -> None:
        if oid in self.resolved:
            return
        self.resolved[oid] = how
        pl = self.order_town.get(oid)
        if not pl:
            return
        self.town.setdefault(pl, {"ok": 0, "fail": 0})[how] += 1

    # ------------------------------------------------------------ state (saved every 2 s in the final)
    def get_state(self) -> dict:
        return {"comp": self.comp.get_state(), "trust": self.trust, "quoted": self.quoted, "town": self.town, "orderTown": self.order_town,
                "resolved": self.resolved, "pending": self.pending, "unbooked": self.unbooked, "lastNow": self.last_now,
                "lateOrders": self.late_orders, "doneOrders": self.done_orders, "openWarned": list(self.open_warned), "openOrders": list(self.open_orders)}

    def set_state(self, st: dict) -> None:
        self.comp.set_state(st["comp"])
        self.trust.update(st["trust"])
        self.quoted.update(st["quoted"])
        self.town.update(st["town"])
        self.order_town.update(st["orderTown"])
        self.resolved.update(st["resolved"])
        self.pending.update(st.get("pending") or {})
        self.unbooked.update(st.get("unbooked") or {})
        self.last_now = st.get("lastNow") or 0
        self.late_orders, self.done_orders = st["lateOrders"], st["doneOrders"]
        self.open_warned.update(st["openWarned"])
        self.open_orders.update(st["openOrders"])

    # ------------------------------------------------------------ the policy
    def init(self, desk) -> None:
        roster = desk.api.roster(desk.world_id)
        self.homes = {}
        for d in roster:
            self.homes[d["homePlaceId"]] = self.homes.get(d["homePlaceId"], 0) + 1

    def quote(self, desk, req: dict) -> dict:
        p = self.params
        km = max(road(l["pickup"]["placeId"], l["delivery"]["placeId"])["km"] for l in req["legs"])
        priv = any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in req["legs"])

        def step(tab: list, v: float) -> float:
            return next(val for lim, val in tab if v < lim)

        def structural(o: dict) -> float:
            return 1 - (1 - step(p["riskSlack"], self.slack_of(req, o))) * (1 - step(p["riskKm"], km))

        ok_opts = [o for o in req["options"] if km <= p["maxKm"] and self.slack_of(req, o) >= (p["minSlackPriv"] if priv else p["minSlack"])
                   and not (len(req["legs"]) > 1 and priv and not p["tradeinPriv"])]
        if len(ok_opts) > 1:   # offer a fast option only if it is not much riskier
            min_r = min(structural(o) for o in ok_opts)
            ok_opts = [o for o in ok_opts if structural(o) <= min_r + p["optRiskGap"]]
        prices = {o["kind"]: round_half_up(o["listPriceCents"] * p["declineMult"] / 100) * 100 for o in req["options"]}
        if not ok_opts:
            self.stats["declined"] += 1
            return prices
        o, ref = ok_opts[0], req["referenceFeeCents"]
        lst = o["listPriceCents"]
        t = 1 + 0.01 * self.trust.get(req["dealerId"], 0)
        pw = self.comp.p_win(req["arrivedAt"], [math.log(u / t) for u in self.grid])
        pf = 1 - (1 - max(self.p_fail(l["pickup"]["placeId"]) for l in req["legs"])) * (1 - max(structural(o) for o in ok_opts))
        ls = self.late_share()
        if ls >= p["hardLate"] and pf > p["hardPFail"]:   # hard brake: only safe cars, which also dilute the late share
            self.stats["braked"] = self.stats.get("braked", 0) + 1
            return prices
        lam = self.shadow()
        f0 = p["f0"] + p["f0Risk"] * pf
        best, bu = -1, None
        for i, u in enumerate(self.grid):
            price = u * lst
            cost = p["costMult"] * f0 * ref + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
            ev = pw[i] * (price - cost)
            if ev > best:
                best, bu = ev, u
        if best <= p["minEv"]:
            self.stats["evNeg"] += 1
            return prices
        for oo in ok_opts:
            prices[oo["kind"]] = round_half_up(oo["listPriceCents"] * bu / 100) * 100
        self.quoted[req["id"]] = math.log(bu / t)
        self.stats["quoted"] += 1
        return prices

    def on_event(self, desk, e: dict, replay: bool = False) -> None:
        self.last_now = max(self.last_now, e["at"])
        self.sweep_pending(e["at"])
        kind = e["kind"]
        if kind == "order_won":
            oid = e["order"]["id"]
            if oid in self.quoted:
                r = desk.requests.get(oid)
                self.comp.observe(r["arrivedAt"] if r else e["at"], self.quoted.pop(oid), True)
            self.order_town[oid] = e["order"]["legs"][0]["pickup"]["placeId"]
            self.open_orders.add(oid)
            self.pending[oid] = e["at"]
            self.unbooked[oid] = e["at"]
        elif kind == "order_lost":
            if e["requestId"] in self.quoted:
                r = desk.requests.get(e["requestId"])
                self.comp.observe(r["arrivedAt"] if r else e["at"], self.quoted.pop(e["requestId"]), False)
        elif kind == "gig_booked":
            info = desk.gig_info.get(e["gigId"])
            oid = info["orderId"] if info else None
            if oid:
                self.pending.pop(oid, None)
                self.unbooked.pop(oid, None)
                if oid not in self.open_warned:
                    self.resolve(oid, "ok")
        elif kind == "deadline_close":
            self.resolve(e["orderId"], "fail")
            self.pending.pop(e["orderId"], None)
            if e["orderId"] in self.open_orders:
                self.open_warned.add(e["orderId"])
        elif kind == "order_cancelled":
            self.open_orders.discard(e["orderId"])
            self.open_warned.discard(e["orderId"])
            self.pending.pop(e["orderId"], None)
            self.unbooked.pop(e["orderId"], None)
        elif kind in ("gig_dropped", "no_show"):
            info = desk.gig_info.get(e["gigId"])
            oid = info["orderId"] if info else None
            if oid and oid in self.open_orders:
                self.unbooked[oid] = e["at"]
        elif kind == "delivered":
            o = desk.orders.get(e["orderId"])
            if not o:
                return
            done = [e["at"] if i == e["leg"] else d for i, d in enumerate(o["deliveredAt"])]
            if all(d is not None for d in done):
                on_time = all(d <= o["deadline"] for d in done)
                t = self.trust.get(o["dealerId"], 0)
                self.trust[o["dealerId"]] = min(25, t + 1) if on_time else t // 2
                self.open_orders.discard(e["orderId"])
                self.open_warned.discard(e["orderId"])
                self.unbooked.pop(e["orderId"], None)
                self.done_orders += 1
                if not on_time:
                    self.late_orders += 1

    def post_extras(self, desk, order: dict, legs: list, retry: dict) -> dict:
        req = desk.requests[order["id"]]
        f = self.note_of(req)
        return {
            "manual": f["manual"], "plates": f["plates"],
            "pickupWindow": lambda leg, noted, base: self.pickup_window_for(req, base, retry["pickupFrom"]),
            "deliveryWindow": lambda leg, noted, base: self.delivery_window_for(req, base),
        }

    def on_unbooked(self, desk, e: dict) -> None:
        """A drop or a no-show: the old fee did not hold a driver, so go up a step now."""
        info = desk.gig_info.get(e["gigId"])
        if not info:
            return
        order = desk.orders.get(info["orderId"])
        if not order or order["id"] in desk.urgent:
            return
        p = self.params
        ref = desk.requests[order["id"]]["referenceFeeCents"] * info["cars"] / len(order["legs"])
        post = desk.posts.get(e["gigId"])
        cur = max([s["feeCents"] for s in (post["schedule"] if post else []) if s["at"] <= desk.now], default=0)
        f1 = max(cur * 1.15, ref * p["f1"])
        t2 = max(desk.now + 1, order["lastPickupAt"] - p["f2Lead"])
        schedule = [{"at": desk.now, "feeCents": round_half_up(f1)}, {"at": t2, "feeCents": round_half_up(max(f1, ref * p["f2"]))}]
        desk.outbox.append({"type": "reprice", "gigId": e["gigId"], "schedule": schedule})
        if post:
            desk.posts[e["gigId"]] = {**post, "schedule": schedule}

    def fees(self, desk, order: dict, cars: int, urgent: bool) -> list:
        p = self.params
        ref = desk.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        if urgent:
            return steep(desk.now, ref, 8)
        pf = self.p_fail(order["legs"][0]["pickup"]["placeId"], False)
        f0 = p["f0"] + p["f0Risk"] * pf
        steps = [{"at": desk.now, "feeCents": round_half_up(ref * f0)}]
        t1 = max(desk.now + 1, order["lastPickupAt"] - p["f1Lead"])
        t2 = max(t1 + 1, order["lastPickupAt"] - p["f2Lead"])
        steps.append({"at": t1, "feeCents": round_half_up(ref * max(f0, p["f1"]))})
        steps.append({"at": t2, "feeCents": round_half_up(ref * max(f0, p["f2"]))})
        return steps
