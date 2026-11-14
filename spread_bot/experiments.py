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
    # Return-leg pairs on top of the deployed lanes_trust control (their control, not G).
    # ``_fee_only`` holds and pairs but quotes every request at its standalone cost.
    "return_pairs": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "enableReturnPairs": True},
    "return_pairs_fee_only": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "enableReturnPairs": True,
                              "pairQuote": False},
    # Cheap direct offers to drivers whose booked gig of ours meets an open car, on lanes_trust.
    "follow_offers": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "enableFollowOffers": True},
    # Acceptance sweep, one gate at a time against lanes_trust.  Private cars keep minSlackPriv.
    "accept_slack330": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "minSlack": 330},
    "accept_slack300": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "minSlack": 300},
    "low_trust_slack": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "minSlackLowTrust": 240},
    # Found in the ranking runs of 14 Nov, each against lanes_trust: no fee step-up on a drop
    # with time to spare; private trade-ins (typical fee 0.57 x list, all declined so far);
    # longer lanes (400-550 km earns EUR 73 an order at an ordinary late share).
    "drop_hold": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "dropHoldLead": 600},
    "tradein_private": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "tradeinPriv": True},
    "accept_km650": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "maxKm": 650},
    "accept_km600": {**G_CONTROL, "laneScale": 1.0, "trustBonus": 1.0, "maxKm": 600},
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


def _sum_counts(dicts: Iterable[Optional[dict]]) -> dict:
    """Key-wise sum of nested counter dicts."""
    total: dict = {}
    for d in dicts:
        for key, value in (d or {}).items():
            if isinstance(value, dict):
                total[key] = _sum_counts([total.get(key), value])
            else:
                total[key] = total.get(key, 0) + value
    return total


def pair_metrics(value: dict) -> dict:
    """Return-leg pair telemetry of one world; zeros for a report row written before it existed."""
    stats = value.get("pairStats") or {}
    rows = value.get("pairRows") or []
    outcomes = [on_time for row in rows if row["state"] == "booked" for on_time in row["onTime"].values()]
    return {
        "candidates": stats.get("candidates", 0), "feasible": stats.get("feasible", 0),
        "posted": stats.get("posted", 0), "booked": stats.get("booked", 0),
        "dropped": stats.get("dropped", 0), "dissolved": stats.get("dissolved", 0),
        "held": stats.get("held", 0), "holdReleased": stats.get("holdReleased", 0),
        "feeSavingEuros": stats.get("feeSavingCents", 0) / 100,
        "pairedOrdersDelivered": len(outcomes),
        "pairedOnTimeShare": (sum(outcomes) / len(outcomes)) if outcomes else None,
        "quoteDiscounted": stats.get("quoteDiscounted", 0), "quoteWins": stats.get("quoteWins", 0),
        "quoteWinsOtherwiseDeclined": stats.get("quoteWinsOtherwiseDeclined", 0),
        "quoteDiscountEuros": stats.get("quoteDiscountCents", 0) / 100,
    }


def compare(rows: Iterable[dict], control: str, candidate: str, floor: float = SELECTION_FLOOR,
            min_seeds: int = 8) -> dict:
    """Per-seed paired comparison of ``candidate`` against ``control``, with the promotion rule.

    Promote only on at least ``min_seeds`` common seeds, when the candidate's worst seed is on
    time at least ``floor``, its mean margin is higher, and the mean difference stays positive without its best seed (no single outlier).
    The added orders and margin attributable to the candidate are the paired differences.
    """
    by_seed: dict = {}
    for row in rows:
        if not row.get("error") and row["experiment"] in (control, candidate):
            by_seed.setdefault(row["seed"], {})[row["experiment"]] = row
    per_seed = []
    for seed, pair in by_seed.items():
        if len(pair) < 2:
            continue
        c, x = pair[control], pair[candidate]
        per_seed.append({
            "seed": seed,
            "controlMarginEuros": c["marginCents"] / 100, "marginEuros": x["marginCents"] / 100,
            "marginDiffEuros": (x["marginCents"] - c["marginCents"]) / 100,
            "controlWonOrders": c["wonOrders"], "wonOrders": x["wonOrders"],
            "addedOrders": x["wonOrders"] - c["wonOrders"],
            "controlMarginPerOrderEuros": c["marginCents"] / 100 / c["wonOrders"] if c["wonOrders"] else None,
            "marginPerOrderEuros": x["marginCents"] / 100 / x["wonOrders"] if x["wonOrders"] else None,
            "controlOnTimeShare": c["onTimeShare"], "onTimeShare": x["onTimeShare"],
            "pairs": pair_metrics(x),
        })
    per_seed.sort(key=lambda r: r["seed"])
    diffs = [r["marginDiffEuros"] for r in per_seed]
    if not diffs:
        return {"control": control, "candidate": candidate, "seeds": 0, "perSeed": [], "promote": False}
    mean_diff = statistics.mean(diffs)
    without_best = statistics.mean(sorted(diffs)[:-1]) if len(diffs) > 1 else None
    worst = min(r["onTimeShare"] for r in per_seed)
    return {
        "control": control, "candidate": candidate, "seeds": len(diffs), "perSeed": per_seed,
        "meanMarginDiffEuros": round(mean_diff, 2),
        "stderrMarginDiffEuros": round(statistics.stdev(diffs) / len(diffs) ** 0.5, 2) if len(diffs) > 1 else None,
        "meanDiffWithoutBestSeedEuros": round(without_best, 2) if without_best is not None else None,
        "seedsUp": sum(1 for d in diffs if d > 0),
        "meanAddedOrders": round(statistics.mean(r["addedOrders"] for r in per_seed), 1),
        "worstOnTimeShare": worst, "controlWorstOnTimeShare": min(r["controlOnTimeShare"] for r in per_seed),
        "promote": bool(len(diffs) >= min_seeds and worst >= floor and mean_diff > 0 and without_best is not None and without_best > 0),
    }


def format_comparison(result: dict) -> str:
    """The per-seed table of ``compare`` as text."""
    lines = [f"{result['candidate']} against {result['control']} ({result['seeds']} common seeds)",
             f"{'seed':<8}{'margin':>9}{'vs ctl':>8}{'orders':>8}{'vs ctl':>7}{'EUR/ord':>8}{'on time':>9}{'ctl':>7}"
             f"{'cand':>6}{'feas':>5}{'post':>5}{'book':>5}{'diss':>5}{'saved':>7}{'pOnT':>6}{'qWin':>5}"]
    for r in result["perSeed"]:
        m = r["pairs"]
        paired = "-" if m["pairedOnTimeShare"] is None else f"{100 * m['pairedOnTimeShare']:.0f}%"
        lines.append(
            f"{r['seed']:<8}{r['marginEuros']:>9.0f}{r['marginDiffEuros']:>+8.0f}{r['wonOrders']:>8}{r['addedOrders']:>+7}"
            f"{r['marginPerOrderEuros'] or 0:>8.1f}{100 * r['onTimeShare']:>8.1f}%{100 * r['controlOnTimeShare']:>6.1f}%"
            f"{m['candidates']:>6}{m['feasible']:>5}{m['posted']:>5}{m['booked']:>5}{m['dissolved']:>5}"
            f"{m['feeSavingEuros']:>7.0f}{paired:>6}{m['quoteWins']:>5}")
    if result["seeds"]:
        lines.append(
            f"mean margin difference EUR {result['meanMarginDiffEuros']:+.0f} (s.e. {result['stderrMarginDiffEuros']}), "
            f"without the best seed EUR {result['meanDiffWithoutBestSeedEuros']}, {result['seedsUp']}/{result['seeds']} seeds up, "
            f"orders {result['meanAddedOrders']:+.1f}, worst on time {100 * result['worstOnTimeShare']:.1f}% "
            f"(control {100 * result['controlWorstOnTimeShare']:.1f}%) -> {'PROMOTE' if result['promote'] else 'do not promote'}")
    return "\n".join(lines)


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
            "returnPairs": _sum_counts(
                {k: v for k, v in pair_metrics(value).items() if k != "pairedOnTimeShare"} for value in values),
            "declineReasons": _sum_counts(value.get("declineReasons") for value in values),
            "followOffers": _sum_counts(value.get("followOffers") for value in values),
        }
        paired = summary[name]["returnPairs"]
        on_time_pairs = sum((pair_metrics(v)["pairedOnTimeShare"] or 0) * pair_metrics(v)["pairedOrdersDelivered"] for v in values)
        paired["pairedOnTimeShare"] = (on_time_pairs / paired["pairedOrdersDelivered"]) if paired.get("pairedOrdersDelivered") else None
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
            # With several configurations in one run, the first is the control of the others.
            "comparisons": [compare(rows, names[0], name) for name in names[1:]],
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
                "declineReasons": {**desk.policy.decline_reasons, "nearMiss": desk.policy.decline_near},
                "pairStats": desk.policy.pair_stats, "pairRows": list(desk.policy.pairs.values()),
                "followOffers": desk.policy.follow_stats,
            }
            pairs = desk.policy.pair_stats
            print(f"{name:<15} {seed:<8} margin €{score['marginCents'] / 100:>8.0f}  "
                  f"{100 * score['onTimeShare']:>5.1f}% on time  {score['wonOrders']:>4} orders"
                  + (f"  pairs {pairs['booked']}/{pairs['posted']} booked" if pairs["posted"] else ""), flush=True)
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
    for result in final_report["comparisons"]:
        print(format_comparison(result), flush=True)
    print(f"Wrote {output}", flush=True)
    return final_report
