"""Entry point (port of src/main.js). Python 3.9+, no dependencies.

  python -m spread_bot --level 8 --seed v-1                   # one practice week
  python -m spread_bot --seeds v-1,v-2,v-3                    # several weeks in parallel + total
  python -m spread_bot --seed v-1 --params '{"backlogB":12}'  # override settings
  python -m spread_bot --level 8 --board                      # next ranking run (it counts!)
  caffeinate -i python -m spread_bot --final                  # test final / final: waits, then plays
  python -m spread_bot --seed v-1 --starter                   # the kit starter's decisions, for checking the desk

SPREAD_URL and SPREAD_KEY come from the environment or from a .env file in the working directory or
the project folder."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import geo
from .api import Api, SpreadError
from .desk import Desk, play_practice
from .policy_v4 import PolicyV4

DAY = 1440
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_dotenv() -> None:
    for folder in (Path.cwd(), Path(__file__).resolve().parent.parent):
        f = folder / ".env"
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))
        return


def report(label: str, s: dict) -> None:
    print(f"{label:<9} margin €{s['marginCents'] / 100:>11.2f}  {s['wonOrders']:>4} orders  "
          f"{100 * s['onTimeShare']:.1f}% on time  {100 * s['undeliveredShare']:.1f}% not delivered", flush=True)


def make_policy(args):
    return None if args.starter else PolicyV4(args.params)


# several seeds in parallel (max 8 practice slots): --seeds v-1,v-2,... prints one line per seed and the total
def sweep_seeds(api: Api, seeds: list, level: int, args) -> None:
    out: list = []

    def one(seed: str) -> None:
        w = api.create(level, seed)
        try:
            desk, score, _ = play_practice(api, w["worldId"], level, make_policy(args))
            out.append({"seed": seed, "score": score, "stats": getattr(desk.policy, "stats", None)})
            print(f"{seed:<8} margin €{score['marginCents'] / 100:>7.0f}  {score['wonOrders']:>4} orders  {100 * score['onTimeShare']:.1f}% on time", flush=True)
        except Exception as e:
            print(seed, repr(e), file=sys.stderr, flush=True)
            try:
                api.delete(w["worldId"])
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=min(8, len(seeds))) as pool:
        list(pool.map(one, seeds))
    if not out:
        return
    tot = sum(r["score"]["marginCents"] for r in out) / 100
    print(f"TOTAL €{tot:.0f}  avg €{tot / len(out):.0f}  min on time {100 * min(r['score']['onTimeShare'] for r in out):.1f}%  "
          f"ranked {sum(1 for r in out if r['score']['ranked'])}/{len(out)}")


def practice(api: Api, args) -> None:
    if args.seeds:
        return sweep_seeds(api, args.seeds.split(","), args.level, args)
    w = api.create(args.level, args.seed, args.board)
    print(f"{'Ranking run' if args.board else 'Practice world'} {w['worldId']}, level {args.level}", flush=True)
    try:
        _, score, _ = play_practice(api, w["worldId"], args.level, make_policy(args))
    except BaseException as e:   # Ctrl-C or an error: free the slot (a ranking run plays on and counts)
        if not args.board:
            try:
                api.delete(w["worldId"])
            except Exception:
                pass
        if isinstance(e, KeyboardInterrupt):
            sys.exit("Stopped.")
        raise
    report("End", score)
    print("Ranked." if score["ranked"] else "Not ranked.")


# ---------------------------------------------------------------- the final: poll events, act at once
def next_final(api: Api) -> dict:
    said = False
    while True:
        try:
            f = api.final()
            snap = api.snapshot(f["worldId"])
            if snap["now"] < snap["endsAt"]:
                return f
        except SpreadError as e:
            if e.status != 404:
                print(str(e), file=sys.stderr, flush=True)
        if not said:
            print("No final is running yet; waiting (it is set 15 min before a test final, 30 min before the final).", flush=True)
            said = True
        time.sleep(10)


def play_final(api: Api, args) -> None:
    f = next_final(api)
    world_id = f["worldId"]
    print(f"Final world {world_id}, starts {f['startsAt']}", flush=True)
    policy = PolicyV4(args.params)
    # restore the policy's learnt state after a restart of this program
    saved: Optional[dict] = None
    try:
        saved = json.loads(Path(args.state).read_text())
        if saved["worldId"] != world_id:
            saved = None
    except (OSError, ValueError, KeyError):
        saved = None
    history = api.events(world_id, -1)
    snap = api.snapshot(world_id)
    desk = Desk(api, world_id, 8, snap["now"], policy)
    policy.init(desk)
    if saved:
        policy.set_state(saved["policy"])
    desk.rejoin(snap, history, not saved)
    desk.send()
    after = history[-1]["seq"] if history else -1
    last_day, last_save = desk.now // DAY, 0.0
    seen = {e["seq"] for e in history}
    while desk.now < snap["endsAt"]:
        try:
            evs = api.events(world_id, after)
        except Exception as e:
            print("events:", str(e), file=sys.stderr, flush=True)
            time.sleep(1)
            continue
        for e in evs:
            if e["seq"] in seen:
                continue
            seen.add(e["seq"])
            after = max(after, e["seq"])
            desk.now = max(desk.now, e["at"])
            try:
                desk.handle(e)
            except Exception as err:
                print("handle", e["kind"], repr(err), file=sys.stderr, flush=True)
        try:
            desk.send()
        except Exception as e:
            print("send:", str(e), file=sys.stderr, flush=True)
        if not evs:
            time.sleep(0.3)
            try:
                desk.now = max(desk.now, api.final()["now"])
            except Exception:
                pass
        if time.time() - last_save > 2:
            last_save = time.time()
            Path(args.state).write_text(json.dumps({"worldId": world_id, "policy": policy.get_state()}))
        d = desk.now // DAY
        if d > last_day:
            last_day = d
            try:
                report(WEEKDAYS[(d - 1) % 7], api.score(world_id))
            except Exception:
                pass
    report("End", api.score(world_id))


def main() -> None:
    ap = argparse.ArgumentParser(description="Plays a Spread world with the v4 policy. Set SPREAD_URL and SPREAD_KEY (or a .env file).")
    ap.add_argument("--level", type=int, default=8, choices=range(1, 9))
    ap.add_argument("--seed", help="any text; the same seed gives the same world")
    ap.add_argument("--seeds", help="comma-separated seeds to play in parallel (at most 8 at once)")
    ap.add_argument("--board", action="store_true", help="play your next ranking run; it counts")
    ap.add_argument("--final", action="store_true", help="play your copy of the final")
    ap.add_argument("--params", type=json.loads, default={}, help='JSON overrides, e.g. \'{"backlogB":12,"maxKm":550}\'')
    ap.add_argument("--state", default="spread-state.json", help="where the final saves what it has learnt")
    ap.add_argument("--starter", action="store_true", help="play the kit starter's decisions (no policy), to check the desk")
    args = ap.parse_args()

    load_dotenv()
    base = os.environ.get("SPREAD_URL") or "https://spread.bahnexpress.com"
    key = os.environ.get("SPREAD_KEY")
    if not key:
        sys.exit("Set SPREAD_KEY")
    api = Api(base, key)
    geo.load(api)
    if args.final:
        while True:
            try:
                play_final(api, args)
                break
            except KeyboardInterrupt:
                sys.exit("Stopped. The final world keeps running without your system; start again with --final.")
            except Exception as e:
                print("final loop:", repr(e), "- retrying in 5 s", file=sys.stderr, flush=True)
                time.sleep(5)
    else:
        practice(api, args)


if __name__ == "__main__":
    main()
