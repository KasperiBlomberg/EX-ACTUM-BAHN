"""Small deterministic checks for v4's option, chain, weather and offer extensions."""
from __future__ import annotations

import unittest

from spread_bot.desk import Desk
from spread_bot.geo import P, ROAD
from spread_bot.policy_v2 import CompModel
from spread_bot.policy_v4 import PolicyV4


def install_map() -> None:
    P.clear()
    P.update({"yard": {"id": "yard", "lat": 60.0, "lon": 24.0},
              "north": {"id": "north", "lat": 61.0, "lon": 24.0},
              "east": {"id": "east", "lat": 60.0, "lon": 25.0}})
    ROAD.clear()
    for a in P:
        for b in P:
            ROAD[a, b] = {"from": a, "to": b, "km": 0 if a == b else 120, "minutes": 0 if a == b else 120}


def leg(destination: str = "north") -> dict:
    return {"car": {"premium": False, "electric": False},
            "pickup": {"placeId": "yard", "privateAddress": False, "window": {"from": 480, "to": 4000}, "hours": None},
            "delivery": {"placeId": destination, "privateAddress": False, "window": {"from": 480, "to": 4000}, "hours": None}}


def request(order_id: str, options=None) -> dict:
    return {"id": order_id, "dealerId": "dealer", "batchId": None, "arrivedAt": 480, "respondBy": 500,
            "legs": [leg()], "options": options or [{"kind": "same_day", "deadline": 1250, "listPriceCents": 10000},
                                                        {"kind": "next_day", "deadline": 2690, "listPriceCents": 10000}],
            "referenceFeeCents": 7000, "note": None}


def order(order_id: str, last_pickup=1100) -> dict:
    return {"id": order_id, "dealerId": "dealer", "option": "next_day", "priceCents": 15000, "deadline": 2690,
            "legs": [leg()], "status": "waiting", "deliveredAt": [None], "lastPickupAt": last_pickup}


class Api:
    def snapshot(self, _world):
        return {"driverPositions": [{"driverId": "driver", "placeId": "yard"}]}


class PolicyV4Tests(unittest.TestCase):
    def setUp(self):
        install_map()

    def policy(self) -> PolicyV4:
        p = PolicyV4()
        p.homes = {"yard": 30}
        p.drivers = {"driver": {"id": "driver", "homePlaceId": "yard", "rating": 5.0, "completedGigs": 100, "licence": "manual"}}
        return p

    def test_same_day_is_priced_above_next_day(self):
        p = self.policy()
        prices = p.quote(None, request("r1"))
        self.assertGreater(prices["same_day"], prices["next_day"])
        self.assertEqual(set(p.quoted["r1"]), {"same_day", "next_day"})

    def test_same_yard_orders_make_one_discounted_chain(self):
        p = self.policy()
        desk = Desk(Api(), "w", 8, 500, p)
        for oid in ("a", "b"):
            desk.requests[oid] = request(oid)
            desk.orders[oid] = order(oid)
            p.bundle_pending[oid] = 500
        p.flush_chains(desk)
        self.assertEqual(len(desk.outbox), 1)
        action = desk.outbox[0]
        self.assertEqual(len(action["stops"]), 4)
        self.assertLess(action["schedule"][0]["feeCents"], 2 * 7000 * p.params["f0"])

    def test_rejected_chain_falls_back_to_single_car_posts(self):
        p = self.policy()
        desk = Desk(Api(), "w", 8, 500, p)
        for oid in ("a", "b"):
            desk.requests[oid] = request(oid)
            desk.orders[oid] = order(oid)
            p.bundle_pending[oid] = 500
        p.flush_chains(desk)
        bundle, desk.outbox = desk.outbox[0], []
        desk.read_results([bundle], [{"ok": False, "reason": "window changed"}])
        self.assertEqual(len(desk.outbox), 2)
        self.assertTrue(all(len(a["stops"]) == 2 for a in desk.outbox))

    def test_changing_one_open_chain_order_reposts_its_sibling(self):
        p = self.policy()
        desk = Desk(Api(), "w", 8, 500, p)
        for oid in ("a", "b"):
            desk.requests[oid] = request(oid)
            desk.orders[oid] = order(oid)
        action = {"type": "post", "stops": [{"orderId": "a", "leg": 0, "kind": "pickup"}, {"orderId": "a", "leg": 0, "kind": "delivery"},
                                                   {"orderId": "b", "leg": 0, "kind": "pickup"}, {"orderId": "b", "leg": 0, "kind": "delivery"}],
                  "audience": {"experienced": False, "manualLicence": False}, "schedule": [{"at": 500, "feeCents": 10000}]}
        desk.carry(action, "g")
        desk.change(order("a"))
        self.assertEqual(desk.outbox[0]["type"], "withdraw")
        self.assertEqual({a["stops"][0]["orderId"] for a in desk.outbox[1:]}, {"a", "b"})

    def test_forecast_delays_summer_tyre_pickup(self):
        p = self.policy()
        p.on_event(None, {"kind": "forecast", "at": 500, "from": 500, "to": 700, "placeIds": ["yard"]})
        req = request("summer")
        p.notes["summer"] = {"ready_at": None, "release_at": None, "deadline_at": None, "pickup_open": None,
                              "pickup_close": None, "delivery_close": None, "manual": False, "plates": False,
                              "summer": True, "trace": []}
        self.assertEqual(p.pickup_window_for(req, {"from": 500, "to": 900}, 0)["from"], 700)

    def test_targeted_offer_is_above_the_app_fee(self):
        p = self.policy()
        desk = Desk(Api(), "w", 8, 8 * 60, p)
        o = order("a", last_pickup=680)
        desk.orders["a"] = o
        p.open_orders.add("a")
        desk.posts["g"] = {"type": "post", "stops": [{"orderId": "a", "leg": 0, "kind": "pickup"}],
                           "audience": {"experienced": False, "manualLicence": False},
                           "schedule": [{"at": 480, "feeCents": 7000}]}
        desk.leg_gigs["a"] = {0: "g"}
        desk.gig_info["g"] = {"orderId": "a", "orderIds": ["a"], "firstFee": 7000, "cars": 1}
        p.send_targeted_offers(desk)
        self.assertEqual(len(desk.outbox), 1)
        self.assertGreater(desk.outbox[0]["feeCents"], 7000)

    def test_offer_telemetry_keeps_context_response_and_delivery_label(self):
        p = PolicyV4({"recordTelemetry": True})
        p.homes = {"yard": 30}
        p.drivers = {"driver": {"id": "driver", "homePlaceId": "yard", "rating": 5.0,
                                "completedGigs": 100, "drops": 2, "licence": "manual"}}
        desk = Desk(Api(), "w", 8, 8 * 60, p)
        o = order("a", last_pickup=680)
        desk.orders["a"] = o
        p.open_orders.add("a")
        desk.posts["g"] = {"type": "post", "stops": [{"orderId": "a", "leg": 0, "kind": "pickup"}],
                           "audience": {"experienced": False, "manualLicence": False},
                           "schedule": [{"at": 480, "feeCents": 7000}]}
        desk.leg_gigs["a"] = {0: "g"}
        desk.gig_info["g"] = {"orderId": "a", "orderIds": ["a"], "firstFee": 7000, "cars": 1}
        p.send_targeted_offers(desk)
        row = p.offer_training_rows()[0]
        self.assertEqual(row["appFeeCents"], 7000)
        self.assertGreater(row["feePremiumCents"], 0)
        self.assertEqual(row["driverPlaceId"], "yard")
        p.on_event(desk, {"kind": "offer_answered", "at": 485, "gigId": "g", "driverId": "driver", "answer": "accepted"})
        p.on_event(desk, {"kind": "gig_booked", "at": 485, "gigId": "g"})
        p.on_event(desk, {"kind": "delivered", "at": 600, "orderId": "a", "leg": 0})
        self.assertEqual(row["answer"], "accepted")
        self.assertEqual(row["gigBookedAt"], 485)
        self.assertTrue(row["orderOutcomes"]["a"]["onTime"])

    def test_booking_a_chain_resolves_each_order(self):
        p = self.policy()
        desk = Desk(Api(), "w", 8, 500, p)
        desk.gig_info["g"] = {"orderId": "a", "orderIds": ["a", "b"], "firstFee": 10000, "cars": 2}
        p.order_town = {"a": "yard", "b": "yard"}
        p.open_orders.update({"a", "b"})
        p.pending = {"a": 500, "b": 500}
        p.unbooked = {"a": 500, "b": 500}
        p.on_event(desk, {"kind": "gig_booked", "at": 510, "gigId": "g"})
        self.assertFalse(p.pending)
        self.assertFalse(p.unbooked)
        self.assertEqual(p.town["yard"]["ok"], 2)

    def test_telemetry_uses_only_order_win_state_then_adds_labels(self):
        p = PolicyV4({"recordTelemetry": True})
        p.homes = {"yard": 30}
        desk = Desk(Api(), "w", 8, 500, p)
        desk.requests["a"] = request("a")
        won = order("a")
        p.on_event(desk, {"kind": "order_won", "at": 500, "order": won})
        row = p.booking_rows["a"]
        self.assertEqual(row["leadMinutes"], 600)
        self.assertEqual(row["nearbyDriverHomes"], 30)
        self.assertIsNone(row["bookedAt"])
        desk.gig_info["g"] = {"orderId": "a", "orderIds": ["a"], "firstFee": 7000, "cars": 1}
        p.on_event(desk, {"kind": "gig_booked", "at": 510, "gigId": "g"})
        self.assertEqual(row["bookedAt"], 510)
        self.assertTrue(row["bookedBeforeWarning"])

    def test_dealer_state_survives_and_reacts(self):
        m = CompModel()
        before = m.p_win(500, [0.3], "dealer")[0]
        m.observe(500, 0.3, True, "dealer")
        self.assertGreater(m.p_win(500, [0.3], "dealer")[0], before)
        state = m.get_state()
        restored = CompModel()
        restored.set_state(state)
        self.assertEqual(restored.dealer, m.dealer)


    def test_lane_offset_raises_the_quote_on_a_dear_lane(self):
        base = PolicyV4(PolicyV4.SAFE_DEPLOYMENT)
        lanes = PolicyV4({**PolicyV4.SAFE_DEPLOYMENT, "laneScale": 1.0})
        lanes.lanes = {"pickup": {"yard": 0.0}, "delivery": {"north": 0.15}}
        for p in (base, lanes):
            p.homes = {"yard": 30}
        r = request("r1")
        self.assertEqual(base.lane_offset(r), 0.0)
        self.assertAlmostEqual(lanes.lane_offset(r), 0.15)
        self.assertGreater(lanes.quote(None, r)["next_day"], base.quote(None, r)["next_day"])
        # the live model must learn the price net of the known lane part
        import math
        quoted = lanes.quote(None, request("r2"))["next_day"] / 10000
        self.assertAlmostEqual(lanes.quoted["r2"]["next_day"], math.log(quoted) - 0.15, places=2)

    def test_trust_bonus_lowers_an_early_quote_until_the_dealer_is_at_the_cap(self):
        desk = type("FakeDesk", (), {"orders": {}})()
        base = PolicyV4(PolicyV4.SAFE_DEPLOYMENT)
        invest = PolicyV4({**PolicyV4.SAFE_DEPLOYMENT, "trustBonus": 1.0})
        for p in (base, invest):
            p.homes = {"yard": 30}
        r = request("r1")
        self.assertLess(invest.quote(desk, r)["next_day"], base.quote(desk, r)["next_day"])
        invest.trust["dealer"] = 25
        self.assertEqual(invest.trust_bonus(desk, r, r["options"][0]), 0.0)
        self.assertEqual(base.trust_bonus(desk, r, r["options"][0]), 0.0)


if __name__ == "__main__":
    unittest.main()
