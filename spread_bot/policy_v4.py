"""v4 policy (port of src/policy_v4.js) = v2 + per-pickup-town booking success (learnt live, prior from
the roster) priced into quotes and opening fees + a global lateness shadow price + earlier two-step
escalation. This is the policy the bot plays."""
from __future__ import annotations

import math
from typing import Optional

from .desk import round_half_up, steep
from .geo import P, hav, road
from .notes import parse_note
from .booking_model import PartialPoolingLogistic
from .policy_v2 import CompModel, eff_deadline, leg_minutes, make_grid


class PolicyV4:
<<<<<<< Updated upstream
=======
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
        # The delivery minimum is a hard constraint.  Level-8 driver scarcity made the former
        # unconstrained baseline win work it could not serve, so every normal/ranking run now
        # reserves a deliberately small share of daily capacity before quoting.
        "enableCapacityReservations": True,
        "capacityGlobalPerDriver": 0.20,
        "capacityLocalPerDriver": 0.25,
        "capacityMinGlobal": 4,
        "capacityMinLocal": 1,
        "enableCalibratedOnTimeFees": False,
    }
>>>>>>> Stashed changes
    DEFAULTS = {
        "maxKm": 450, "minSlack": 360, "minSlackPriv": 600, "tradeinPriv": False,
        "f0": 0.92, "f0Risk": 0.30,          # opening fee = (f0 + f0Risk * pFail_town) * ref
        # A fitted model must be supplied with a held-out calibration result.  This keeps a
        # one-world online fit from silently steering live fee decisions.
        "calibratedRiskModel": None, "calibratedRiskValidation": None,
        "riskModelMinFolds": 2, "riskFeeGrid": (0.78, 0.88, 0.98, 1.08, 1.18, 1.30),
        "riskPenaltyRef": 1.0,
        "f1": 1.25, "f1Lead": 420, "f2": 1.5, "f2Lead": 270,
        "costMult": 1.0,                     # legacy lower-bound fee / ref on a smooth booking
        # Quote revenue must cover the fee required to attract a driver, not merely the opening
        # app fee.  Level 8 shortages often need the later fee steps or a repost after a drop.
        # The delivery-home term captures the costly return leg on lanes such as Vantaa -> Lahti.
        "driverCostFloor": 1.35, "driverReturnRisk": 0.55, "driverKmRisk": 0.0015,
        "driverRepostRisk": 0.30, "driverMarginFloor": 1.12,
        "failRef": 1.0, "failPrice": 0.15,   # extra cost when a car gets no driver before the warning
        "priorW": 4,
        "targetLate": 0.06, "shadowK": 25,
        # unbooked for 4 h counts as a failure; own unbooked gigs in a town raise its risk
<<<<<<< Updated upstream
        "earlyFail": 240, "backlogB": 10, "warnW": 0.4, "pendW": 0.15, "hardLate": 0.075, "hardPFail": 0.15,
        # structural risk by slack and distance
        "riskSlack": [[480, 0.20], [720, 0.07], [1e9, 0.02]], "riskKm": [[200, 0], [300, 0.04], [1e9, 0.14]], "optRiskGap": 0.08,
        "fastPremium": 0.0,                # tune same-day premium separately; v-9 preferred no premium
=======
        "earlyFail": 240, "backlogB": 12, "warnW": 0.4, "pendW": 0.15, "hardLate": 0.075, "hardPFail": 0.15,
        # Quote-time capacity is kept separate from booking failure risk.  An accepted quote
        # reserves its cars until the dealer answers, so a burst of independent requests cannot
        # overfill a town before any resulting orders appear in ``pending``.
        "capacityGlobalPerDriver": 0.70, "capacityLocalPerDriver": 0.75,
        "capacityLocalRadiusKm": 55, "capacityMinGlobal": 12, "capacityMinLocal": 2,
        "capacityLateScale": 0.55,
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
        "enableOffers": True, "enableSnow": True, "enableCapacityReservations": True,
        "enableCalibratedOnTimeFees": True, "recordTelemetry": False,
>>>>>>> Stashed changes
        "uMin": 1.0, "uMax": 2.3, "minEv": 0, "k": 11, "war": 0.15, "daySd": 0.12, "declineMult": 5,
    }

    def __init__(self, params: Optional[dict] = None):
        p = self.params = {**self.DEFAULTS, **(params or {})}
<<<<<<< Updated upstream
        self.comp = CompModel(k=p["k"], war=p["war"], daySd=p["daySd"])
=======
        dealer = {} if p["enableDealerEffects"] else {"dealerEta": 0.0, "dealerCarry": 0.0, "dealerReact": 0.0, "dealerCap": 0.0}
        self.comp = CompModel(k=p["k"], war=p["war"], daySd=p["daySd"], **dealer)
        self.risk_model = self.load_calibrated_risk_model()
>>>>>>> Stashed changes
        self.trust: dict = {}        # dealer -> on-time count (0..25)
        self.quoted: dict = {}       # request id -> option -> x (legacy states may hold one float)
        self.town: dict = {}         # pickup place -> {ok, fail}
        self.order_town: dict = {}   # order -> pickup place
        self.quote_capacity: dict = {}  # request -> {day, towns, cars}; live until won/lost
        self.order_capacity: dict = {}  # order -> {day, towns, cars}; live until delivered/cancelled
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
    def load_calibrated_risk_model(self) -> Optional[PartialPoolingLogistic]:
        """Load a vetted held-out model, otherwise retain the established town heuristic."""
        p = self.params
        validation = p.get("calibratedRiskValidation") or {}
        model_brier = ((validation.get("model") or {}).get("brier"))
        heuristic_brier = ((validation.get("heuristic") or {}).get("brier"))
        if (not p.get("enableCalibratedOnTimeFees") or not p.get("calibratedRiskModel")
                or validation.get("folds", 0) < p["riskModelMinFolds"]
                or model_brier is None or heuristic_brier is None or model_brier > heuristic_brier):
            return None
        try:
            return PartialPoolingLogistic.from_state(p["calibratedRiskModel"])
        except (TypeError, ValueError):
            return None

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

    # ------------------------------------------------------------ quote-time capacity reservations
    def capacity_item(self, req: dict) -> dict:
        """One conservative daily reservation for a request's cars.

        Options share one order if won, hence one reservation rather than one per delivery
        option.  The request's first feasible pickup day is the admission day; a later option
        does not create extra capacity merely because it moves the deadline.
        """
        note = self.note_of(req)
        earliest = max(req["arrivedAt"], req["legs"][0]["pickup"]["window"]["from"],
                       note["ready_at"] or 0, note["release_at"] or 0)
        return {"day": earliest // DAY,
                "towns": tuple(sorted({leg["pickup"]["placeId"] for leg in req["legs"]})),
                "cars": len(req["legs"])}

    def capacity_used(self, day: int, town: Optional[str] = None) -> int:
        items = list(self.quote_capacity.values()) + list(self.order_capacity.values())
        return sum(item["cars"] for item in items
                   if item["day"] == day and (town is None or town in item["towns"]))

    def capacity_limit(self, town: Optional[str] = None) -> int:
        """Roster-home-derived throughput limit, tightened by current lateness pressure."""
        p = self.params
        if self.homes is None:
            return 10**9
        if town is None:
            drivers = sum(self.homes.values())
            base = max(p["capacityMinGlobal"], math.floor(drivers * p["capacityGlobalPerDriver"]))
        else:
            drivers = sum(count for home, count in self.homes.items()
                          if hav(P[town], P[home]) <= p["capacityLocalRadiusKm"])
            base = max(p["capacityMinLocal"], math.floor(drivers * p["capacityLocalPerDriver"]))
        excess = max(0.0, self.late_share() - p["targetLate"])
        scale = max(p["capacityLateScale"], 1.0 - excess * 10)
        return max(1, math.floor(base * scale))

    def capacity_allows(self, req: dict) -> bool:
        if not self.params["enableCapacityReservations"]:
            return True
        item = self.capacity_item(req)
        if self.capacity_used(item["day"]) + item["cars"] > self.capacity_limit():
            return False
        return all(self.capacity_used(item["day"], town) + item["cars"] <= self.capacity_limit(town)
                   for town in item["towns"])

    def reserve_quote_capacity(self, req: dict) -> None:
        if self.params["enableCapacityReservations"]:
            self.quote_capacity[req["id"]] = self.capacity_item(req)

    def promote_quote_capacity(self, order: dict, req: Optional[dict]) -> None:
        if not self.params["enableCapacityReservations"]:
            return
        item = self.quote_capacity.pop(order["id"], None)
        if item is None and req is not None:
            item = self.capacity_item(req)
        if item is not None:
            self.order_capacity[order["id"]] = item

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
                "quoteCapacity": self.quote_capacity, "orderCapacity": self.order_capacity,
                "resolved": self.resolved, "pending": self.pending, "unbooked": self.unbooked, "lastNow": self.last_now,
                "lateOrders": self.late_orders, "doneOrders": self.done_orders, "openWarned": list(self.open_warned), "openOrders": list(self.open_orders)}

    def set_state(self, st: dict) -> None:
        self.comp.set_state(st["comp"])
        self.trust.update(st["trust"])
        self.quoted.update(st["quoted"])
        self.town.update(st["town"])
        self.order_town.update(st["orderTown"])
        self.quote_capacity.update(st.get("quoteCapacity") or {})
        self.order_capacity.update(st.get("orderCapacity") or {})
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
<<<<<<< Updated upstream
=======
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
            "tradeIn": len(order["legs"]) > 1, "waitingMinutes": 0,
            "forecastRisk": self.snow_risk(req, option),
            "bookedAt": None, "bookedBeforeWarning": None, "warnedAt": None,
            "deliveredAt": None, "onTime": None, "cancelled": False,
        }

    def booking_training_rows(self) -> list:
        return list(self.booking_rows.values())

    def fee_risk_row(self, desk, order: dict, cars: int, fee: float) -> Optional[dict]:
        """Features known when pricing the opening app fee, never later outcome labels."""
        req = desk.requests.get(order["id"])
        if not req:
            return None
        option = next((item for item in req["options"] if item["kind"] == order["option"]), None)
        if not option:
            return None
        first = order["legs"][0]
        pickup, delivery = first["pickup"]["placeId"], first["delivery"]["placeId"]
        note = self.note_of(req)
        nearby = sum(count for home, count in (self.homes or {}).items() if hav(P[pickup], P[home]) <= 30)
        ref = req["referenceFeeCents"] * cars / len(order["legs"])
        return {
            "pickupPlaceId": pickup, "deliveryPlaceId": delivery, "dealerId": order["dealerId"],
            "option": order["option"], "roadKm": road(pickup, delivery)["km"],
            "slackMinutes": self.slack_of(req, option), "leadMinutes": order["lastPickupAt"] - desk.now,
            "priceCents": fee, "referenceFeeCents": ref, "backlogAtWin": self.backlog(pickup),
            "nearbyDriverHomes": nearby, "forecastRisk": self.snow_risk(req, option), "legs": cars,
            "private": first["pickup"]["privateAddress"] or first["delivery"]["privateAddress"],
            "premium": any(leg["car"]["premium"] for leg in order["legs"]),
            "electric": any(leg["car"]["electric"] for leg in order["legs"]),
            "manual": note["manual"], "plates": note["plates"], "summerTyres": note["summer"],
            "tradeIn": len(order["legs"]) > 1, "waitingMinutes": max(0, desk.now - self.unbooked.get(order["id"], desk.now)),
        }

    def opening_fee(self, desk, order: dict, cars: int, ref: float, fallback: float) -> float:
        """Choose a fee by expected payout plus calibrated failure harm, with a fail-safe fallback."""
        if self.risk_model is None:
            return ref * fallback
        penalty = ref * (self.params["riskPenaltyRef"] + self.shadow())
        best = None
        for multiplier in self.params["riskFeeGrid"]:
            fee = ref * multiplier
            row = self.fee_risk_row(desk, order, cars, fee)
            if row is None:
                return ref * fallback
            failure = self.risk_model.predict_failure(row)
            expected = fee + failure * penalty
            if best is None or expected < best[0]:
                best = (expected, fee)
        return best[1] if best is not None else ref * fallback

    def projected_driver_cost(self, req: dict, option: dict, failure_risk: float) -> float:
        """Conservative driver payout forecast used before deciding whether to bid.

        ``referenceFeeCents`` describes a typical driver near the pickup.  It omits the return
        burden when the delivery town has little supply and the repricing needed after a gig
        fails to book or is dropped.  Quoting against only the opening fee caused orders whose
        driver fee exceeded the dealer price.
        """
        p = self.params
        delivery_support = min(self.prior_ok(leg["delivery"]["placeId"]) for leg in req["legs"])
        road_km = max(road(leg["pickup"]["placeId"], leg["delivery"]["placeId"])["km"]
                      for leg in req["legs"])
        return_risk = 1.0 - delivery_support
        lane_risk = max(0.0, road_km - 60) * p["driverKmRisk"]
        escalation_risk = min(1.0, failure_risk + p["driverReturnRisk"] * return_risk + lane_risk)
        opening = p["f0"] + p["f0Risk"] * failure_risk
        # Later app steps reach f2; a repost can go higher.  This is an expected payout proxy,
        # intentionally above the initial posted fee when supply or the return trip is poor.
        multiplier = max(p["driverCostFloor"],
                         opening + (p["f2"] - opening + p["driverRepostRisk"]) * escalation_risk)
        return req["referenceFeeCents"] * multiplier

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
>>>>>>> Stashed changes

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
<<<<<<< Updated upstream
        ref = req["referenceFeeCents"]
=======
        if not self.capacity_allows(req):
            self.stats["capacityDeclined"] = self.stats.get("capacityDeclined", 0) + 1
            return prices
>>>>>>> Stashed changes
        t = 1 + 0.01 * self.trust.get(req["dealerId"], 0)
        town_fail = max(self.p_fail(l["pickup"]["placeId"]) for l in req["legs"])
        ls = self.late_share()
        lam = self.shadow()
<<<<<<< Updated upstream
        quoted_x = {}
        for option in ok_opts:
            pf = 1 - (1 - town_fail) * (1 - structural(option))
=======
        ref = req["referenceFeeCents"]
        town_fail = max(self.p_fail(l["pickup"]["placeId"]) for l in req["legs"])
        if not p["separateOptionPricing"]:
            # Pre-v5 control: one markup selected from the first eligible option and copied to
            # every option.  Keeping this exact control makes the option-price ablation useful.
            o = ok_opts[0]
            pf = 1 - (1 - town_fail) * (1 - max(structural(oo) for oo in ok_opts))
            if ls >= p["hardLate"] and pf > p["hardPFail"]:
                self.stats["braked"] = self.stats.get("braked", 0) + 1
                return prices
            xs = [math.log(u / t) for u in self.grid]
            pw = self.comp.p_win(req["arrivedAt"], xs, req["dealerId"])
            f0 = p["f0"] + p["f0Risk"] * pf
            driver_cost = max(self.projected_driver_cost(req, oo, pf) for oo in ok_opts)
            price_floor = driver_cost * p["driverMarginFloor"]
            best, bu = -1.0, None
            for i, u in enumerate(self.grid):
                price = u * o["listPriceCents"]
                if price < price_floor:
                    continue
                cost = max(p["costMult"] * f0 * ref, driver_cost) + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                ev = pw[i] * (price - cost)
                if ev > best:
                    best, bu = ev, u
            if best <= p["minEv"] or bu is None:
                if bu is None:
                    self.stats["driverMarginDeclined"] = self.stats.get("driverMarginDeclined", 0) + 1
                self.stats["evNeg"] += 1
                return prices
            x = math.log(bu / t)
            for oo in ok_opts:
                prices[oo["kind"]] = round_half_up(oo["listPriceCents"] * bu / 100) * 100
            self.quoted[req["id"]] = {oo["kind"]: x for oo in ok_opts}
            self.reserve_quote_capacity(req)
            self.stats["quoted"] += 1
            return prices
        learnt: dict = {}
        quoted_any = False
        for o in ok_opts:
            # Each delivery option has its own slack, snow exposure and dealer speed value.  The
            # effective log price is also the correct value to learn from when this option wins.
            pf = 1 - (1 - town_fail) * (1 - structural(o))
>>>>>>> Stashed changes
            if ls >= p["hardLate"] and pf > p["hardPFail"]:
                continue
            premium = p["fastPremium"] if option["kind"] == "same_day" else 0.0
            xs = [math.log(u * (1 + premium) / t) for u in self.grid]
            win_probs = self.comp.p_win(req["arrivedAt"], xs)
            f0 = p["f0"] + p["f0Risk"] * pf
<<<<<<< Updated upstream
            best_ev, best_x = -1, None
            for u, x, win_prob in zip(self.grid, xs, win_probs):
                quote_u = u * (1 + premium)
                price = quote_u * option["listPriceCents"]
                cost = p["costMult"] * f0 * ref + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                ev = win_prob * (price - cost)
                if ev > best_ev:
                    best_ev, best_x = ev, x
            if best_ev > p["minEv"]:
                prices[option["kind"]] = round_half_up(option["listPriceCents"] * math.exp(best_x) * t / 100) * 100
                quoted_x[option["kind"]] = best_x
            else:
                prices[option["kind"]] = round_half_up(option["listPriceCents"] * p["declineMult"] / 100) * 100
        if not quoted_x:
=======
            driver_cost = self.projected_driver_cost(req, o, pf)
            price_floor = driver_cost * p["driverMarginFloor"]
            best, bu = -1.0, None
            for i, u in enumerate(self.grid):
                price = u * o["listPriceCents"]
                if price < price_floor:
                    continue
                cost = max(p["costMult"] * f0 * ref, driver_cost) + pf * lam * (p["failRef"] * ref + p["failPrice"] * price)
                ev = pw[i] * (price - cost)
                if ev > best:
                    best, bu = ev, u
            if best > p["minEv"] and bu is not None:
                prices[o["kind"]] = round_half_up(o["listPriceCents"] * bu / 100) * 100
                learnt[o["kind"]] = math.log(bu / t) - speed
                quoted_any = True
        if not quoted_any:
            self.stats["driverMarginDeclined"] = self.stats.get("driverMarginDeclined", 0) + 1
>>>>>>> Stashed changes
            if ls >= p["hardLate"]:
                self.stats["braked"] = self.stats.get("braked", 0) + 1
            self.stats["evNeg"] += 1
            return prices
<<<<<<< Updated upstream
        self.quoted[req["id"]] = quoted_x
=======
        self.quoted[req["id"]] = learnt
        self.reserve_quote_capacity(req)
>>>>>>> Stashed changes
        self.stats["quoted"] += 1
        return prices

    def on_event(self, desk, e: dict, replay: bool = False) -> None:
        self.last_now = max(self.last_now, e["at"])
        self.sweep_pending(e["at"])
        kind = e["kind"]
        if kind == "order_won":
            oid = e["order"]["id"]
            r = desk.requests.get(oid)
            self.promote_quote_capacity(e["order"], r)
            if oid in self.quoted:
<<<<<<< Updated upstream
                r = desk.requests.get(oid)
                quote_x = self.quoted.pop(oid)
                if isinstance(quote_x, dict):
                    x = quote_x.get(e["order"].get("option"), min(quote_x.values()))
                else:  # state files written by the earlier single-price policy
                    x = quote_x
                self.comp.observe(r["arrivedAt"] if r else e["at"], x, True)
=======
                options = self.quoted.pop(oid)
                # Saved state from the pre-option policy contains one float; retain its learning
                # signal on a rolling deploy instead of failing the event handler.
                x = options.get(e["order"]["option"]) if isinstance(options, dict) else options
                if x is not None:
                    self.comp.observe(r["arrivedAt"] if r else e["at"], x, True, e["order"]["dealerId"])
            self.record_booking_row(desk, e["order"], e["at"])
>>>>>>> Stashed changes
            self.order_town[oid] = e["order"]["legs"][0]["pickup"]["placeId"]
            self.open_orders.add(oid)
            self.pending[oid] = e["at"]
            self.unbooked[oid] = e["at"]
        elif kind == "order_lost":
            self.quote_capacity.pop(e["requestId"], None)
            if e["requestId"] in self.quoted:
                r = desk.requests.get(e["requestId"])
                quote_x = self.quoted.pop(e["requestId"])
                if isinstance(quote_x, dict):
                    x = min(quote_x.values())  # loss is evidence against the most competitive option we offered
                else:  # state files written by the earlier single-price policy
                    x = quote_x
                self.comp.observe(r["arrivedAt"] if r else e["at"], x, False)
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
<<<<<<< Updated upstream
=======
            self.bundle_pending.pop(e["orderId"], None)
            self.order_capacity.pop(e["orderId"], None)
>>>>>>> Stashed changes
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
                self.order_capacity.pop(e["orderId"], None)
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
        opening = self.opening_fee(desk, order, cars, ref, f0)
        steps = [{"at": desk.now, "feeCents": round_half_up(opening)}]
        t1 = max(desk.now + 1, order["lastPickupAt"] - p["f1Lead"])
        t2 = max(t1 + 1, order["lastPickupAt"] - p["f2Lead"])
        steps.append({"at": t1, "feeCents": round_half_up(ref * max(f0, p["f1"]))})
        steps.append({"at": t2, "feeCents": round_half_up(ref * max(f0, p["f2"]))})
        return steps
