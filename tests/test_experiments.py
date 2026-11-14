"""Offline checks for named policy ablations and report aggregation."""
from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from spread_bot.experiments import EXPERIMENTS, booking_calibration, experiment_params, open_practice_slots, run_experiments, summarize
from spread_bot.policy_v4 import PolicyV4


class ExperimentTests(unittest.TestCase):
    def test_baseline_disables_every_new_feature(self):
        params = experiment_params("baseline_g")
        self.assertFalse(params["separateOptionPricing"])
        self.assertFalse(params["enableDealerEffects"])
        self.assertFalse(params["enableBundles"])
        self.assertFalse(params["enableOffers"])
        self.assertFalse(params["enableSnow"])
        self.assertEqual(PolicyV4(params).comp.c["dealerCap"], 0.0)

    def test_each_single_feature_ablation_changes_only_one_switch(self):
        base = experiment_params("baseline_g")
        for name in ("option_prices", "dealer_effects", "chains", "direct_offers", "snow"):
            changed = experiment_params(name)
            differences = {key for key in set(base) | set(changed) if base.get(key) != changed.get(key)}
            self.assertEqual(len(differences), 1, name)

    def test_overrides_are_applied_without_mutating_named_configs(self):
        params = experiment_params("chains", {"bundleWait": 15})
        self.assertEqual(params["bundleWait"], 15)
        self.assertNotIn("bundleWait", EXPERIMENTS["chains"])

    def test_summary_uses_worst_seed_on_time_share(self):
        rows = [
            {"experiment": "chains", "marginCents": 100000, "onTimeShare": 0.96, "wonOrders": 30, "ranked": True},
            {"experiment": "chains", "marginCents": 200000, "onTimeShare": 0.93, "wonOrders": 40, "ranked": True},
            {"experiment": "chains", "seed": "bad", "error": "network"},
        ]
        result = summarize(rows)["chains"]
        self.assertEqual(result["runs"], 2)
        self.assertEqual(result["meanMarginEuros"], 1500)
        self.assertEqual(result["minOnTimeShare"], 0.93)
        self.assertEqual(result["rankedRuns"], 2)
        self.assertTrue(result["eligible"])

    def test_booking_calibration_uses_only_labelled_non_cancelled_orders(self):
        calibration = booking_calibration([{"bookingRows": [
            {"townPFailAtWin": 0.2, "bookedBeforeWarning": True, "cancelled": False},
            {"townPFailAtWin": 0.6, "bookedBeforeWarning": False, "cancelled": False},
            {"townPFailAtWin": 0.9, "bookedBeforeWarning": None, "cancelled": False},
            {"townPFailAtWin": 0.9, "bookedBeforeWarning": False, "cancelled": True},
        ]}])
        self.assertEqual(calibration["n"], 2)
        self.assertAlmostEqual(calibration["brier"], (0.2 ** 2 + 0.4 ** 2) / 2)
        self.assertEqual([item["n"] for item in calibration["bins"]], [1, 1])

    def test_runner_checkpoints_rows_and_enables_telemetry(self):
        class Api:
            def __init__(self):
                self.n = 0

            def create(self, _level, _seed):
                self.n += 1
                return {"worldId": f"w{self.n}"}

            def delete(self, _world_id):
                return None

        in_flight_seen = []

        def play(_api, world_id, _level, policy):
            self.assertTrue(policy.params["recordTelemetry"])
            in_flight_seen.append(json.loads(output.read_text())["inFlight"])
            desk = type("Desk", (), {"policy": policy})()
            policy.booking_rows = {world_id: {"orderId": world_id, "onTime": True}}
            return desk, {"marginCents": 100000, "onTimeShare": 0.95, "wonOrders": 20,
                          "ranked": True, "undeliveredShare": 0.0}, []

        with TemporaryDirectory() as folder, patch("spread_bot.experiments.play_practice", play):
            output = Path(folder) / "report.json"
            report = run_experiments(Api(), ["one", "two"], 8, ["baseline_g"], str(output))
            self.assertEqual(len(report["results"]), 2)
            self.assertTrue(output.is_file())
            self.assertIn("bookingRows", output.read_text())
            self.assertTrue(all(items for items in in_flight_seen))
            self.assertFalse(report["inFlight"])

    def test_runner_respects_existing_practice_slots_and_records_create_errors(self):
        class FullApi:
            def req(self, _method, _path):
                return [{"kind": "practice", "ended": False}] * 3

            def create(self, _level, _seed):
                raise RuntimeError("create refused")

        self.assertEqual(open_practice_slots(FullApi()), 5)
        with TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            report = run_experiments(FullApi(), ["one"], 8, ["baseline_g"], str(output), workers=8)
            self.assertEqual(report["workers"], 1)
            self.assertIn("create refused", report["results"][0]["error"])
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
