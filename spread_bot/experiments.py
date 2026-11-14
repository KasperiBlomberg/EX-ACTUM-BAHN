"""Repeatable, constrained ablations for the dispatch policy.

The simulator is deterministic for a seed and action sequence.  Each named configuration below
therefore runs on the same seed list; compare configurations only inside one report.  These helpers
do not run automatically--the CLI's ``--experiment`` flag is an explicit request to create worlds.
"""
from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable, Optional

from .booking_model import validate_by_seed
from .desk import play_practice
from .policy_v4 import PolicyV4

SELECTION_FLOOR = 0.93

G_CONTROL = PolicyV4.SAFE_DEPLOYMENT

# Each feature run differs from G_CONTROL in exactly one change.  ``all`` is the current policy.
EXPERIMENTS = {
    "baseline_g": G_CONTROL,
    "option_prices": {**G_CONTROL, "separateOptionPricing": True},
    "dealer_effects": {**G_CONTROL, "enableDealerEffects": True},
    "chains": {**G_CONTROL, "enableBundles": True},
    "direct_offers": {**G_CONTROL, "enableOffers": True},
    "snow": {**G_CONTROL, "enableSnow": True},
    # Quote-side additions of 14 Nov: the offline lane table and the dealer-trust investment.
    "lanes": {**G_CONTROL, "laneScale": 1.0},
    "trust_bonus": {**G_CONTROL, "trustBonus": 1.0},
    "lanes_trust": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0},
    "all": {"separateOptionPricing": True, "enableDealerEffects": True, "enableBundles": True,
            "enableOffers": True, "enableSnow": True},
}


def experiment_params(name: str, overrides: Optional[dict] = None) -> dict:
    """Parameters for a named ablation, optionally with explicit CLI overrides."""
    if name not in EXPERIMENTS:
        raise ValueError(f"Unknown experiment {name!r}; choose one of {', '.join(EXPERIMENTS)}")
    return {**EXPERIMENTS[name], **(overrides or {})}


def booking_calibration(values: Iterable[dict]) -> dict:
    """Calibration of the existing town-risk prediction against booking-before-warning labels."""
    observations = []
    for value in values:
        for row in value.get("bookingRows") or []:
            if row.get("cancelled") or row.get("bookedBeforeWarning") is None:
                continue
            p = min(1.0, max(0.0, row["townPFailAtWin"]))
            failed = 0.0 if row["bookedBeforeWarning"] else 1.0
            observations.append((p, failed))
    if not observations:
        return {"n": 0, "brier": None, "bins": []}
    bins = [[] for _ in range(5)]
    for p, failed in observations:
        bins[min(4, int(p * 5))].append((p, failed))
    return {
        "n": len(observations),
        "brier": sum((p - failed) ** 2 for p, failed in observations) / len(observations),
        "bins": [{"from": i / 5, "to": (i + 1) / 5, "n": len(items),
                  "meanPredictedFailure": statistics.mean(p for p, _ in items),
                  "observedFailure": statistics.mean(failed for _, failed in items)}
                 for i, items in enumerate(bins) if items],
    }


def offer_effectiveness(values: Iterable[dict]) -> dict:
    """Descriptive direct-offer outcomes, without assigning app bookings to an offer.

    An accepted answer is observable.  A later gig booking is not necessarily caused by the offer,
    because the same gig remains visible in the app, so it is intentionally not treated as an
    acceptance proxy here.
    """
    rows = [row for value in values for row in (value.get("offerRows") or [])]
    answered = [row for row in rows if row.get("answer") is not None]
    accepted = [row for row in answered if row["answer"] == "accepted"]
    completed = [outcome for row in accepted for outcome in row.get("orderOutcomes", {}).values()
                 if outcome.get("onTime") is not None]
    return {
        "sent": len(rows), "answered": len(answered), "accepted": len(accepted),
        "answerRate": (len(answered) / len(rows)) if rows else None,
        "acceptanceRate": (len(accepted) / len(answered)) if answered else None,
        "acceptedOrderOutcomes": len(completed),
        "acceptedOrderOnTimeShare": (sum(outcome["onTime"] for outcome in completed) / len(completed)) if completed else None,
    }


def summarize(rows: Iterable[dict], floor: float = SELECTION_FLOOR) -> dict:
    """Metrics used to select an experiment, grouped by named configuration."""
    grouped: dict = {}
    for row in rows:
        if row.get("error"):
            continue
        grouped.setdefault(row["experiment"], []).append(row)
    summary = {}
    for name, values in grouped.items():
        margins = [v["marginCents"] / 100 for v in values]
        on_time = [v["onTimeShare"] for v in values]
        min_on_time = min(on_time)
        summary[name] = {
            "runs": len(values),
            "meanMarginEuros": round(statistics.mean(margins), 2),
            "medianMarginEuros": round(statistics.median(margins), 2),
            "minOnTimeShare": min_on_time,
            "meanOnTimeShare": statistics.mean(on_time),
            "rankedRuns": sum(v["ranked"] for v in values),
            "meanWonOrders": round(statistics.mean(v["wonOrders"] for v in values), 1),
            "eligible": min_on_time >= floor,
            "bookingCalibration": booking_calibration(values),
            "bookingModelValidation": validate_by_seed(values),
            "offerEffectiveness": offer_effectiveness(values),
        }
    return summary


def open_practice_slots(api) -> int:
    """Available practice slots, leaving other callers' live worlds alone."""
    try:
        worlds = api.req("GET", "/v1/worlds")
    except (AttributeError, TypeError):  # small offline fakes do not implement raw requests
        return 8
    active = sum(1 for world in worlds if world["kind"] == "practice" and not world["ended"])
    return max(0, 8 - active)


def run_experiments(api, seeds: list[str], level: int, names: list[str], output: str,
                    overrides: Optional[dict] = None, workers: int = 1) -> dict:
    """Run named configurations on common seeds, save every raw result, and return the report."""
    unknown = [name for name in names if name not in EXPERIMENTS]
    if unknown:
        raise ValueError(f"Unknown experiment(s): {', '.join(unknown)}")
    if not seeds:
        raise ValueError("Give at least one seed")
    if workers < 1:
        raise ValueError("workers must be positive")

    work = [(name, seed) for name in names for seed in seeds]
    rows: list[dict] = []
    in_flight: dict = {}
    report_lock = Lock()
    slots = open_practice_slots(api)
    if slots == 0:
        raise RuntimeError("No practice slots are free; finish or delete a practice world before starting an experiment")
    workers = min(workers, slots, len(work))

    def report() -> dict:
        return {
            "createdAt": datetime.now(timezone.utc).isoformat(), "level": level, "seeds": seeds,
            "experiments": names, "selectionFloor": SELECTION_FLOOR, "workers": workers,
            "results": list(rows), "summary": summarize(rows), "inFlight": list(in_flight.values()),
        }

    def checkpoint() -> None:
        # A practice suite can outlive a terminal or agent session.  Persist every completed
        # world, not only the all-green final result, so an interrupted run is still useful.
        with report_lock:
            Path(output).write_text(json.dumps(report(), indent=2) + "\n")

    def one(name: str, seed: str) -> dict:
        params = experiment_params(name, overrides)
        policy_params = {**params, "recordTelemetry": True}
        world = None
        try:
            world = api.create(level, seed)
            with report_lock:
                in_flight[world["worldId"]] = {"experiment": name, "seed": seed, "worldId": world["worldId"], "params": params}
                Path(output).write_text(json.dumps(report(), indent=2) + "\n")
            desk, score, _ = play_practice(api, world["worldId"], level, PolicyV4(policy_params))
            row = {
                "experiment": name, "seed": seed, "worldId": world["worldId"],
                "params": params, "marginCents": score["marginCents"], "onTimeShare": score["onTimeShare"],
                "wonOrders": score["wonOrders"], "ranked": score["ranked"], "undeliveredShare": score["undeliveredShare"],
                "policyStats": desk.policy.stats, "bookingRows": desk.policy.booking_training_rows(),
                "offerRows": desk.policy.offer_training_rows(),
            }
            print(f"{name:<15} {seed:<8} margin €{score['marginCents'] / 100:>8.0f}  "
                  f"{100 * score['onTimeShare']:>5.1f}% on time  {score['wonOrders']:>4} orders", flush=True)
            return row
        except Exception as exc:
            # A failed run must not consume one of the eight practice slots.
            if world is not None:
                try:
                    api.delete(world["worldId"])
                except Exception:
                    pass
            return {"experiment": name, "seed": seed, "worldId": world["worldId"] if world else None,
                    "params": params, "error": repr(exc)}

    checkpoint()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, name, seed) for name, seed in work]
        for future in as_completed(futures):
            row = future.result()
            with report_lock:
                rows.append(row)
                if row.get("worldId"):
                    in_flight.pop(row["worldId"], None)
                Path(output).write_text(json.dumps(report(), indent=2) + "\n")
            if row.get("error"):
                print(f"{row['experiment']:<15} {row['seed']:<8} ERROR {row['error']}", flush=True)
    with report_lock:
        final_report = report()
        Path(output).write_text(json.dumps(final_report, indent=2) + "\n")
    print(f"Wrote {output}", flush=True)
    return final_report
