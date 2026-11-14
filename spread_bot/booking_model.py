"""Small dependency-free booking-risk model for offline validation.

This is a logistic model with Gaussian (L2) priors.  Pickup, delivery, dealer and option
effects are separate, strongly regularised offsets around zero; that is the useful empirical-
Bayes / partial-pooling form for sparse towns and dealers.  It is deliberately *not* part of the
live quote path yet.  We first require it to beat the current heuristic on held-out practice
weeks and to have sensible reliability bins.
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional


NUMERIC = (
    "roadKm", "slackMinutes", "leadMinutes", "priceRatio", "backlogAtWin",
    "nearbyDriverHomes", "forecastRisk", "legs", "private", "premium", "electric",
    "manual", "plates", "summerTyres",
)
CATEGORICAL = {
    "pickup": "pickupPlaceId", "delivery": "deliveryPlaceId", "dealer": "dealerId",
    "option": "option",
}


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _numeric(row: dict, name: str) -> float:
    if name == "priceRatio":
        reference = row.get("referenceFeeCents") or 0
        return (row.get("priceCents") or 0) / reference if reference else 1.0
    value = row.get(name, 0)
    return float(value) if value is not None else 0.0


def labelled(rows: Iterable[dict]) -> list:
    """Rows whose warning label is known at the end of the practice world."""
    return [row for row in rows
            if not row.get("cancelled") and row.get("bookedBeforeWarning") is not None]


class PartialPoolingLogistic:
    """MAP logistic regression with conservative categorical shrinkage.

    There is no package dependency in the competition kit.  Batch gradient descent is sufficient
    here because a practice report holds hundreds, not millions, of booking observations.
    """

    def __init__(self, l2_numeric: float = 0.15, l2_category: float = 2.5,
                 iterations: int = 180, learning_rate: float = 0.35):
        self.l2_numeric = l2_numeric
        self.l2_category = l2_category
        self.iterations = iterations
        self.learning_rate = learning_rate
        self.means: dict = {}
        self.scales: dict = {}
        self.weights = {"intercept": 0.0, **{name: 0.0 for name in NUMERIC}}
        self.effects = {group: {} for group in CATEGORICAL}

    def _standardise(self, row: dict, name: str) -> float:
        return (_numeric(row, name) - self.means[name]) / self.scales[name]

    def score(self, row: dict) -> float:
        value = self.weights["intercept"]
        for name in NUMERIC:
            value += self.weights[name] * self._standardise(row, name)
        for group, key in CATEGORICAL.items():
            value += self.effects[group].get(row.get(key), 0.0)
        return value

    def predict_failure(self, row: dict) -> float:
        return _sigmoid(self.score(row))

    def fit(self, rows: Iterable[dict]) -> "PartialPoolingLogistic":
        data = labelled(rows)
        if not data:
            raise ValueError("Need labelled booking rows to fit the risk model")
        # Standardise only from the training set, so held-out seeds cannot leak distribution data.
        self.means = {name: statistics.mean(_numeric(row, name) for row in data) for name in NUMERIC}
        self.scales = {}
        for name in NUMERIC:
            deviation = statistics.pstdev(_numeric(row, name) for row in data)
            self.scales[name] = deviation if deviation >= 1e-6 else 1.0
        for group, key in CATEGORICAL.items():
            self.effects[group] = {row.get(key): 0.0 for row in data if row.get(key) is not None}

        # Start at the observed log-odds rather than making early steps fight a large intercept.
        failures = sum(1 for row in data if not row["bookedBeforeWarning"])
        prior = (failures + 0.5) / (len(data) + 1.0)
        self.weights["intercept"] = math.log(prior / (1.0 - prior))
        n = len(data)
        for step in range(self.iterations):
            weight_grad = {name: 0.0 for name in self.weights}
            effect_grad = {group: {category: 0.0 for category in values}
                           for group, values in self.effects.items()}
            for row in data:
                error = self.predict_failure(row) - (0.0 if row["bookedBeforeWarning"] else 1.0)
                weight_grad["intercept"] += error
                for name in NUMERIC:
                    weight_grad[name] += error * self._standardise(row, name)
                for group, key in CATEGORICAL.items():
                    category = row.get(key)
                    if category in effect_grad[group]:
                        effect_grad[group][category] += error
            rate = self.learning_rate / math.sqrt(1.0 + step / 30.0)
            self.weights["intercept"] -= rate * weight_grad["intercept"] / n
            for name in NUMERIC:
                gradient = weight_grad[name] / n + self.l2_numeric * self.weights[name]
                self.weights[name] -= rate * gradient
            for group, values in self.effects.items():
                for category, effect in values.items():
                    gradient = effect_grad[group][category] / n + self.l2_category * effect
                    self.effects[group][category] -= rate * gradient
        return self


def calibration(predictions: Iterable[tuple]) -> dict:
    """Brier score and five reliability bins for (predicted failure, actual failure)."""
    values = list(predictions)
    if not values:
        return {"n": 0, "brier": None, "bins": []}
    bins = [[] for _ in range(5)]
    for predicted, actual in values:
        predicted = min(1.0, max(0.0, predicted))
        bins[min(4, int(predicted * 5))].append((predicted, actual))
    return {
        "n": len(values),
        "brier": sum((predicted - actual) ** 2 for predicted, actual in values) / len(values),
        "bins": [
            {"from": index / 5, "to": (index + 1) / 5, "n": len(items),
             "meanPredictedFailure": statistics.mean(predicted for predicted, _ in items),
             "observedFailure": statistics.mean(actual for _, actual in items)}
            for index, items in enumerate(bins) if items
        ],
    }


def validate_by_seed(values: Iterable[dict]) -> dict:
    """Leave-one-practice-week-out validation, never training and scoring on one seed.

    ``values`` is a configuration's raw report rows, each of which owns its own booking rows.
    The output is omitted until at least two independently seeded worlds have labels.
    """
    grouped: dict = {}
    for value in values:
        seed = value.get("seed")
        if not seed:
            continue
        grouped.setdefault(seed, []).extend(labelled(value.get("bookingRows") or []))
    usable = {seed: rows for seed, rows in grouped.items() if rows}
    if len(usable) < 2:
        return {"folds": 0, "model": calibration([]), "heuristic": calibration([])}
    predicted, heuristic = [], []
    used_folds = 0
    for held_out, test_rows in usable.items():
        train_rows = [row for seed, rows in usable.items() if seed != held_out for row in rows]
        if not train_rows:
            continue
        model = PartialPoolingLogistic().fit(train_rows)
        for row in test_rows:
            actual = 0.0 if row["bookedBeforeWarning"] else 1.0
            predicted.append((model.predict_failure(row), actual))
            heuristic.append((row["townPFailAtWin"], actual))
        used_folds += 1
    return {"folds": used_folds, "model": calibration(predicted), "heuristic": calibration(heuristic)}
