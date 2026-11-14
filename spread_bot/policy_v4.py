"""v4 policy (port of src/policy_v4.js) = v2 + per-pickup-town booking success (learnt live, prior from
the roster) priced into quotes and opening fees + a global lateness shadow price + earlier two-step
escalation. This is the policy the bot plays."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from .desk import DAY, INF, round_half_up, steep
from .geo import P, hav, road
from .notes import parse_note
from .policy_v2 import CompModel, eff_deadline, leg_minutes, make_grid


def load_lanes() -> dict:
    """The competitor's lasting lane factors (log price), fitted by ``lane_fit.py``."""
    return json.loads(Path(__file__).with_name("lanes.json").read_text())


class PolicyV4:
    # The bot played by ``python -m spread_bot`` / ``--final``.  New mechanisms stay opt-in
    # until their own paired practice ablation clears the on-time constraint and improves margin.
    # ``DEFAULTS`` below remain feature-complete so focused unit tests and explicit experiments
    # can construct the individual mechanisms without boilerplate.
    SAFE_DEPLOYMENT = {
        "separateOptionPricing": False,
        "enableDealerEffects": False,
        "enableBundles": False,
        "enableOffers": False,
        "enableSnow": False,
    }
    # What the CLI plays: the G control plus every addition that has cleared its paired ablation.
    # trustBonus: six seeds, mean +EUR 326 a week against G, worst on time 95.1% (14 Nov 2026).
    # laneScale on top of it: the same six seeds, mean +EUR 761 a week against G (+EUR 435 against
    # the trust bonus alone), five of six seeds up, worst on time 94.4%.
    RANKED_DEPLOYMENT = {**SAFE_DEPLOYMENT, "trustBonus": 1.0, "laneScale": 1.0}
    DEFAULTS = {
        # G beat F on every unseen v-9...v-15 seed.  The structural-risk price and hard brake
        # remain the guardrail, so this expands only the cars whose expected risk pays for it.
        "maxKm": 550, "minSlack": 360, "minSlackPriv": 600, "tradeinPriv": False,
        "f0": 0.92, "f0Risk": 0.30,          # opening fee = (f0 + f0Risk * pFail_town) * ref
        "f1": 1.25, "f1Lead": 420, "f2": 1.5, "f2Lead": 270,
        "costMult": 1.0,                     # fee / ref paid on a smooth booking
        "failRef": 1.0, "failPrice": 0.15,   # extra cost when a car gets no driver before the warning
        "priorW": 4,
        "targetLate": 0.06, "shadowK": 25,
        # unbooked for 4 h counts as a failure; own unbooked gigs in a town raise its risk
        "earlyFail": 240, "backlogB": 12, "warnW": 0.4, "pendW": 0.15, "hardLate": 0.075, "hardPFail": 0.15,
        # structural risk by slack and distance
        "riskSlack": [[480, 0.20], [720, 0.07], [1e9, 0.02]], "riskKm": [[200, 0], [300, 0.04], [1e9, 0.14]], "optRiskGap": 0.08,
        # The price model works in effective log price: speedValue is how much a dealer values an
        # option before comparing its price.  It lets same-day prices rise while steering tight
        # cars to the safer later options.
        "speedValue": {"same_day": 0.105, "next_day": 0.0, "within_3_days": -0.025}, "multiLossWeight": 0.55,
        # Forecasts are weak evidence for ordinary cars, but summer tyres cannot be collected in
        # snow.  Windows are also shifted below so an already-won summer-tyre car is not sent out.
        "snowRisk": 0.055, "summerSnowRisk": 0.30,
        # Same-yard chains wait briefly for a second order, never close to the last safe pickup.
        "bundleWait": 22, "bundleMinLead": 390, "bundleMaxCars": 4, "bundleExtraCost": 0.72,
        # Offers are scarce.  Use them only for a driver currently on one of our gigs near the
        # pickup, before the warning, and pay above the app fee that they expect to rise from.
        "offerLead": 225, "offerNearKm": 55, "offerMult": 1.18, "offerRefMult": 1.22, "offerRefresh": 15,
        # Explicit switches let the experiment harness measure each addition against the G
        # baseline on exactly the same worlds.
        "separateOptionPricing": True, "enableDealerEffects": True, "enableBundles": True,
        "enableOffers": True, "enableSnow": True, "recordTelemetry": False,
        # Level-8 dealer trust is an investment: every on-time order lets that dealer accept 1%
        # more (to 25).  trustBonus scales the estimated future value of one more order into the
        # quote's expected value; 0 keeps the myopic G quote.  trustWeekWins is a typical week.
        "trustBonus": 0.0, "trustWeekWins": 300,
        # The competitor's price has a lane part that is almost the same in every world (dear
        # where few drivers live near the delivery).  laneScale applies the table fitted from our
        # practice reveals as a known offset; the live model then learns only what is new.
        "laneScale": 0.0,
        "uMin": 1.0, "uMax": 2.3, "minEv": 0, "k": 11, "war": 0.15, "daySd": 0.12, "declineMult": 5,
    }

    def __init__(self, params: Optional[dict] = None):
        p = self.params = {**self.DEFAULTS, **(params or {})}
        dealer = {} if p["enableDealerEffects"] else {"dealerEta": 0.0, "dealerCarry": 0.0, "dealerReact": 0.0, "dealerCap": 0.0}
        self.comp = CompModel(k=p["k"], war=p["war"], daySd=p["daySd"], **dealer)
        self.trust: dict = {}        # dealer -> on-time count (0..25)
        self.quoted: dict = {}       # request id -> {option -> effective log price}
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
        self.snow: list = []         # [{from, to, places}], kept from forecast events
        self.bundle_pending: dict = {}  # order id -> minute: held briefly for a same-yard chain
        self.drivers: dict = {}      # driver id -> public profile
        self.offered: dict = {}      # gig id -> {driver id: {day, rowId}}; only open direct offers
        self.driver_offers: dict = {}  # driver id -> {day: number sent}, server allows three/day
        self.dealer_reqs: dict = {}    # dealer -> price requests seen, for the trust investment
        self.booking_rows: dict = {}   # order id -> features at win plus booking/on-time labels
        # One row per direct offer.  These are deliberately observational: a gig can still be
        # booked from the app while an offer is open, so ``gigBookedAt`` is not evidence that the
        # offer caused the booking.  ``answer`` is the clean response label.
        self.offer_rows: list = []
        self.last_position_refresh = -10**9
        self.lanes = load_lanes() if p["laneScale"] else None
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
        # Summer-tyre cars cannot be collected while snow falls at either end.  The server later
        # moves the deadline when necessary; until then, waiting avoids an avoidable failed visit.
        if self.params["enableSnow"] and f["summer"]:
            blocked = self.snow_until(req["legs"][0]["pickup"]["placeId"], frm)
            if blocked is not None:
                frm = max(frm, blocked)
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

    # ------------------------------------------------------------ forecast-aware risk and windows
    def snow_until(self, place: str, at: int) -> Optional[int]:
        """End of snow covering ``place`` at ``at``, if a forecast is currently relevant."""
        ends = [f["to"] for f in self.snow if place in f["places"] and f["from"] <= at < f["to"]]
        return max(ends) if ends else None

    def snow_risk(self, req: dict, opt: dict) -> float:
        """Risk from forecast snow during the plausible collection/delivery period."""
        if not self.params["enableSnow"]:
            return 0.0
        f = self.note_of(req)
        start = max(req["respondBy"], req["legs"][0]["pickup"]["window"]["from"], f["ready_at"] or 0, f["release_at"] or 0)
        end = eff_deadline(req, opt["deadline"])
        places = {x["pickup"]["placeId"] for x in req["legs"]} | {x["delivery"]["placeId"] for x in req["legs"]}
        affected = any(places.intersection(weather["places"]) and weather["from"] < end and start < weather["to"] for weather in self.snow)
        if not affected:
            return 0.0
        return self.params["summerSnowRisk"] if f["summer"] else self.params["snowRisk"]

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
                "lateOrders": self.late_orders, "doneOrders": self.done_orders, "openWarned": list(self.open_warned), "openOrders": list(self.open_orders),
                "snow": self.snow, "bundlePending": self.bundle_pending, "offered": self.offered,
                "driverOffers": self.driver_offers, "lastPositionRefresh": self.last_position_refresh,
                "dealerReqs": self.dealer_reqs,
                "bookingRows": self.booking_rows if self.params["recordTelemetry"] else {},
                "offerRows": self.offer_rows if self.params["recordTelemetry"] else []}

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
        self.snow.extend(st.get("snow") or [])
        self.bundle_pending.update(st.get("bundlePending") or {})
        self.offered.update(st.get("offered") or {})
        self.driver_offers.update(st.get("driverOffers") or {})
        self.last_position_refresh = st.get("lastPositionRefresh", -10**9)
        self.dealer_reqs.update(st.get("dealerReqs") or {})
        self.booking_rows.update(st.get("bookingRows") or {})
        self.offer_rows.extend(st.get("offerRows") or [])

    # ------------------------------------------------------------ the policy
    def init(self, desk) -> None:
        roster = desk.api.roster(desk.world_id)
        self.homes = {}
        for d in roster:
            self.homes[d["homePlaceId"]] = self.homes.get(d["homePlaceId"], 0) + 1
            self.drivers[d["id"]] = d

    def record_booking_row(self, desk, order: dict, at: int) -> None:
        """Capture only information available at the moment we accept operational risk."""
        if not self.params["recordTelemetry"] or order["id"] in self.booking_rows:
            return
        req = desk.requests.get(order["id"])
        if not req:
            return
        option = next((o for o in req["options"] if o["kind"] == order["option"]), None)
        if not option:
            return
        first = order["legs"][0]
        pickup, delivery = first["pickup"]["placeId"], first["delivery"]["placeId"]
        note = self.note_of(req)
        near = sum(n for home, n in (self.homes or {}).items() if hav(P[pickup], P[home]) <= 30)
        self.booking_rows[order["id"]] = {
            "orderId": order["id"], "wonAt": at, "dealerId": order["dealerId"], "option": order["option"],
            "pickupPlaceId": pickup, "deliveryPlaceId": delivery,
            "roadKm": road(pickup, delivery)["km"], "roadMinutes": road(pickup, delivery)["minutes"],
            "lastPickupAt": order["lastPickupAt"], "leadMinutes": order["lastPickupAt"] - at,
            "deadline": order["deadline"], "slackMinutes": self.slack_of(req, option),
            "priceCents": order["priceCents"], "referenceFeeCents": req["referenceFeeCents"],
            "legs": len(order["legs"]), "private": first["pickup"]["privateAddress"] or first["delivery"]["privateAddress"],
            "premium": any(leg["car"]["premium"] for leg in order["legs"]),
            "electric": any(leg["car"]["electric"] for leg in order["legs"]),
            "nearbyDriverHomes": near, "backlogAtWin": self.backlog(pickup), "townPFailAtWin": self.p_fail(pickup),
            "manual": note["manual"], "plates": note["plates"], "summerTyres": note["summer"],
            "forecastRisk": self.snow_risk(req, option),
            "bookedAt": None, "bookedBeforeWarning": None, "warnedAt": None,
            "deliveredAt": None, "onTime": None, "cancelled": False,
        }

    def booking_training_rows(self) -> list:
        return list(self.booking_rows.values())

    def record_offer_row(self, desk, gig_id: str, info: dict, driver_id: str, driver_place: str,
                         pickup_place: str, app_fee: int, offer_fee: int) -> Optional[int]:
        """Store offer context before sending it; later events only fill in labels.

        We keep this separate from booking-risk rows because an offer is targeted at a particular
        driver, and its response is not a random sample of normal app bookings.
        """
        if not self.params["recordTelemetry"]:
            return None
        order_ids = list(info.get("orderIds") or [info["orderId"]])
        orders = [desk.orders[oid] for oid in order_ids if oid in desk.orders]
        driver = self.drivers.get(driver_id, {})
        outcomes = {
            order["id"]: {
                "deadline": order["deadline"], "warnedAt": None, "cancelled": False,
                "deliveredAt": None, "onTime": None,
            }
            for order in orders
        }
        row = {
            "offerId": len(self.offer_rows), "offeredAt": desk.now, "gigId": gig_id,
            "driverId": driver_id, "driverPlaceId": driver_place, "pickupPlaceId": pickup_place,
            "driverDistanceKm": hav(P[driver_place], P[pickup_place]), "orderIds": order_ids,
            "cars": info.get("cars", len(order_ids)),
            "leadMinutes": min((order["lastPickupAt"] - desk.now for order in orders), default=None),
            "appFeeCents": app_fee, "offerFeeCents": offer_fee,
            "feePremiumCents": offer_fee - app_fee,
            "feePremiumFraction": ((offer_fee / app_fee) - 1) if app_fee else None,
            "driverRating": driver.get("rating"), "driverCompletedGigs": driver.get("completedGigs"),
            "driverDrops": driver.get("drops"), "driverLicence": driver.get("licence"),
            "answer": None, "answeredAt": None, "gigBookedAt": None, "orderOutcomes": outcomes,
        }
        self.offer_rows.append(row)
        return row["offerId"]

    def offer_training_rows(self) -> list:
        return list(self.offer_rows)

    def label_offer_orders(self, order_id: str, **labels) -> None:
        """Apply later lifecycle labels to every previous offer concerning this order."""
        for row in self.offer_rows:
            outcome = row["orderOutcomes"].get(order_id)
            if outcome is not None:
                outcome.update(labels)

    def lane_offset(self, req: dict) -> float:
        """Log of how far the competitor's price on this lane sits above its world level."""
        if not self.lanes:
            return 0.0
        leg = req["legs"][0]
        return self.params["laneScale"] * (self.lanes["pickup"].get(leg["pickup"]["placeId"], 0.0)
                                           + self.lanes["delivery"].get(leg["delivery"]["placeId"], 0.0))

    def trust_bonus(self, desk, req: dict, opt: dict) -> float:
        """Future value, in cents, of one more on-time order for this dealer.

        One more counted order lifts the price the dealer accepts by 1% on every later order
        until the count would have reached 25 anyway.  Orders already under way count as banked;
        the dealer's share of requests so far sizes the business still to come this week.
        """
        scale = self.params["trustBonus"]
        if not scale:
            return 0.0
        dealer = req["dealerId"]
        under_way = sum(1 for oid in self.open_orders if (desk.orders.get(oid) or {}).get("dealerId") == dealer)
        headroom = 25 - self.trust.get(dealer, 0) - under_way
        if headroom <= 0:
            return 0.0
        share = (self.dealer_reqs.get(dealer, 0) + 1) / (sum(self.dealer_reqs.values()) + 30)
        days_left = max(0.0, (5 * DAY - opt["deadline"]) / DAY)
        future_wins = share * self.params["trustWeekWins"] / 5 * days_left
        return scale * 0.01 * 1.5 * opt["listPriceCents"] * min(headroom, future_wins)

    def quote(self, desk, req: dict) -> dict:
        p = self.params
        self.dealer_reqs[req["dealerId"]] = self.dealer_reqs.get(req["dealerId"], 0) + 1
        km = max(road(l["pickup"]["placeId"], l["delivery"]["placeId"])["km"] for l in req["legs"])
        priv = any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in req["legs"])

        def step(tab: list, v: float) -> float:
            return next(val for lim, val in tab if v < lim)

        def structural(o: dict) -> float:
            base = 1 - (1 - step(p["riskSlack"], self.slack_of(req, o))) * (1 - step(p["riskKm"], km))
            return 1 - (1 - base) * (1 - self.snow_risk(req, o))

        ok_opts = [o for o in req["options"] if km <= p["maxKm"] and self.slack_of(req, o) >= (p["minSlackPriv"] if priv else p["minSlack"])
                   and not (len(req["legs"]) > 1 and priv and not p["tradeinPriv"])]
        if len(ok_opts) > 1:   # offer a fast option only if it is not much riskier
            min_r = min(structural(o) for o in ok_opts)
            ok_opts = [o for o in ok_opts if structural(o) <= min_r + p["optRiskGap"]]
        prices = {o["kind"]: round_half_up(o["listPriceCents"] * p["declineMult"] / 100) * 100 for o in req["options"]}
        if not ok_opts:
            self.stats["declined"] += 1
            return prices
        t = 1 + 0.01 * self.trust.get(req["dealerId"], 0)
        ls = self.late_share()
        lam = self.shadow()
        ref = req["referenceFeeCents"]
        town_fail = max(self.p_fail(l["pickup"]["placeId"]) for l in req["legs"])
        lane = self.lane_offset(req)
        if not p["separateOptionPricing"]:
            # Pre-v5 control: one markup selected from the first eligible option and copied to
            # every option.  Keeping this exact control makes the option-price ablation useful.
            o = ok_opts[0]
            pf = 1 - (1 - town_fail) * (1 - max(structural(oo) for oo in ok_opts))
            if ls >= p["hardLate"] and pf > p["hardPFail"]:
                self.stats["braked"] = self.stats.get("braked", 0) + 1
                return prices
            xs = [math.log(u / t) - lane for u in self.grid]
            pw = self.comp.p_win(req["arrivedAt"], xs, req["dealerId"])
            f0 = p["f0"] + p["f0Risk"] * pf
            bonus = self.trust_bonus(desk, req, o)
            best, bu = -1.0, None
            for i, u in enumerate(self.grid):
                price = u * o["listPriceCents"]
                cost = p["costMult"] * f0 * ref + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                ev = pw[i] * (price - cost + bonus)
                if ev > best:
                    best, bu = ev, u
            if best <= p["minEv"] or bu is None:
                self.stats["evNeg"] += 1
                return prices
            x = math.log(bu / t) - lane
            for oo in ok_opts:
                prices[oo["kind"]] = round_half_up(oo["listPriceCents"] * bu / 100) * 100
            self.quoted[req["id"]] = {oo["kind"]: x for oo in ok_opts}
            self.stats["quoted"] += 1
            return prices
        learnt: dict = {}
        quoted_any = False
        for o in ok_opts:
            # Each delivery option has its own slack, snow exposure and dealer speed value.  The
            # effective log price is also the correct value to learn from when this option wins.
            pf = 1 - (1 - town_fail) * (1 - structural(o))
            if ls >= p["hardLate"] and pf > p["hardPFail"]:
                continue
            speed = p["speedValue"].get(o["kind"], 0.0)
            xs = [math.log(u / t) - speed - lane for u in self.grid]
            pw = self.comp.p_win(req["arrivedAt"], xs, req["dealerId"])
            f0 = p["f0"] + p["f0Risk"] * pf
            bonus = self.trust_bonus(desk, req, o)
            best, bu = -1.0, None
            for i, u in enumerate(self.grid):
                price = u * o["listPriceCents"]
                cost = p["costMult"] * f0 * ref + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                ev = pw[i] * (price - cost + bonus)
                if ev > best:
                    best, bu = ev, u
            if best > p["minEv"] and bu is not None:
                prices[o["kind"]] = round_half_up(o["listPriceCents"] * bu / 100) * 100
                learnt[o["kind"]] = math.log(bu / t) - speed - lane
                quoted_any = True
        if not quoted_any:
            if ls >= p["hardLate"]:
                self.stats["braked"] = self.stats.get("braked", 0) + 1
            self.stats["evNeg"] += 1
            return prices
        self.quoted[req["id"]] = learnt
        self.stats["quoted"] += 1
        return prices

    def on_event(self, desk, e: dict, replay: bool = False) -> None:
        self.last_now = max(self.last_now, e["at"])
        self.sweep_pending(e["at"])
        kind = e["kind"]
        if kind == "forecast":
            forecast = {"from": e["from"], "to": e["to"], "places": e["placeIds"]}
            if forecast not in self.snow:
                self.snow.append(forecast)
            # No later quote can be affected by a forecast that has already passed.
            self.snow = [f for f in self.snow if f["to"] > e["at"]]
        elif kind == "order_won":
            oid = e["order"]["id"]
            if oid in self.quoted:
                r = desk.requests.get(oid)
                options = self.quoted.pop(oid)
                # Saved state from the pre-option policy contains one float; retain its learning
                # signal on a rolling deploy instead of failing the event handler.
                x = options.get(e["order"]["option"]) if isinstance(options, dict) else options
                if x is not None:
                    self.comp.observe(r["arrivedAt"] if r else e["at"], x, True, e["order"]["dealerId"])
            self.record_booking_row(desk, e["order"], e["at"])
            self.order_town[oid] = e["order"]["legs"][0]["pickup"]["placeId"]
            self.open_orders.add(oid)
            self.pending[oid] = e["at"]
            self.unbooked[oid] = e["at"]
        elif kind == "order_lost":
            if e["requestId"] in self.quoted:
                r = desk.requests.get(e["requestId"])
                options = self.quoted.pop(e["requestId"])
                # A loss reveals that even the dealer's best-effective option did not win.  It
                # does not reveal which alternative the dealer compared, so use a discounted
                # update.  Wins, in contrast, always use the option actually chosen above.
                if options:
                    x = min(options.values()) if isinstance(options, dict) else options
                    self.comp.observe(r["arrivedAt"] if r else e["at"], x, False,
                                      r["dealerId"] if r else None,
                                      self.params["multiLossWeight"] if isinstance(options, dict) and len(options) > 1 else 1.0)
        elif kind == "gig_booked":
            info = desk.gig_info.get(e["gigId"])
            # This may be an ordinary app booking while an offer happens to be open.  Keep the
            # timestamp as context, but use offer_answered/accepted as the causal response label.
            for row in self.offer_rows:
                if row["gigId"] == e["gigId"] and row["gigBookedAt"] is None:
                    row["gigBookedAt"] = e["at"]
            for oid in (info.get("orderIds") or [info["orderId"]]) if info else []:
                row = self.booking_rows.get(oid)
                if row and row["bookedAt"] is None:
                    row["bookedAt"] = e["at"]
                    row["bookedBeforeWarning"] = row["warnedAt"] is None
                self.pending.pop(oid, None)
                self.unbooked.pop(oid, None)
                if oid not in self.open_warned:
                    self.resolve(oid, "ok")
        elif kind == "deadline_close":
            row = self.booking_rows.get(e["orderId"])
            if row and row["warnedAt"] is None:
                row["warnedAt"] = e["at"]
                if row["bookedBeforeWarning"] is None:
                    row["bookedBeforeWarning"] = False
            self.label_offer_orders(e["orderId"], warnedAt=e["at"])
            self.resolve(e["orderId"], "fail")
            self.pending.pop(e["orderId"], None)
            if e["orderId"] in self.open_orders:
                self.open_warned.add(e["orderId"])
        elif kind == "order_cancelled":
            row = self.booking_rows.get(e["orderId"])
            if row:
                row["cancelled"] = True
            self.label_offer_orders(e["orderId"], cancelled=True)
            self.open_orders.discard(e["orderId"])
            self.open_warned.discard(e["orderId"])
            self.pending.pop(e["orderId"], None)
            self.unbooked.pop(e["orderId"], None)
            self.bundle_pending.pop(e["orderId"], None)
        elif kind in ("gig_dropped", "no_show"):
            info = desk.gig_info.get(e["gigId"])
            for oid in (info.get("orderIds") or [info["orderId"]]) if info else []:
                if oid in self.open_orders:
                    self.unbooked[oid] = e["at"]
        elif kind == "offer_answered":
            open_offer = self.offered.get(e["gigId"], {}).get(e["driverId"])
            if isinstance(open_offer, dict) and open_offer.get("rowId") is not None:
                row_id = open_offer["rowId"]
                if row_id < len(self.offer_rows):
                    self.offer_rows[row_id].update({"answer": e["answer"], "answeredAt": e["at"]})
            if e["gigId"] in self.offered:
                self.offered[e["gigId"]].pop(e["driverId"], None)
                if not self.offered[e["gigId"]]:
                    self.offered.pop(e["gigId"], None)
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
                row = self.booking_rows.get(e["orderId"])
                if row:
                    row["deliveredAt"] = e["at"]
                    row["onTime"] = on_time
                self.label_offer_orders(e["orderId"], deliveredAt=e["at"], onTime=on_time)
                if not on_time:
                    self.late_orders += 1

    # ------------------------------------------------------------ short holding queue and chains
    def queue_order(self, desk, order: dict) -> bool:
        """Hold an ordinary one-car order just long enough to find a same-yard partner."""
        if not self.params["enableBundles"]:
            return False
        if len(order["legs"]) != 1:
            return False                         # preserve the special trade-in stop order
        req = desk.requests.get(order["id"])
        leg = order["legs"][0]
        if not req or leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"]:
            return False
        option = next((o for o in req["options"] if o["kind"] == order["option"]), req["options"][0])
        if self.note_of(req)["summer"] and self.snow_risk(req, option):
            return False
        if order["lastPickupAt"] - desk.now < self.params["bundleMinLead"]:
            return False
        self.bundle_pending[order["id"]] = desk.now
        return True

    @staticmethod
    def chain_minutes(items: list) -> int:
        minutes, previous = 0, None
        for order, leg in items:
            pickup = order["legs"][leg]["pickup"]["placeId"]
            delivery = order["legs"][leg]["delivery"]["placeId"]
            if previous is not None:
                minutes += road(previous, pickup)["minutes"]
            minutes += road(pickup, delivery)["minutes"]
            previous = delivery
        return minutes

    def best_chain(self, desk, order_ids: list) -> list:
        """Oldest-first nearest-neighbour chain, bounded by the 12-hour map-driving rule."""
        candidates = [desk.orders[oid] for oid in order_ids if oid in desk.orders and oid in self.bundle_pending
                      and not desk.gigs_of(oid) and desk.orders[oid]["deliveredAt"][0] is None]
        if len(candidates) < 2:
            return []
        candidates.sort(key=lambda o: self.bundle_pending[o["id"]])
        selected = [(candidates[0], 0)]
        remaining = candidates[1:]
        while remaining and len(selected) < self.params["bundleMaxCars"]:
            previous = selected[-1][0]["legs"][0]["delivery"]["placeId"]
            remaining.sort(key=lambda o: road(previous, o["legs"][0]["pickup"]["placeId"])["minutes"] +
                           road(o["legs"][0]["pickup"]["placeId"], o["legs"][0]["delivery"]["placeId"])["minutes"])
            nxt = remaining.pop(0)
            trial = selected + [(nxt, 0)]
            if self.chain_minutes(trial) <= 12 * 60:
                selected = trial
        return selected if len(selected) > 1 else []

    def flush_chains(self, desk) -> None:
        p = self.params
        for oid in list(self.bundle_pending):
            order = desk.orders.get(oid)
            if not order or desk.gigs_of(oid) or order["deliveredAt"][0] is not None:
                self.bundle_pending.pop(oid, None)
        by_yard: dict = {}
        for oid in self.bundle_pending:
            order = desk.orders.get(oid)
            if order:
                by_yard.setdefault(order["legs"][0]["pickup"]["placeId"], []).append(oid)
        for ids in by_yard.values():
            chain = self.best_chain(desk, ids)
            if chain and desk.post_bundle([(o, [leg]) for o, leg in chain]):
                for o, _ in chain:
                    self.bundle_pending.pop(o["id"], None)
        for oid, queued_at in list(self.bundle_pending.items()):
            if desk.now - queued_at >= p["bundleWait"]:
                order = desk.orders.get(oid)
                self.bundle_pending.pop(oid, None)
                if order and not desk.gigs_of(oid) and order["deliveredAt"][0] is None:
                    desk.post(order, [0])

    # ------------------------------------------------------------ direct offers to active nearby drivers
    def current_fee(self, desk, gig_id: str) -> int:
        post = desk.posts.get(gig_id)
        if not post:
            return 0
        return max((step["feeCents"] for step in post["schedule"] if step["at"] <= desk.now),
                   default=post["schedule"][0]["feeCents"])

    def can_take_gig(self, driver_id: str, audience: dict) -> bool:
        d = self.drivers.get(driver_id)
        if not d:
            return False
        if audience.get("manualLicence") and d["licence"] != "manual":
            return False
        return not audience.get("experienced") or (d["rating"] >= 4.8 and d["completedGigs"] >= 50)

    def send_targeted_offers(self, desk) -> None:
        if not self.params["enableOffers"]:
            return
        h = (desk.now % DAY) / 60
        if h < 7 or h >= 23:
            return
        due = []
        for oid in self.open_orders:
            order = desk.orders.get(oid)
            if not order or order["status"] != "waiting" or desk.now < order["lastPickupAt"] - self.params["offerLead"]:
                continue
            due.extend(g for g in desk.gigs_of(oid) if g not in desk.booked)
        due = list(dict.fromkeys(due))
        if not due:
            return
        if desk.now - self.last_position_refresh >= self.params["offerRefresh"]:
            try:
                desk.refresh_driver_positions()
                self.last_position_refresh = desk.now
            except Exception:
                return
        day = desk.now // DAY
        for gig_id in due:
            post = desk.posts.get(gig_id)
            info = desk.gig_info.get(gig_id)
            if not post or not info or len(self.offered.get(gig_id, {})) >= 3:
                continue
            pickups = [s for s in post["stops"] if s["kind"] == "pickup"]
            if not pickups:
                continue
            pickup = pickups[0]["orderId"]
            order = desk.orders.get(pickup)
            if not order:
                continue
            place = order["legs"][pickups[0]["leg"]]["pickup"]["placeId"]
            candidates = []
            for driver_id, where in desk.driver_positions.items():
                used = self.driver_offers.get(driver_id, {}).get(str(day), 0)
                if used >= 3 or driver_id in self.offered.get(gig_id, {}) or not self.can_take_gig(driver_id, post["audience"]):
                    continue
                distance = hav(P[where], P[place])
                if distance <= self.params["offerNearKm"]:
                    candidates.append((distance, driver_id, where))
            app_fee = self.current_fee(desk, gig_id)
            fee = max(round_half_up(app_fee * self.params["offerMult"]),
                      round_half_up(info["firstFee"] * self.params["offerRefMult"]))
            for _, driver_id, where in sorted(candidates)[:max(0, 3 - len(self.offered.get(gig_id, {})))]:
                desk.outbox.append({"type": "offer", "gigId": gig_id, "driverId": driver_id, "feeCents": fee})
                row_id = self.record_offer_row(desk, gig_id, info, driver_id, where, place, app_fee, fee)
                self.offered.setdefault(gig_id, {})[driver_id] = {"day": day, "rowId": row_id}
                usage = self.driver_offers.setdefault(driver_id, {})
                usage[str(day)] = usage.get(str(day), 0) + 1

    def tick(self, desk) -> None:
        self.last_now = max(self.last_now, desk.now)
        self.sweep_pending(desk.now)
        self.flush_chains(desk)
        self.send_targeted_offers(desk)

    def next_wake(self, desk) -> int:
        wakes = [when + self.params["bundleWait"] for when in self.bundle_pending.values()]
        for oid in self.open_orders:
            order = desk.orders.get(oid)
            if order and order["status"] == "waiting":
                wakes.append(order["lastPickupAt"] - self.params["offerLead"])
        return min(wakes) if wakes else INF

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
        items = desk.fee_items(desk.posts.get(e["gigId"], {}).get("stops", []))
        if not items or any(order["id"] in desk.urgent for order, _ in items):
            return
        p = self.params
        post = desk.posts.get(e["gigId"])
        cur = max([s["feeCents"] for s in (post["schedule"] if post else []) if s["at"] <= desk.now], default=0)
        normal = self.fees_bundle(desk, items, False) if len(items) > 1 else self.fees(desk, items[0][0], len(items[0][1]), False)
        f1 = max(cur * 1.15, normal[1]["feeCents"] if len(normal) > 1 else normal[0]["feeCents"])
        t2 = max(desk.now + 1, min(order["lastPickupAt"] for order, _ in items) - p["f2Lead"])
        f2 = max(f1, normal[-1]["feeCents"] if normal else f1)
        schedule = [{"at": desk.now, "feeCents": round_half_up(f1)}, {"at": t2, "feeCents": round_half_up(f2)}]
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

    def fees_bundle(self, desk, items: list, urgent: bool) -> list:
        """One lower fee schedule for a same-yard chain, with its earliest pickup driving rises."""
        if len(items) == 1:
            order, legs = items[0]
            return self.fees(desk, order, len(legs), urgent)
        p = self.params
        refs = [desk.requests[order["id"]]["referenceFeeCents"] * len(legs) / len(order["legs"])
                for order, legs in items]
        total = sum(refs)
        if urgent:
            return steep(desk.now, total, 8)
        town = items[0][0]["legs"][items[0][1][0]]["pickup"]["placeId"]
        f0 = p["f0"] + p["f0Risk"] * self.p_fail(town, False)
        # The first car bears the trip to the yard and home; each added car pays only its
        # measured marginal share.  Escalations retain that saving but still arrive early.
        discounted_ref = refs[0] + p["bundleExtraCost"] * sum(refs[1:])
        opening = discounted_ref * f0
        t1 = max(desk.now + 1, min(order["lastPickupAt"] for order, _ in items) - p["f1Lead"])
        t2 = max(t1 + 1, min(order["lastPickupAt"] for order, _ in items) - p["f2Lead"])
        return [{"at": desk.now, "feeCents": round_half_up(opening)},
                {"at": t1, "feeCents": round_half_up(max(opening, discounted_ref * p["f1"]))},
                {"at": t2, "feeCents": round_half_up(max(opening, discounted_ref * p["f2"]))}]
