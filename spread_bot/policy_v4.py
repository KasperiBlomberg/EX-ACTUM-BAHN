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
from .return_legs import is_return_leg, job, plan_pair


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
        "enableReturnPairs": False,
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
        # A car won before its due day and still unbooked at 13:00 on that day is late four times
        # in five; one booked by midnight almost never is.  Drivers plan a day the evening before
        # and early in the morning, so the fee rises then (minute of the day; fEve 0 = off).
        "eveAt": 1020, "fEve": 0.0, "mornAt": 360, "fMorn": 0.0,
        "costMult": 1.0,                     # fee / ref paid on a smooth booking
        "failRef": 1.0, "failPrice": 0.15,   # extra cost when a car gets no driver before the warning
        # pf is a ranking of risk, not a calibrated probability: it predicts 15-20% failures where
        # 1-3% happen.  riskPrice scales it in the quote's cost; 1.0 is the pre-15-Nov behaviour.
        "riskPrice": 1.0,
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
        # Return-leg pairs: an ordinary single car with at least pairMinLead + pairHold minutes
        # to its last pickup is held up to pairHold minutes for an order going back the other
        # way (both ends within pairKm).  The pair is one gig at pairFee x the two typical fees
        # (a round trip costs a driver about one one-way gig); unbooked after pairPostWait
        # minutes it is withdrawn and both cars are posted singly.  With pairQuote, a request
        # that completes a held order is quoted at its marginal fee, weighted by pairBookProb
        # and never under pairMinMarginal x its typical fee.  Off until its ablation clears.
        "enableReturnPairs": False, "pairQuote": True, "pairHold": 60, "pairPostWait": 60,
        "pairKm": 30, "pairMinLead": 540, "pairMaxWait": 90, "pairFee": 0.62,
        "pairBookProb": 0.7, "pairMinMarginal": 0.35,
        # Follow-on offers: 17% of our bookings go to a driver who already holds another gig of
        # ours ending near the pickup, at the full fee, though the second gig costs that driver
        # only the extra time.  When a driver holds a booking that ends within followKm of an
        # open car's pickup (or starts within followKm of its delivery), offer them that car
        # directly at followMult x its typical fee.  A no costs nothing: the gig stays in the app.
        "enableFollowOffers": False, "followKm": 30, "followMult": 0.65,
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
        # Why a request got no real quote, and how far the declined ones were from the gates.
        self.decline_reasons = {"maxKm": 0, "minSlack": 0, "privateSlack": 0, "privateTradeIn": 0,
                                "hardLateBrake": 0, "negativeEv": 0}
        self.decline_near: dict = {}     # band -> {n, listCents}: declined on one gate only
        self.pair_hold: dict = {}        # order id -> minute since it waits for a return leg
        self.pair_reserved: dict = {}    # held order id -> {requestId, until}: promised to a quote
        self.pair_quoted: dict = {}      # request id -> {partner, alone}: quoted at the marginal fee
        self.pairs: dict = {}            # "a|b" -> one telemetry row per posted pair
        self.pair_of: dict = {}          # order id -> key of its live pair
        self.event_at: Optional[int] = None   # minute of the event being handled, for tick
        self.driver_jobs: dict = {}      # booked gig id -> {driver, first, last}: where our drivers go
        self.follow_open: dict = {}      # "gig|driver" -> {fee, appFee}; kept after the answer
        self.follow_won: set = set()     # gigs booked through a follow-on offer
        self.follow_last = -1            # minute of the last follow-on pass
        self.follow_stats = {"sent": 0, "accepted": 0, "declined": 0, "expired": 0, "savingCents": 0, "droppedAfter": 0}
        self.pair_stats = {"held": 0, "holdReleased": 0, "candidates": 0, "feasible": 0, "posted": 0,
                           "postFailed": 0, "booked": 0, "dropped": 0, "dissolved": 0, "feeSavingCents": 0,
                           "quoteCandidates": 0, "quoteFeasible": 0, "quoteDiscounted": 0, "quoteWins": 0,
                           "quoteWinsOtherwiseDeclined": 0, "quoteDiscountCents": 0,
                           "rejects": {"driving_limit": 0, "windows": 0, "near_deadline": 0}}

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
                # Held orders are not saved: after a restart Desk.rejoin posts them singly.
                "pairs": self.pairs, "pairOf": self.pair_of, "pairStats": self.pair_stats,
                "declineReasons": self.decline_reasons, "declineNear": self.decline_near,
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
        self.pairs.update(st.get("pairs") or {})
        self.pair_of.update(st.get("pairOf") or {})
        self.pair_stats.update(st.get("pairStats") or {})
        self.decline_reasons.update(st.get("declineReasons") or {})
        self.decline_near.update(st.get("declineNear") or {})
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

    def note_decline(self, req: dict, km: float, priv: bool) -> None:
        """Count why no option passed the gates, and how near the request was to passing.

        The near-miss bands hold only requests that fail one gate, so a band's count is what a
        looser ``maxKm`` or ``minSlack`` would newly admit (private cars keep their own gate).
        """
        p = self.params
        slack = max(self.slack_of(req, o) for o in req["options"])
        tradein_priv = len(req["legs"]) > 1 and priv and not p["tradeinPriv"]
        slack_ok = slack >= (p["minSlackPriv"] if priv else p["minSlack"])
        if km > p["maxKm"]:
            reason = "maxKm"
        elif tradein_priv:
            reason = "privateTradeIn"
        elif priv and slack >= p["minSlack"]:
            reason = "privateSlack"
        else:
            reason = "minSlack"
        self.decline_reasons[reason] += 1
        band = None
        if reason == "maxKm" and slack_ok and not tradein_priv:
            band = next((f"km{lim}" for lim in (600, 650, 700) if km <= lim), "kmOver700")
        elif reason == "minSlack" and not priv:
            band = next((f"slack{lim}" for lim in (330, 300, 270, 240) if slack >= lim), "slackUnder240")
        if band:
            near = self.decline_near.setdefault(band, {"n": 0, "listCents": 0})
            near["n"] += 1
            near["listCents"] += max(o["listPriceCents"] for o in req["options"])

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
            self.note_decline(req, km, priv)
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
                self.decline_reasons["hardLateBrake"] += 1
                return prices
            xs = [math.log(u / t) - lane for u in self.grid]
            pw = self.comp.p_win(req["arrivedAt"], xs, req["dealerId"])
            f0 = p["f0"] + p["f0Risk"] * pf
            bonus = self.trust_bonus(desk, req, o)

            def best_markup(fee_cost: float):
                best, bu = -1.0, None
                for i, u in enumerate(self.grid):
                    price = u * o["listPriceCents"]
                    cost = fee_cost + p["riskPrice"] * pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                    ev = pw[i] * (price - cost + bonus)
                    if ev > best:
                        best, bu = ev, u
                return (best, bu) if best > p["minEv"] else (best, None)

            best, bu = best_markup(p["costMult"] * f0 * ref)
            # Only a request that completes a held, safely postable return leg is priced at its
            # marginal fee; everything else keeps the standalone cost above.
            pair = self.pair_quote(desk, req, ok_opts, f0)
            if pair:
                alone_u = bu
                best, bu = best_markup(p["costMult"] * pair["feeCost"])
                if bu is None or (alone_u is not None and bu >= alone_u):
                    bu, pair = alone_u, None
            if bu is None:
                self.stats["evNeg"] += 1
                self.decline_reasons["negativeEv"] += 1
                return prices
            x = math.log(bu / t) - lane
            for oo in ok_opts:
                prices[oo["kind"]] = round_half_up(oo["listPriceCents"] * bu / 100) * 100
            if pair:
                self.pair_stats["quoteDiscounted"] += 1
                self.pair_reserved[pair["partner"]] = {"requestId": req["id"], "until": req["respondBy"] + 1}
                self.pair_quoted[req["id"]] = {"partner": pair["partner"], "alone": {
                    oo["kind"]: None if alone_u is None else round_half_up(oo["listPriceCents"] * alone_u / 100) * 100
                    for oo in ok_opts}}
            self.quoted[req["id"]] = {oo["kind"]: x for oo in ok_opts}
            self.stats["quoted"] += 1
            return prices
        learnt: dict = {}
        quoted_any = False
        braked = 0
        for o in ok_opts:
            # Each delivery option has its own slack, snow exposure and dealer speed value.  The
            # effective log price is also the correct value to learn from when this option wins.
            pf = 1 - (1 - town_fail) * (1 - structural(o))
            if ls >= p["hardLate"] and pf > p["hardPFail"]:
                braked += 1
                continue
            speed = p["speedValue"].get(o["kind"], 0.0)
            xs = [math.log(u / t) - speed - lane for u in self.grid]
            pw = self.comp.p_win(req["arrivedAt"], xs, req["dealerId"])
            f0 = p["f0"] + p["f0Risk"] * pf
            bonus = self.trust_bonus(desk, req, o)
            best, bu = -1.0, None
            for i, u in enumerate(self.grid):
                price = u * o["listPriceCents"]
                cost = p["costMult"] * f0 * ref + p["riskPrice"] * pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
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
            self.decline_reasons["hardLateBrake" if braked == len(ok_opts) else "negativeEv"] += 1
            return prices
        self.quoted[req["id"]] = learnt
        self.stats["quoted"] += 1
        return prices

    def on_event(self, desk, e: dict, replay: bool = False) -> None:
        self.last_now = max(self.last_now, e["at"])
        self.sweep_pending(e["at"])
        kind = e["kind"]
        self.event_at = None if replay else e["at"]
        if self.pair_hold or self.pair_of or self.pair_reserved or self.pair_quoted:
            self.pair_event(desk, e)
        if self.params["enableFollowOffers"] and not replay:
            self.follow_event(desk, e)
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
                pair_row = self.pairs.get(self.pair_of.get(e["orderId"]))
                if pair_row and pair_row["state"] == "booked":
                    pair_row["onTime"][e["orderId"]] = on_time
                if not on_time:
                    self.late_orders += 1

    # ------------------------------------------------------------ short holding queue and chains
    def queue_order(self, desk, order: dict) -> bool:
        """Hold an ordinary one-car order just long enough to find a same-yard partner."""
        if self.params["enableReturnPairs"]:
            try:
                if self.queue_for_pair(desk, order):
                    return True
            except Exception:
                # Pairing is an optimisation only: on any error the car is posted as usual.
                self.pair_hold.pop(order["id"], None)
                if desk.gigs_of(order["id"]) or self.has_post(desk, order["id"]):
                    return True
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
        # Desk.handle ticks after every event of a step, with the clock already at the step's
        # end.  Later events of that step (a booking) are not handled yet, so there a hold is
        # judged at the event's own minute and no pair gig is withdrawn; the loop's tick after
        # the last event sees the settled state.
        at, self.event_at = self.event_at, None
        self.flush_pairs(desk, desk.now if at is None else min(at, desk.now), at is None)
        self.flush_chains(desk)
        self.send_targeted_offers(desk)
        if at is None:
            self.send_follow_offers(desk)

    def next_wake(self, desk) -> int:
        wakes = [when + self.params["bundleWait"] for when in self.bundle_pending.values()]
        for oid, held_at in self.pair_hold.items():
            reserved = self.pair_reserved.get(oid)
            wakes.append(max(held_at + self.params["pairHold"], reserved["until"] + 1 if reserved else 0, desk.now + 1))
        for row in self.pairs.values():
            if row["state"] == "posted":
                wakes.append(desk.now + 1 if row.get("dropped") else max(row["postedAt"] + self.params["pairPostWait"], desk.now + 1))
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
        key = next((k for k, row in self.pairs.items() if row["gigId"] == e["gigId"] and row["state"] == "booked"), None)
        if key:
            # The round trip did not hold its driver: back to two ordinary single-car gigs, at
            # the next settled tick (a later event of this step may show it booked again).
            row = self.pairs[key]
            self.pair_stats["dropped"] += 1
            self.pair_stats["feeSavingCents"] -= row["savingCents"] or 0
            row.update({"state": "posted", "dropped": True})
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
        rises = [(t1, p["f1"]), (t2, p["f2"])]
        due = order["lastPickupAt"] // DAY * DAY
        if p["fEve"] and due > desk.now // DAY * DAY:
            rises.append((max(desk.now + 90, due - DAY + p["eveAt"]), p["fEve"]))
        if p["fMorn"] and due + p["mornAt"] > desk.now + 90:
            rises.append((due + p["mornAt"], p["fMorn"]))
        mult, last = f0, desk.now
        for at, m in sorted(rises):
            if at >= order["lastPickupAt"]:
                continue
            at, mult = max(at, last + 1), max(mult, m)   # times strictly rise, fees never fall
            steps.append({"at": at, "feeCents": round_half_up(ref * mult)})
            last = at
        return steps

    def fees_bundle(self, desk, items: list, urgent: bool) -> list:
        """One lower fee schedule for a same-yard chain, with its earliest pickup driving rises."""
        if len(items) == 1:
            order, legs = items[0]
            return self.fees(desk, order, len(legs), urgent)
        p = self.params
        key = self.pair_of.get(items[0][0]["id"])
        if len(items) == 2 and key and key == self.pair_of.get(items[1][0]["id"]):
            return self.pair_fees(desk, items, self.pairs[key], urgent)
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

    # ------------------------------------------------------------ return-leg pairs (A->B with B->A)
    def single_opening(self, desk, order: dict) -> float:
        """The opening fee ``fees`` gives this one-car order posted alone."""
        p = self.params
        pf = self.p_fail(order["legs"][0]["pickup"]["placeId"], False)
        return desk.requests[order["id"]]["referenceFeeCents"] * (p["f0"] + p["f0Risk"] * pf)

    def pair_fees(self, desk, items: list, row: dict, urgent: bool) -> list:
        """Round-trip fee: pairFee x the two typical fees, at the two-gig level before the warning."""
        p = self.params
        total = sum(desk.requests[order["id"]]["referenceFeeCents"] for order, _ in items)
        if urgent:
            return steep(desk.now, total, 8)
        single = sum(self.single_opening(desk, order) for order, _ in items)
        opening = min(p["pairFee"] * total, single)
        # The pair's own last start can be earlier than either order's lastPickupAt, so both
        # rises come before the earliest warning (3 hours before lastPickupAt).
        last = min([row["latest"]] + [order["lastPickupAt"] for order, _ in items])
        t1 = max(desk.now + 1, last - p["f1Lead"])
        t2 = max(t1 + 1, last - p["f2Lead"])
        return [{"at": desk.now, "feeCents": round_half_up(opening)},
                {"at": t1, "feeCents": round_half_up(max(opening, single))},
                {"at": t2, "feeCents": round_half_up(max(opening, single, total * p["f1"]))}]

    def pair_ordinary(self, req: Optional[dict], legs: list, at: int, until: int) -> bool:
        """Only a plain dealer-to-dealer car is paired.

        A private address, a premium car (experienced drivers only), a low battery (charging
        stop), a manual gearbox, missing plates, summer tyres, any time the note states, and snow
        forecast at either end all keep the car in the existing single-gig path, where those
        constraints are already handled.
        """
        if not req or len(legs) != 1:
            return False
        leg = legs[0]
        car = leg["car"]
        if leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"] or car["premium"]:
            return False
        if car["electric"] and car.get("chargePercent") is not None and car["chargePercent"] < 30:
            return False
        f = self.note_of(req)
        if f["manual"] or f["plates"] or f["summer"]:
            return False
        if any(f[k] is not None for k in ("ready_at", "release_at", "deadline_at", "pickup_open", "pickup_close", "delivery_close")):
            return False
        places = {leg["pickup"]["placeId"], leg["delivery"]["placeId"]}
        return not any(places.intersection(w["places"]) and w["from"] < until and at < w["to"] for w in self.snow)

    def order_job(self, desk, order: dict) -> dict:
        leg = order["legs"][0]
        return job(order["id"], leg, leg["pickup"]["window"]["from"], order["lastPickupAt"],
                   desk.requests[order["id"]]["referenceFeeCents"])

    def plan(self, a: dict, b: dict, at: int, extra_lead: int = 0):
        p = self.params
        return plan_pair(a, b, at, p["pairKm"], p["pairMinLead"] + extra_lead, p["pairMaxWait"])

    @staticmethod
    def has_post(desk, order_id: str) -> bool:
        return any(a["type"] == "post" and any(s["orderId"] == order_id for s in a["stops"]) for a in desk.outbox)

    def held_orders(self, desk, for_request: Optional[str] = None):
        """Held orders that are still unposted and not promised to another open quote."""
        for oid in list(self.pair_hold):
            order = desk.orders.get(oid)
            reserved = self.pair_reserved.get(oid)
            if reserved and reserved["requestId"] != for_request and reserved["until"] >= desk.now:
                continue
            if order and not desk.gigs_of(oid) and not self.has_post(desk, oid) and order["deliveredAt"][0] is None:
                yield order

    def queue_for_pair(self, desk, order: dict) -> bool:
        """On a win: pair the order with a held return leg, or hold it for one.  False = post as usual."""
        p, oid = self.params, order["id"]
        quoted = self.pair_quoted.pop(oid, None)
        if quoted:
            self.pair_stats["quoteWins"] += 1
            alone = quoted["alone"].get(order["option"])
            if alone is None:
                self.pair_stats["quoteWinsOtherwiseDeclined"] += 1
            else:
                self.pair_stats["quoteDiscountCents"] += alone - order["priceCents"]
        req = desk.requests.get(oid)
        if not self.pair_ordinary(req, order["legs"], desk.now, order["deadline"]):
            return False
        mine = self.order_job(desk, order)
        best = None
        for other in self.held_orders(desk, oid):
            theirs = self.order_job(desk, other)
            if not is_return_leg(mine, theirs, p["pairKm"]):
                continue
            self.pair_stats["candidates"] += 1
            plan, reason = self.plan(mine, theirs, desk.now)
            if not plan:
                self.pair_stats["rejects"][reason] += 1
                continue
            self.pair_stats["feasible"] += 1
            if best is None or plan["latest"] > best[0]["latest"]:
                best = (plan, other)
        if best and self.post_pair(desk, order, best[1], best[0], bool(quoted and quoted["partner"] == best[1]["id"])):
            return True
        if best or order["lastPickupAt"] - desk.now < p["pairMinLead"] + p["pairHold"]:
            return False        # a failed pair post, or too little lead to wait: post it now
        self.pair_hold[oid] = desk.now
        self.pair_stats["held"] += 1
        return True

    def post_pair(self, desk, a: dict, b: dict, plan: dict, via_quote: bool) -> bool:
        """Post the round trip as one gig through Desk.post_bundle; False leaves both orders as they were."""
        first, second = (a, b) if plan["first"] == a["id"] else (b, a)
        key = first["id"] + "|" + second["id"]
        total = sum(desk.requests[o["id"]]["referenceFeeCents"] for o in (first, second))
        self.pairs[key] = {
            "orders": [first["id"], second["id"]], "postedAt": desk.now, "latest": plan["latest"],
            "driveMinutes": plan["driveMinutes"], "viaQuote": via_quote, "typicalFeeCents": total,
            "singleFeeCents": round_half_up(sum(self.single_opening(desk, o) for o in (first, second))),
            "state": "posted", "gigId": None, "bookedAt": None, "feeCents": None, "savingCents": None,
            "endedAt": None, "reason": None, "onTime": {},
        }
        self.pair_of[first["id"]] = self.pair_of[second["id"]] = key
        try:
            posted = desk.post_bundle([(first, [0]), (second, [0])])
        except Exception:
            posted = False
        if not posted:
            del self.pairs[key]
            self.pair_of.pop(first["id"], None)
            self.pair_of.pop(second["id"], None)
            self.pair_stats["postFailed"] += 1
            return False
        for o in (first, second):
            self.pair_hold.pop(o["id"], None)
            self.pair_reserved.pop(o["id"], None)
        self.pair_stats["posted"] += 1
        return True

    def end_pair(self, key: str, at: int, reason: str) -> None:
        """The pair is over as a pair: its cars are, or are about to be, ordinary single gigs."""
        row = self.pairs[key]
        row.update({"state": "dissolved", "endedAt": at, "reason": reason})
        self.pair_stats["dissolved"] += 1
        for oid in row["orders"]:
            if self.pair_of.get(oid) == key:
                del self.pair_of[oid]
            if oid in self.pending:
                self.pending[oid] = at      # the town's booking record starts at the single post

    def dissolve_pair(self, desk, key: str, reason: str, at: int, withdraw: bool = True) -> None:
        """Back to the individual gigs, with the usual single-car fees.

        ``withdraw`` takes the open pair gig out of the app first.  Without it the pair gig is
        already gone (refused, expired or changed), and only a car left without a gig is posted.
        """
        row = self.pairs[key]
        if row["gigId"] in desk.booked:
            return
        self.end_pair(key, at, reason)
        orders = [desk.orders[oid] for oid in row["orders"] if oid in desk.orders]
        try:
            for order in orders:
                if withdraw and row["gigId"] in desk.gigs_of(order["id"]):
                    desk.withdraw(order["id"])     # also reposts the other car of the gig
                    break
        finally:
            for order in orders:
                if order["deliveredAt"][0] is None and not desk.gigs_of(order["id"]) and not self.has_post(desk, order["id"]):
                    desk.post(order, [0])

    def release_hold(self, desk, order_id: str, at: int) -> None:
        self.pair_hold.pop(order_id, None)
        self.pair_reserved.pop(order_id, None)
        order = desk.orders.get(order_id)
        if not order or desk.gigs_of(order_id) or self.has_post(desk, order_id) or order["deliveredAt"][0] is not None:
            return
        if order_id in self.pending:
            self.pending[order_id] = at
        self.pair_stats["holdReleased"] += 1
        desk.post(order, [0])

    def flush_pairs(self, desk, now: int, settled: bool = True) -> None:
        """Release holds that ran out, confirm posted pairs, and dissolve pairs nobody booked.

        ``settled`` is False while later events of the same step are still unhandled.
        """
        p = self.params
        for oid, reserved in list(self.pair_reserved.items()):
            if reserved["until"] < now:
                del self.pair_reserved[oid]
        for oid, held_at in list(self.pair_hold.items()):
            order = desk.orders.get(oid)
            reserved = self.pair_reserved.get(oid)
            waiting_for_quote = bool(reserved and reserved["until"] >= now)
            if (not order or (now - held_at >= p["pairHold"] and not waiting_for_quote)
                    or order["lastPickupAt"] - now < p["pairMinLead"]):
                self.release_hold(desk, oid, now)
        for key, row in list(self.pairs.items()):
            if row["state"] != "posted":
                continue
            gigs = [desk.leg_gigs.get(oid, {}).get(0) for oid in row["orders"]]
            if row["gigId"] is None:
                if gigs[0] and gigs[0] == gigs[1]:
                    row["gigId"] = gigs[0]
                elif all(self.has_post(desk, oid) for oid in row["orders"]):
                    continue                              # the post has not been sent yet
                else:
                    # The world refused the pair and Desk already fell back to single gigs.
                    self.dissolve_pair(desk, key, "refused", now, withdraw=False)
                    continue
            elif gigs[0] != row["gigId"] or gigs[1] != row["gigId"]:
                self.dissolve_pair(desk, key, "changed", now, withdraw=False)
                continue
            if settled and row["gigId"] not in desk.booked and (
                    row.get("dropped") or now - row["postedAt"] >= p["pairPostWait"]
                    or row["latest"] - now < p["pairMinLead"] - p["pairPostWait"]):
                self.dissolve_pair(desk, key, "dropped" if row.get("dropped") else "unbooked", now)

    def pair_event(self, desk, e: dict) -> None:
        """Keep holds, promises and pair rows in step with the world's events."""
        kind = e["kind"]
        if kind == "order_lost":
            self.pair_quoted.pop(e["requestId"], None)
            for oid, reserved in list(self.pair_reserved.items()):
                if reserved["requestId"] == e["requestId"]:
                    del self.pair_reserved[oid]
        elif kind in ("order_changed", "order_cancelled"):
            oid = e["order"]["id"] if kind == "order_changed" else e["orderId"]
            # Desk posts a changed order itself, and reposts the other car of an open gig.
            self.pair_hold.pop(oid, None)
            self.pair_reserved.pop(oid, None)
            key = self.pair_of.get(oid)
            if key and self.pairs[key]["state"] == "posted":
                self.end_pair(key, e["at"], "changed" if kind == "order_changed" else "cancelled")
        elif kind == "gig_expired":
            key = next((k for k, row in self.pairs.items() if row["gigId"] == e["gigId"] and row["state"] == "posted"), None)
            if key:
                self.end_pair(key, e["at"], "expired")      # Desk.repost splits it into single gigs
        elif kind == "gig_booked":
            info = desk.gig_info.get(e["gigId"]) or {}
            ids = info.get("orderIds") or []
            key = self.pair_of.get(ids[0]) if len(ids) == 2 else None
            row = self.pairs.get(key) if key and key == self.pair_of.get(ids[1]) else None
            if row and row["state"] == "posted":
                schedule = desk.posts[e["gigId"]]["schedule"]
                fee = max((s["feeCents"] for s in schedule if s["at"] <= e["at"]), default=schedule[0]["feeCents"])
                row.update({"state": "booked", "gigId": e["gigId"], "bookedAt": e["at"], "feeCents": fee,
                            "savingCents": row["singleFeeCents"] - fee})
                self.pair_stats["booked"] += 1
                self.pair_stats["feeSavingCents"] += row["savingCents"]

    def pair_quote(self, desk, req: dict, ok_opts: list, f0: float) -> Optional[dict]:
        """Marginal fee cost of a request that completes a held return leg, else None.

        The pair must be postable at the response deadline whichever eligible option the dealer
        takes, with half an hour of lead beyond what posting needs; the held order is then
        promised to this request until the dealer decides.  The cost is the pair fee less what
        the held car would have cost alone, floored at pairMinMarginal x the typical fee, and
        weighted with the standalone fee for the pairs that find no driver.
        """
        p = self.params
        if not (p["enableReturnPairs"] and p["pairQuote"] and self.pair_hold and desk is not None):
            return None
        at, leg, ref = req["respondBy"], req["legs"][0], req["referenceFeeCents"]
        if not self.pair_ordinary(req, req["legs"], at, max(o["deadline"] for o in ok_opts)):
            return None
        drive = road(leg["pickup"]["placeId"], leg["delivery"]["placeId"])["minutes"]
        jobs = [job(req["id"], leg, max(at, leg["pickup"]["window"]["from"]),
                    eff_deadline(req, o["deadline"]) - drive - 30, ref) for o in ok_opts]
        best = None
        for other in self.held_orders(desk, req["id"]):
            theirs = self.order_job(desk, other)
            if not is_return_leg(jobs[0], theirs, p["pairKm"]):
                continue
            self.pair_stats["quoteCandidates"] += 1
            plans = [self.plan(mine, theirs, at, 30)[0] for mine in jobs]
            if not all(plans):
                continue
            self.pair_stats["quoteFeasible"] += 1
            latest = min(plan["latest"] for plan in plans)
            if best is None or latest > best[0]:
                best = (latest, other)
        if not best:
            return None
        other = best[1]
        other_ref = desk.requests[other["id"]]["referenceFeeCents"]
        marginal = max(p["pairFee"] * (other_ref + ref) - self.single_opening(desk, other), p["pairMinMarginal"] * ref)
        fee_cost = p["pairBookProb"] * marginal + (1 - p["pairBookProb"]) * f0 * ref
        return {"partner": other["id"], "feeCost": fee_cost} if fee_cost < f0 * ref else None

    # ------------------------------------------------------------ follow-on offers to our own drivers
    def follow_event(self, desk, e: dict) -> None:
        kind = e["kind"]
        if kind == "gig_booked":
            stops = (desk.posts.get(e["gigId"]) or {}).get("stops") or []
            places = [desk.orders[s["orderId"]]["legs"][s["leg"]][s["kind"]]["placeId"] for s in stops if s["orderId"] in desk.orders]
            if places and e.get("driverId"):
                self.driver_jobs[e["gigId"]] = {"driver": e["driverId"], "first": places[0], "last": places[-1]}
        elif kind in ("gig_dropped", "no_show"):
            self.driver_jobs.pop(e["gigId"], None)
            if e["gigId"] in self.follow_won:
                self.follow_won.discard(e["gigId"])
                self.follow_stats["droppedAfter"] += 1
        elif kind == "offer_answered":
            sent = self.follow_open.get(e["gigId"] + "|" + e["driverId"])
            if sent and "answer" not in sent:
                sent["answer"] = e["answer"]
                self.follow_stats[e["answer"]] = self.follow_stats.get(e["answer"], 0) + 1
                if e["answer"] == "accepted":
                    self.follow_stats["savingCents"] += sent["appFee"] - sent["fee"]
                    self.follow_won.add(e["gigId"])

    def send_follow_offers(self, desk) -> None:
        """Offer each open single car, below its app fee, to drivers whose booked gig of ours meets it."""
        p = self.params
        if not p["enableFollowOffers"] or desk.now == self.follow_last or not self.driver_jobs:
            return
        if not 7 * 60 <= desk.now % DAY < 22 * 60 + 45:      # no driver reads an offer at night
            return
        self.follow_last = desk.now
        for gig_id, held in list(self.driver_jobs.items()):
            stops = (desk.posts.get(gig_id) or {}).get("stops") or []
            if gig_id not in desk.booked or all(
                    s["orderId"] not in desk.orders or desk.orders[s["orderId"]]["deliveredAt"][s["leg"]] is not None
                    for s in stops if s["kind"] == "delivery"):
                del self.driver_jobs[gig_id]
        day = desk.now // DAY
        for oid in self.open_orders:
            order = desk.orders.get(oid)
            if not order or order["status"] != "waiting" or len(order["legs"]) != 1 or oid in desk.urgent or oid in self.pair_of:
                continue
            gigs = desk.gigs_of(oid)
            if len(gigs) != 1 or gigs[0] in desk.booked:
                continue
            gig_id = gigs[0]
            post, info = desk.posts.get(gig_id), desk.gig_info.get(gig_id)
            if not post or not info or info.get("cars") != 1:
                continue
            app_fee = self.current_fee(desk, gig_id)
            fee = round_half_up(p["followMult"] * desk.requests[oid]["referenceFeeCents"])
            room = 3 - len(self.offered.get(gig_id, {}))
            if fee >= app_fee or room <= 0:
                continue
            pickup, delivery = order["legs"][0]["pickup"]["placeId"], order["legs"][0]["delivery"]["placeId"]
            candidates = {}
            for held in self.driver_jobs.values():
                driver_id = held["driver"]
                key = gig_id + "|" + driver_id
                if key in self.follow_open or self.driver_offers.get(driver_id, {}).get(str(day), 0) >= 3:
                    continue
                if not self.can_take_gig(driver_id, post["audience"]):
                    continue
                after = hav(P[held["last"]], P[pickup]) <= p["followKm"]       # this car continues their trip
                before = hav(P[delivery], P[held["first"]]) <= p["followKm"]   # this car brings them to it
                if after or before:
                    rank = (0 if after and before else 1, driver_id)
                    if driver_id not in candidates or rank < candidates[driver_id][0]:
                        candidates[driver_id] = (rank, held["last"])
            for driver_id, (_, where) in sorted(candidates.items(), key=lambda item: item[1][0])[:room]:
                desk.outbox.append({"type": "offer", "gigId": gig_id, "driverId": driver_id, "feeCents": fee})
                row_id = self.record_offer_row(desk, gig_id, info, driver_id, where, pickup, app_fee, fee)
                self.offered.setdefault(gig_id, {})[driver_id] = {"day": day, "rowId": row_id}
                usage = self.driver_offers.setdefault(driver_id, {})
                usage[str(day)] = usage.get(str(day), 0) + 1
                self.follow_open[gig_id + "|" + driver_id] = {"fee": fee, "appFee": app_fee}
                self.follow_stats["sent"] += 1
