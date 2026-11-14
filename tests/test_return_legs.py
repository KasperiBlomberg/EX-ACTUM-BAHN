"""Safety checks for return-leg pairing: the matcher, the holding queue, fallbacks and the quote."""
from __future__ import annotations

import unittest

from spread_bot.desk import Desk
from spread_bot.experiments import compare, experiment_params, pair_metrics, summarize
from spread_bot.geo import P, ROAD
from spread_bot.policy_v4 import PolicyV4
from spread_bot.return_legs import job, plan_pair, site_open

HOURS = {"opens": 420, "closes": 1200, "weekdays": [0, 1, 2, 3, 4], "saturday": None}


def install_map(minutes: int = 120) -> None:
    P.clear()
    P.update({"yard": {"id": "yard", "lat": 60.0, "lon": 24.0},
              "north": {"id": "north", "lat": 61.0, "lon": 24.0},
              "east": {"id": "east", "lat": 60.0, "lon": 25.0}})
    ROAD.clear()
    for a in P:
        for b in P:
            ROAD[a, b] = {"from": a, "to": b, "km": 0 if a == b else 120, "minutes": 0 if a == b else minutes}


def leg(pickup: str, delivery: str, start: int = 480, **car) -> dict:
    return {"car": {"premium": False, "electric": False, "chargePercent": None, **car},
            "pickup": {"placeId": pickup, "privateAddress": False, "window": {"from": start, "to": 4080}, "hours": HOURS},
            "delivery": {"placeId": delivery, "privateAddress": False, "window": {"from": start, "to": 4080}, "hours": HOURS}}


def request(order_id: str, pickup: str, delivery: str, note: str = None, **car) -> dict:
    return {"id": order_id, "dealerId": "dealer", "batchId": None, "arrivedAt": 485, "respondBy": 500,
            "legs": [leg(pickup, delivery, **car)],
            "options": [{"kind": "next_day", "deadline": 2640, "listPriceCents": 10000}],
            "referenceFeeCents": 7000, "note": {"text": note} if note else None}


def order(req: dict, last_pickup: int = 2490) -> dict:
    return {"id": req["id"], "dealerId": "dealer", "option": "next_day", "priceCents": 15000, "deadline": 2640,
            "legs": req["legs"], "status": "waiting", "deliveredAt": [None], "lastPickupAt": last_pickup}


def a_job(job_id: str, pickup: str, delivery: str, start: int = 480, last: int = 2490) -> dict:
    return job(job_id, leg(pickup, delivery, start), start, last, 7000)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        install_map()

    def plan(self, a, b, now=500):
        return plan_pair(a, b, now, 30, 540, 90)

    def test_valid_return_pair(self):
        plan, reason = self.plan(a_job("a", "yard", "north"), a_job("b", "north", "yard"))
        self.assertIsNone(reason)
        self.assertEqual(plan["driveMinutes"], 240)
        self.assertGreaterEqual(plan["latest"] - 500, 540)
        self.assertEqual({plan["first"], plan["second"]}, {"a", "b"})

    def test_a_car_that_does_not_come_back_is_no_return_leg(self):
        self.assertEqual(self.plan(a_job("a", "yard", "north"), a_job("b", "north", "east"))[1], "distance")
        self.assertEqual(self.plan(a_job("a", "yard", "north"), a_job("b", "yard", "north"))[1], "distance")

    def test_incompatible_windows(self):
        # b is ready only on Thursday morning, after a must have been collected
        late = a_job("b", "north", "yard", start=3 * 1440 + 480, last=3 * 1440 + 900)
        self.assertEqual(self.plan(a_job("a", "yard", "north"), late)[1], "windows")

    def test_closed_sites_make_the_windows_incompatible(self):
        a, b = a_job("a", "yard", "north"), a_job("b", "north", "yard")
        b["pickupHours"] = a["deliveryHours"] = {"opens": 420, "closes": 450, "weekdays": [0, 1, 2, 3, 4], "saturday": None}
        self.assertEqual(self.plan(a, b)[1], "windows")
        self.assertFalse(site_open(HOURS, 6 * 1440 + 600))           # Sunday
        self.assertFalse(site_open(HOURS, 1190))                     # inside the closing margin
        self.assertTrue(site_open(HOURS, 600))

    def test_twelve_hour_driving_limit(self):
        install_map(minutes=250)                                      # 250 + 0 + 250 fits, three legs do not
        self.assertIsNotNone(self.plan(a_job("a", "yard", "north"), a_job("b", "north", "yard"))[0])
        install_map(minutes=365)                                      # 730 road minutes
        self.assertEqual(self.plan(a_job("a", "yard", "north"), a_job("b", "north", "yard"))[1], "driving_limit")

    def test_near_deadline_is_rejected(self):
        soon = a_job("b", "north", "yard", last=1000)                 # must be collected today
        self.assertEqual(self.plan(a_job("a", "yard", "north"), soon)[1], "near_deadline")


class Api:
    """Accepts every action sent outside an advance."""

    def __init__(self):
        self.n = 0

    def act(self, _world, actions):
        self.n += len(actions)
        return [{"ok": True, "id": f"sent-{self.n}-{i}"} for i in range(len(actions))]


class PairingPolicyTests(unittest.TestCase):
    def setUp(self):
        install_map()
        self.use(PolicyV4({**PolicyV4.RANKED_DEPLOYMENT, "enableReturnPairs": True}))

    def use(self, policy: PolicyV4) -> None:
        self.policy = policy
        self.policy.homes = {"yard": 30, "north": 30}
        self.desk = Desk(Api(), "w", 8, 500, self.policy)

    def win(self, req: dict, at: int = 500, last_pickup: int = 2490, price: int = 15000) -> dict:
        self.desk.now = at
        self.desk.requests[req["id"]] = req
        won = {**order(req, last_pickup), "priceCents": price}
        self.desk.handle({"kind": "order_won", "at": at, "order": won})
        return won

    def carry(self, gig_id: str = "g1") -> dict:
        """The world accepts everything in the outbox."""
        actions, self.desk.outbox = self.desk.outbox, []
        self.desk.read_results(actions, [{"ok": True, "id": f"{gig_id}-{i}"} for i in range(len(actions))])
        return actions

    def posts(self) -> list:
        return [a for a in self.desk.outbox if a["type"] == "post"]

    def test_first_order_is_held_then_paired_into_one_discounted_gig(self):
        self.win(request("a", "yard", "north"))
        self.assertEqual(self.desk.outbox, [])
        self.assertIn("a", self.policy.pair_hold)
        self.win(request("b", "north", "yard"), at=520)
        [action] = self.posts()
        stops = [(s["orderId"], s["kind"]) for s in action["stops"]]
        self.assertIn(stops, ([("a", "pickup"), ("a", "delivery"), ("b", "pickup"), ("b", "delivery")],
                              [("b", "pickup"), ("b", "delivery"), ("a", "pickup"), ("a", "delivery")]))
        fees = [step["feeCents"] for step in action["schedule"]]
        self.assertEqual(fees[0], round(0.62 * 14000))
        self.assertEqual(fees, sorted(fees))
        self.assertGreaterEqual(fees[1], 0.92 * 14000)                # the two-gig level before the warning
        self.assertLess(action["schedule"][-1]["at"], 2490 - 180)
        self.assertFalse(self.policy.pair_hold)
        stats = self.policy.pair_stats
        self.assertEqual((stats["candidates"], stats["feasible"], stats["posted"]), (1, 1, 1))

    def test_booked_pair_records_saving_and_on_time(self):
        self.win(request("a", "yard", "north"))
        self.win(request("b", "north", "yard"), at=520)
        self.carry()
        self.desk.now = 530
        self.desk.handle({"kind": "gig_booked", "at": 530, "gigId": "g1-0"})
        row = next(iter(self.policy.pairs.values()))
        self.assertEqual(row["state"], "booked")
        self.assertGreater(row["savingCents"], 0)
        self.assertEqual(self.policy.pair_stats["feeSavingCents"], row["savingCents"])
        self.desk.now = 900
        self.desk.handle({"kind": "delivered", "at": 800, "orderId": "a", "leg": 0})
        self.desk.handle({"kind": "delivered", "at": 3000, "orderId": "b", "leg": 0})
        self.assertEqual(row["onTime"], {"a": True, "b": False})
        metrics = pair_metrics({"pairStats": self.policy.pair_stats, "pairRows": list(self.policy.pairs.values())})
        self.assertEqual((metrics["booked"], metrics["pairedOnTimeShare"]), (1, 0.5))
        self.desk.now = 1000
        self.policy.tick(self.desk)                                   # a booked pair is never dissolved
        self.assertEqual(self.desk.outbox, [])

    def test_unmatched_hold_is_released_as_a_single_gig_after_the_hold(self):
        self.win(request("a", "yard", "north"))
        self.assertEqual(self.policy.next_wake(self.desk), 560)
        self.desk.now = 559
        self.policy.tick(self.desk)
        self.assertEqual(self.desk.outbox, [])
        self.desk.now = 560
        self.policy.tick(self.desk)
        [action] = self.posts()
        self.assertEqual(len(action["stops"]), 2)
        self.assertEqual(action["schedule"][0]["at"], 560)
        self.assertEqual(self.policy.pair_stats["holdReleased"], 1)

    def test_order_without_slack_is_not_delayed(self):
        self.win(request("a", "yard", "north"), last_pickup=1090)     # under pairMinLead + pairHold away
        self.assertNotIn("a", self.policy.pair_hold)
        self.assertEqual(len(self.posts()), 1)

    def test_no_pair_when_the_partner_is_too_close_to_its_last_pickup(self):
        self.win(request("a", "yard", "north"), last_pickup=1100)     # may be held, cannot wait for a round trip
        self.win(request("b", "north", "yard"), at=520)
        self.assertEqual(self.policy.pair_stats["rejects"]["near_deadline"], 1)
        self.assertEqual(self.policy.pair_stats["posted"], 0)
        self.assertFalse(any(len(a["stops"]) == 4 for a in self.posts()))

    def test_unbooked_pair_dissolves_into_two_single_gigs(self):
        self.win(request("a", "yard", "north"))
        self.win(request("b", "north", "yard"), at=520)
        self.carry()
        self.desk.now = 579
        self.policy.tick(self.desk)
        self.assertEqual(self.desk.outbox, [])
        self.desk.now = 580
        self.policy.tick(self.desk)
        self.assertEqual([a["type"] for a in self.desk.outbox], ["withdraw", "post", "post"])
        singles = self.posts()
        self.assertEqual({a["stops"][0]["orderId"] for a in singles}, {"a", "b"})
        for action in singles:
            self.assertEqual(len(action["stops"]), 2)
            self.assertGreaterEqual(action["schedule"][0]["feeCents"], 0.92 * 7000)   # the ordinary single fee
        self.assertEqual(self.policy.pair_stats["dissolved"], 1)
        self.assertFalse(self.policy.pair_of)
        self.policy.tick(self.desk)                                   # and only once
        self.assertEqual(len(self.desk.outbox), 3)

    def test_pair_is_not_withdrawn_while_a_later_booking_is_unhandled(self):
        self.win(request("a", "yard", "north"))
        self.win(request("b", "north", "yard"), at=520)
        self.carry()
        self.desk.now = 600                                           # one step brings both events
        self.desk.handle({"kind": "order_lost", "at": 590, "requestId": "x"})
        self.assertEqual(self.desk.outbox, [])
        self.desk.handle({"kind": "gig_booked", "at": 595, "gigId": "g1-0"})
        self.policy.tick(self.desk)
        self.assertEqual(self.desk.outbox, [])

    def test_refused_pair_falls_back_to_single_gigs(self):
        self.win(request("a", "yard", "north"))
        self.win(request("b", "north", "yard"), at=520)
        bundle, self.desk.outbox = self.desk.outbox, []
        self.desk.read_results(bundle, [{"ok": False, "reason": "window changed"}])
        self.assertEqual(sorted(a["stops"][0]["orderId"] for a in self.posts()), ["a", "b"])
        self.assertTrue(all(len(a["stops"]) == 2 for a in self.posts()))
        self.carry("s")
        self.policy.tick(self.desk)                                   # the pair is over, nothing is withdrawn
        self.assertEqual(self.desk.outbox, [])
        self.assertEqual(next(iter(self.policy.pairs.values()))["reason"], "refused")

    def test_failing_bundle_post_leaves_individual_behaviour(self):
        self.win(request("a", "yard", "north"))

        def broken(_items, _retry=None):
            raise RuntimeError("desk bug")

        self.desk.post_bundle = broken
        self.win(request("b", "north", "yard"), at=520)
        self.assertEqual([a["stops"][0]["orderId"] for a in self.posts()], ["b"])   # posted at once
        self.assertEqual(self.policy.pair_stats["postFailed"], 1)
        self.desk.now = 560
        self.policy.tick(self.desk)
        self.assertEqual(sorted(a["stops"][0]["orderId"] for a in self.posts()), ["a", "b"])

    def test_dropped_pair_goes_back_to_single_gigs(self):
        self.win(request("a", "yard", "north"))
        self.win(request("b", "north", "yard"), at=520)
        self.carry()
        self.desk.now = 530
        self.desk.handle({"kind": "gig_booked", "at": 530, "gigId": "g1-0"})
        self.desk.now = 700
        self.desk.handle({"kind": "gig_dropped", "at": 700, "gigId": "g1-0"})
        self.policy.tick(self.desk)
        self.assertEqual([a["type"] for a in self.desk.outbox], ["withdraw", "post", "post"])
        self.assertEqual(self.policy.pair_stats["feeSavingCents"], 0)
        self.assertEqual(next(iter(self.policy.pairs.values()))["reason"], "dropped")

    def test_restricted_orders_are_never_held_or_paired(self):
        private = request("p", "north", "yard")
        private["legs"][0]["delivery"]["privateAddress"] = True
        restricted = [private,
                      request("m", "north", "yard", note="Manual gearbox."),
                      request("k", "north", "yard", note="No plates on the car."),
                      request("s", "north", "yard", note="Still on summer tyres."),
                      request("t", "north", "yard", note="Pickup before 14:00."),
                      request("e", "north", "yard", electric=True, chargePercent=20),
                      request("x", "north", "yard", premium=True)]
        tradein = request("d", "north", "yard")
        tradein["legs"].append(leg("yard", "north"))
        self.win(request("a", "yard", "north"))
        for at, req in enumerate(restricted, 501):
            self.win(req, at=at)
        self.desk.now = 510
        self.desk.requests["d"] = tradein
        self.desk.handle({"kind": "order_won", "at": 510, "order": {**order(tradein), "deliveredAt": [None, None]}})
        self.assertEqual(set(self.policy.pair_hold), {"a"})
        self.assertEqual(self.policy.pair_stats["posted"], 0)
        self.assertEqual(self.policy.pair_stats["candidates"], 0)

    def test_snow_at_either_end_keeps_the_car_out_of_pairing(self):
        self.policy.on_event(self.desk, {"kind": "forecast", "at": 490, "from": 600, "to": 900, "placeIds": ["north"]})
        self.win(request("a", "yard", "north"))
        self.assertFalse(self.policy.pair_hold)
        self.assertEqual(len(self.posts()), 1)

    def test_changed_held_order_is_posted_once_by_the_desk(self):
        won = self.win(request("a", "yard", "north"))
        self.desk.now = 520
        self.desk.handle({"kind": "order_changed", "at": 520, "order": won})
        self.assertEqual(len(self.posts()), 1)
        self.desk.now = 560
        self.policy.tick(self.desk)
        self.assertEqual(len(self.posts()), 1)

    # ------------------------------------------------------------ the quote
    def price(self, policy: PolicyV4, req: dict) -> int:
        return policy.quote(self.desk, req)["next_day"]

    def quotes_after_one_held_order(self, pair_quote: bool, last_pickup: int = 2490) -> dict:
        """The same history with and without the quote-side extension: a holds, then four quotes."""
        self.use(PolicyV4({**PolicyV4.RANKED_DEPLOYMENT, "enableReturnPairs": True, "pairQuote": pair_quote}))
        prices = {"early": self.price(self.policy, request("b0", "north", "yard"))}      # nothing held yet
        self.win(request("a", "yard", "north"), last_pickup=last_pickup)
        self.desk.now = 505
        for rid, destination, note in (("b", "yard", None), ("c", "east", None), ("b2", "yard", None), ("m", "yard", "Manual gearbox.")):
            req = request(rid, "north", destination, note)
            req["arrivedAt"], req["respondBy"] = 505, 520
            self.desk.requests[rid] = req
            prices[rid] = self.price(self.policy, req)
        return prices

    def test_completing_request_is_quoted_lower_and_others_are_not(self):
        alone = self.quotes_after_one_held_order(False)
        paired = self.quotes_after_one_held_order(True)
        self.assertLess(paired["b"], alone["b"])
        # another lane, a second request for the same held car, a restricted car: standalone prices
        self.assertEqual({k: v for k, v in paired.items() if k != "b"}, {k: v for k, v in alone.items() if k != "b"})
        self.assertEqual(self.policy.pair_stats["quoteDiscounted"], 1)
        # the held order is promised to this request: it is not released before the dealer answers
        self.desk.now = 519
        self.policy.tick(self.desk)
        self.assertIn("a", self.policy.pair_hold)
        back = self.desk.requests["b"]
        self.win(back, at=520, price=paired["b"])
        self.assertEqual(self.policy.pair_stats["posted"], 1)
        self.assertEqual(self.policy.pair_stats["quoteWins"], 1)
        self.assertEqual(self.policy.pair_stats["quoteDiscountCents"], alone["b"] - paired["b"])
        self.assertTrue(next(iter(self.policy.pairs.values()))["viaQuote"])

    def test_lost_request_frees_the_held_order(self):
        self.win(request("a", "yard", "north"))
        self.price(self.policy, request("b", "north", "yard"))
        self.assertIn("a", self.policy.pair_reserved)
        self.policy.on_event(self.desk, {"kind": "order_lost", "at": 500, "requestId": "b"})
        self.assertFalse(self.policy.pair_reserved)
        self.assertFalse(self.policy.pair_quoted)

    def test_no_discount_when_the_pair_could_not_be_posted_safely(self):
        # a is held, but too close to its last pickup for a round trip
        alone = self.quotes_after_one_held_order(False, last_pickup=1100)
        self.assertEqual(self.quotes_after_one_held_order(True, last_pickup=1100), alone)
        self.assertEqual(self.policy.pair_stats["quoteCandidates"], 2)
        self.assertEqual(self.policy.pair_stats["quoteDiscounted"], 0)
        self.assertFalse(self.policy.pair_reserved)

    # ------------------------------------------------------------ decline reasons
    def test_declines_are_counted_by_reason(self):
        p = PolicyV4(PolicyV4.RANKED_DEPLOYMENT)
        p.homes = self.policy.homes
        far = request("far", "yard", "north")
        ROAD["yard", "north"] = {"from": "yard", "to": "north", "km": 580, "minutes": 120}
        p.quote(None, far)
        ROAD["yard", "north"]["km"] = 120
        tight = request("tight", "yard", "north")
        tight["options"][0]["deadline"] = 500 + 120 + 30 + 340        # 340 minutes of slack
        p.quote(None, tight)
        private = request("priv", "yard", "north")
        private["legs"][0]["pickup"]["privateAddress"] = True
        private["legs"][0]["pickup"]["window"] = {"from": 480, "to": 1000}   # a 500-minute slot
        p.quote(None, private)
        tradein = request("trade", "yard", "north")
        tradein["legs"].append(leg("north", "yard"))
        tradein["legs"][1]["pickup"]["privateAddress"] = True
        p.quote(None, tradein)
        p.late_orders, p.done_orders = 10, 40                         # 25% late: the hard brake
        p.town["yard"] = {"ok": 0, "fail": 30}
        p.quote(None, request("braked", "yard", "north"))
        self.assertEqual(p.decline_reasons, {"maxKm": 1, "minSlack": 1, "privateSlack": 1, "privateTradeIn": 1,
                                             "hardLateBrake": 1, "negativeEv": 0})
        self.assertEqual(p.decline_near, {"km600": {"n": 1, "listCents": 10000}, "slack330": {"n": 1, "listCents": 10000}})
        self.assertEqual(p.stats["declined"], 4)
        cheap = request("cheap", "yard", "north")
        cheap["referenceFeeCents"] = 90000
        p.late_orders = 0
        p.quote(None, cheap)
        self.assertEqual(p.decline_reasons["negativeEv"], 1)
        self.assertEqual(p.stats["evNeg"], 1)


class FollowOfferTests(unittest.TestCase):
    def setUp(self):
        install_map()
        self.policy = PolicyV4({**PolicyV4.RANKED_DEPLOYMENT, "enableFollowOffers": True})
        self.policy.homes = {"yard": 30}
        self.policy.drivers = {"d1": {"id": "d1", "homePlaceId": "yard", "rating": 4.5, "completedGigs": 10, "licence": "manual"}}
        self.desk = Desk(Api(), "w", 8, 600, self.policy)

    def post(self, rid: str, pickup: str, delivery: str, gig: str) -> None:
        req = request(rid, pickup, delivery)
        self.desk.requests[rid] = req
        self.desk.handle({"kind": "order_won", "at": self.desk.now, "order": order(req)})
        actions, self.desk.outbox = self.desk.outbox, []
        self.desk.read_results(actions, [{"ok": True, "id": gig}])

    def test_driver_with_a_booking_is_offered_the_car_that_continues_it_below_the_app_fee(self):
        self.post("a", "yard", "north", "g1")
        self.post("b", "north", "yard", "g2")          # continues a's trip
        self.post("c", "east", "east", "g3")           # unrelated
        self.desk.handle({"kind": "gig_booked", "at": 600, "gigId": "g1", "driverId": "d1", "feeCents": 6500})
        self.policy.tick(self.desk)
        offers = [a for a in self.desk.outbox if a["type"] == "offer"]
        self.assertEqual([(a["gigId"], a["driverId"]) for a in offers], [("g2", "d1")])
        self.assertEqual(offers[0]["feeCents"], round(0.65 * 7000))
        self.assertLess(offers[0]["feeCents"], self.desk.posts["g2"]["schedule"][0]["feeCents"])
        self.desk.now = 601
        self.policy.tick(self.desk)                    # one offer per driver and gig
        self.assertEqual(len([a for a in self.desk.outbox if a["type"] == "offer"]), 1)
        self.desk.handle({"kind": "offer_answered", "at": 601, "gigId": "g2", "driverId": "d1", "answer": "accepted"})
        self.assertEqual(self.policy.follow_stats["accepted"], 1)
        self.assertGreater(self.policy.follow_stats["savingCents"], 0)

    def test_follow_offers_are_off_by_default(self):
        self.assertFalse(PolicyV4(PolicyV4.RANKED_DEPLOYMENT).params["enableFollowOffers"])


class DropHoldTests(unittest.TestCase):
    def drop(self, params: dict, last_pickup: int) -> list:
        install_map()
        policy = PolicyV4({**PolicyV4.RANKED_DEPLOYMENT, **params})
        policy.homes = {"yard": 30}
        desk = Desk(Api(), "w", 8, 600, policy)
        req = request("a", "yard", "north")
        desk.requests["a"] = req
        desk.handle({"kind": "order_won", "at": 600, "order": order(req, last_pickup)})
        actions, desk.outbox = desk.outbox, []
        desk.read_results(actions, [{"ok": True, "id": "g1"}])
        desk.handle({"kind": "gig_booked", "at": 610, "gigId": "g1", "driverId": "d1", "feeCents": 6500})
        desk.now = 700
        desk.handle({"kind": "gig_dropped", "at": 700, "gigId": "g1", "driverId": "d1"})
        return [a for a in desk.outbox if a["type"] == "reprice"]

    def test_drop_with_time_to_spare_keeps_the_fee_schedule(self):
        self.assertEqual(self.drop({"dropHoldLead": 600}, last_pickup=2490), [])

    def test_drop_close_to_the_last_pickup_still_steps_up(self):
        [reprice] = self.drop({"dropHoldLead": 600}, last_pickup=1200)
        self.assertGreaterEqual(reprice["schedule"][0]["feeCents"], 1.25 * 7000)

    def test_default_steps_up_on_every_drop(self):
        self.assertEqual(len(self.drop({}, last_pickup=2490)), 1)


class LowTrustSlackTests(unittest.TestCase):
    def test_tight_car_is_quoted_only_for_a_dealer_with_little_trust_to_lose(self):
        install_map()
        tight = request("tight", "yard", "north")
        tight["options"][0]["deadline"] = 500 + 120 + 30 + 300        # 300 minutes of slack
        declined = tight["options"][0]["listPriceCents"] * 5
        control = PolicyV4(PolicyV4.RANKED_DEPLOYMENT)
        loose = PolicyV4({**PolicyV4.RANKED_DEPLOYMENT, "minSlackLowTrust": 240})
        for p in (control, loose):
            p.homes = {"yard": 30}
        self.assertEqual(control.quote(None, tight)["next_day"], declined)
        self.assertLess(loose.quote(None, tight)["next_day"], declined)
        loose.trust["dealer"] = 3
        self.assertEqual(loose.quote(None, tight)["next_day"], declined)
        private = request("priv", "yard", "north")
        private["legs"][0]["pickup"]["privateAddress"] = True
        private["legs"][0]["pickup"]["window"] = {"from": 480, "to": 1000}
        loose.trust["dealer"] = 0
        self.assertEqual(loose.quote(None, private)["next_day"], declined)   # private cars keep their gate


class ExperimentConfigTests(unittest.TestCase):
    def test_return_pairs_are_off_by_default_and_differ_from_the_control_by_one_switch(self):
        self.assertFalse(PolicyV4(PolicyV4.RANKED_DEPLOYMENT).params["enableReturnPairs"])
        self.assertFalse(PolicyV4().params["enableReturnPairs"])
        control, pairs = experiment_params("lanes_trust"), experiment_params("return_pairs")
        self.assertEqual({k for k in pairs if control.get(k) != pairs[k]}, {"enableReturnPairs"})

    def test_old_report_rows_still_summarise(self):
        rows = [{"experiment": "lanes_trust", "seed": "s1", "marginCents": 100000, "onTimeShare": 0.96, "wonOrders": 30, "ranked": True}]
        summary = summarize(rows)["lanes_trust"]
        self.assertEqual(summary["returnPairs"]["posted"], 0)
        self.assertIsNone(summary["returnPairs"]["pairedOnTimeShare"])
        self.assertEqual(summary["declineReasons"], {})
        new = {**rows[0], "seed": "s2", "declineReasons": {"maxKm": 2, "nearMiss": {"km600": {"n": 1, "listCents": 500}}}}
        reasons = summarize(rows + [new, {**new, "seed": "s3"}])["lanes_trust"]["declineReasons"]
        self.assertEqual(reasons, {"maxKm": 4, "nearMiss": {"km600": {"n": 2, "listCents": 1000}}})

    def test_comparison_applies_the_promotion_rule(self):
        def row(name, seed, margin, on_time, orders=100):
            return {"experiment": name, "seed": seed, "marginCents": margin, "onTimeShare": on_time, "wonOrders": orders, "ranked": True}

        rows = [row("lanes_trust", s, 1000000, 0.96) for s in ("s1", "s2", "s3")]
        up = rows + [row("return_pairs", s, 1050000, 0.95, 110) for s in ("s1", "s2", "s3")]
        self.assertFalse(compare(up, "lanes_trust", "return_pairs")["promote"])     # three seeds are too few
        self.assertTrue(compare(up, "lanes_trust", "return_pairs", min_seeds=3)["promote"])
        self.assertEqual(compare(up, "lanes_trust", "return_pairs")["meanAddedOrders"], 10)
        outlier = rows + [row("return_pairs", "s1", 1300000, 0.95), row("return_pairs", "s2", 990000, 0.95),
                          row("return_pairs", "s3", 990000, 0.95)]
        result = compare(outlier, "lanes_trust", "return_pairs", min_seeds=3)
        self.assertGreater(result["meanMarginDiffEuros"], 0)
        self.assertFalse(result["promote"])                           # one seed carries the mean
        late = rows + [row("return_pairs", s, 1050000, 0.95 if s != "s2" else 0.92) for s in ("s1", "s2", "s3")]
        self.assertFalse(compare(late, "lanes_trust", "return_pairs")["promote"])


if __name__ == "__main__":
    unittest.main()
