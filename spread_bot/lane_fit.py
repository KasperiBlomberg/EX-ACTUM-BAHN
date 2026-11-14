"""Fit the part of the competitor's lane factor that holds in every world.

The handbook says the competitor's price is the list price times a lane factor, a day factor, the
world's level and a small request factor, and that one part of the lane factor "is almost the same
in every world".  No reveal shows the competitor's price, but every ended practice world reveals
our quotes and whether each won.  Over many worlds that is enough to fit, offline:

    P(win) = sigmoid(k * (a[seed, day] + pickup[p] + delivery[d] - x))
    x      = log(quote / list) - log(1 + trust / 100)

``a`` absorbs each world's level and day factors, so ``pickup`` and ``delivery`` keep only what
repeats across worlds.  The policy reads the table from ``lanes.json`` (``laneScale``).

  python -m spread_bot.lane_fit --fetch reveals/            # download compact rows, once per world
  python -m spread_bot.lane_fit --fit reveals/ --holdout    # held-out-seed check, then write lanes.json

Pure Python, no dependencies; 170,000 quotes fit in about 15 seconds.
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import math
import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

K = 11.0                       # the slope the live competitor model uses
LANES_FILE = Path(__file__).with_name("lanes.json")


def sig(v: float) -> float:
    return 1 / (1 + math.exp(-v)) if v > -700 else 0.0


# ---------------------------------------------------------------- download
def fetch(api, out: str) -> None:
    """Save every ended level-8 practice world's quotes and outcomes as compact rows."""
    os.makedirs(out, exist_ok=True)
    worlds = [w for w in api.req("GET", "/v1/worlds") if w["kind"] == "practice" and w["ended"]
              and w["level"] == 8 and w["season"] is None and w["parent"] is None]

    def one(w: dict) -> None:
        path = f"{out}/{w['worldId']}.json"
        if os.path.exists(path):
            return
        try:
            r = api.reveal(w["worldId"])
        except Exception as e:
            print("ERR", w["worldId"], repr(e)[:120], flush=True)
            return
        reqs = r["requests"]
        # The list of worlds does not give the seed; the same seed has the same first requests.
        fp = hashlib.md5(json.dumps([(x["request"]["id"], x["request"]["dealerId"], x["request"]["arrivedAt"],
                                      x["request"]["options"][0]["listPriceCents"]) for x in reqs[:40]]).encode()).hexdigest()[:10]
        delivered = {}
        for o in r["orders"]:
            d = [leg["deliveredAt"] for leg in o["legs"]]
            nd = [leg["noteDeadline"] for leg in o["legs"] if leg["noteDeadline"] is not None]
            delivered[o["orderId"]] = (None if any(x is None for x in d) else max(d), min(nd) if nd else None)
        rows = []
        for x in reqs:
            q = x["request"]
            l0 = q["legs"][0]
            dl = delivered.get(q["id"])
            rows.append({"id": q["id"], "dealer": q["dealerId"], "at": q["arrivedAt"],
                         "pu": l0["pickup"]["placeId"], "de": l0["delivery"]["placeId"], "legs": len(q["legs"]),
                         "priv": any(leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"] for leg in q["legs"]),
                         "opts": {o["kind"]: (o["listPriceCents"], o["deadline"]) for o in q["options"]},
                         "quote": x["quote"], "outcome": x["outcome"], "option": x["option"], "ref": q["referenceFeeCents"],
                         "deliveredAt": dl[0] if dl else None, "noteDeadline": dl[1] if dl else None})
        Path(path).write_text(json.dumps({"worldId": w["worldId"], "fp": fp, "rows": rows}))
        print("ok", w["worldId"], fp, len(rows), flush=True)

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(one, worlds))
    print("worlds saved:", len(os.listdir(out)))


# ---------------------------------------------------------------- rows
def load(src: str) -> list:
    """One row per quote we really made: seed, day, lane, trust-adjusted log price, won or lost."""
    rows = []
    for path in sorted(glob.glob(src + "/*.json")):
        w = json.loads(Path(path).read_text())
        rs = sorted(w["rows"], key=lambda r: r["at"])
        if not any(r["outcome"] == "won" for r in rs):
            continue
        # dealer trust as the world counted it, approximately: +1 per on-time order, halved when late
        dels = []
        for r in rs:
            if r["outcome"] == "won" and r["deliveredAt"] is not None and r["option"] in r["opts"]:
                dl = r["opts"][r["option"]][1]
                if r["noteDeadline"] is not None:
                    dl = min(dl, r["noteDeadline"])
                dels.append((r["deliveredAt"], r["dealer"], r["deliveredAt"] <= dl))
        dels.sort()
        trust, i = collections.defaultdict(int), 0
        for r in rs:
            while i < len(dels) and dels[i][0] <= r["at"] + 15:
                _, dealer, ok = dels[i]
                trust[dealer] = min(25, trust[dealer] + 1) if ok else trust[dealer] // 2
                i += 1
            if r["outcome"] == "unquoted" or not r["quote"]:
                continue
            us = {k: r["quote"][k] / r["opts"][k][0] for k in r["quote"] if k in r["opts"]}
            us = {k: u for k, u in us.items() if u < 3.0}       # 5x list is our way to decline
            if not us:
                continue
            if r["outcome"] == "won":
                if r["option"] not in us:
                    continue
                u, y = us[r["option"]], 1
            else:
                u, y = min(us.values()), 0
            t = 1 + 0.01 * trust[r["dealer"]]
            rows.append({"fp": w["fp"], "g": (w["fp"], r["at"] // 1440), "p": r["pu"], "d": r["de"],
                         "x": math.log(u / t), "y": y, "w": 1.0 if len(us) == 1 or y else 0.55})
    return rows


# ---------------------------------------------------------------- fit
def fit(rows: list, lam_place: float = 4.0, fixed=None, groups_only: bool = False, sweeps: int = 14):
    """Block-coordinate Newton steps with a ridge on the places.

    ``fixed`` = (pickup, delivery) holds a lane table still and fits only the seed-day levels;
    ``groups_only`` fits the levels with no lane table (the model the bot had before).
    """
    a: dict = collections.defaultdict(float)
    pickup: dict = collections.defaultdict(float)
    delivery: dict = collections.defaultdict(float)
    if fixed:
        pickup.update(fixed[0])
        delivery.update(fixed[1])

    def step(key, table: dict, lam: float) -> None:
        g: dict = collections.defaultdict(float)
        h: dict = collections.defaultdict(float)
        for r in rows:
            p = sig(K * (a[r["g"]] + pickup[r["p"]] + delivery[r["d"]] - r["x"]))
            kk = key(r)
            g[kk] += r["w"] * K * (r["y"] - p)
            h[kk] += r["w"] * K * K * max(p * (1 - p), 1e-4)
        for kk in g:
            delta = (g[kk] - lam * table[kk]) / (h[kk] + lam)
            table[kk] += max(-0.15, min(0.15, delta))

    for _ in range(sweeps):
        step(lambda r: r["g"], a, 0.5)
        if fixed or groups_only:
            continue
        step(lambda r: r["p"], pickup, lam_place * K * K * 0.25)
        step(lambda r: r["d"], delivery, lam_place * K * K * 0.25)
    return a, pickup, delivery


def logloss(rows: list, a: dict, pickup: dict, delivery: dict) -> float:
    s = 0.0
    for r in rows:
        p = sig(K * (a.get(r["g"], 0.0) + pickup.get(r["p"], 0.0) + delivery.get(r["d"], 0.0) - r["x"]))
        p = min(1 - 1e-6, max(1e-6, p))
        s -= math.log(p) if r["y"] else math.log(1 - p)
    return s / len(rows)


def holdout(rows: list, seed: int = 7) -> dict:
    """Fit the table on three quarters of the seeds; score it on quotes from the other seeds.

    In a held-out seed the world's own levels are learnt from half of its quotes (as the bot does
    live) and the log loss is measured on the other half, with and without the lane table.
    """
    fps = sorted({r["fp"] for r in rows})
    rng = random.Random(seed)
    test_fps = set(rng.sample(fps, max(2, len(fps) // 4)))
    train = [r for r in rows if r["fp"] not in test_fps]
    test = [r for r in rows if r["fp"] in test_fps]
    rng.shuffle(test)
    calib, score = test[::2], test[1::2]
    _, pickup, delivery = fit(train)
    a1, _, _ = fit(calib, fixed=(pickup, delivery))
    a0, _, _ = fit(calib, groups_only=True)
    return {"heldOutSeeds": len(test_fps), "quotes": len(score),
            "logLossWithoutLanes": round(logloss(score, a0, {}, {}), 4),
            "logLossWithLanes": round(logloss(score, a1, pickup, delivery), 4)}


def make_table(rows: list) -> dict:
    _, pickup, delivery = fit(rows)
    n_p = collections.Counter(r["p"] for r in rows)
    n_d = collections.Counter(r["d"] for r in rows)
    # centre both tables on the request mix, so the live model's world level keeps its meaning
    mp = sum(pickup[k] * n for k, n in n_p.items()) / len(rows)
    md = sum(delivery[k] * n for k, n in n_d.items()) / len(rows)
    return {"k": K, "rows": len(rows), "seeds": len({r["fp"] for r in rows}),
            "pickup": {k: round(pickup[k] - mp, 4) for k in sorted(n_p)},
            "delivery": {k: round(delivery[k] - md, 4) for k in sorted(n_d)}}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fit the competitor's lasting lane factors from practice reveals.")
    ap.add_argument("--fetch", metavar="DIR", help="download compact reveal rows of every ended level-8 practice world")
    ap.add_argument("--fit", metavar="DIR", help="fit the lane table from the rows in DIR")
    ap.add_argument("--holdout", action="store_true", help="with --fit: print the held-out-seed log loss first")
    ap.add_argument("--output", default=str(LANES_FILE), help="where --fit writes the table")
    args = ap.parse_args()
    if args.fetch:
        from .__main__ import load_dotenv
        from .api import Api
        load_dotenv()
        fetch(Api(os.environ.get("SPREAD_URL") or "https://spread.bahnexpress.com", os.environ["SPREAD_KEY"]), args.fetch)
    if args.fit:
        rows = load(args.fit)
        print("quotes", len(rows), "seeds", len({r["fp"] for r in rows}), "won", round(sum(r["y"] for r in rows) / len(rows), 3), flush=True)
        if args.holdout:
            print(holdout(rows), flush=True)
        table = make_table(rows)
        Path(args.output).write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
        print("wrote", args.output)


if __name__ == "__main__":
    main()
