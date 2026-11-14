"""v2 policy (port of src/policy_v2.js): v1's selection + live Bayesian competitor model (world
level x day factor, price-war prior) + dealer trust + expected-margin-optimal quotes.
The competitor model is also what v4 uses."""
from __future__ import annotations

import math
from typing import Optional

from .desk import round_half_up, steep
from .geo import road


def sig(x: float) -> float:
    if x < -700:   # JS gives 0 here; Python's exp would overflow
        return 0.0
    return 1 / (1 + math.exp(-x))


class CompModel:
    """Market posterior with a global world/day price and a small dealer correction.

    The global grid learns facts shared by all dealers.  A dealer gets a short-lived correction as
    well: the competitor cuts that dealer after we win a lot and eases off after we lose.  Keeping
    this separate prevents one dealer's feedback loop from moving the price we quote to everybody.
    """

    DEFAULTS = {"lo": 1.05, "hi": 1.95, "nL": 37, "dLo": -0.40, "dHi": 0.35, "nD": 31, "daySd": 0.12, "war": 0.15, "k": 11, "temper": 0.8,
                # Per-dealer terms are deliberately much smaller than the day factor.  They learn
                # within a day and then anticipate the competitor's next-morning response.
                "dealerEta": 0.018, "dealerCarry": 0.65, "dealerReact": 0.045, "dealerCap": 0.16}

    def __init__(self, **o):
        c = self.c = {**self.DEFAULTS, **o}
        self.L = [math.log(c["lo"]) + (math.log(c["hi"]) - math.log(c["lo"])) * i / (c["nL"] - 1) for i in range(c["nL"])]
        self.D = [c["dLo"] + (c["dHi"] - c["dLo"]) * i / (c["nD"] - 1) for i in range(c["nD"])]
        self.log_l = [0.0] * c["nL"]
        self.log_j: Optional[list] = None
        self.day: Optional[int] = None
        self.war_seen = False
        self.dealer: dict = {}       # dealer -> {z, wins, n}; z is a log competitor-price offset

    def day_prior(self) -> list:
        c = self.c
        w = 0.02 if self.war_seen else c["war"]
        return [math.log((1 - w) * math.exp(-0.5 * (d / c["daySd"]) ** 2) / c["daySd"] + w * math.exp(-0.5 * ((d + 0.223) / 0.04) ** 2) / 0.04) for d in self.D]

    @staticmethod
    def norm(a: list) -> list:
        m = max(v for r in a for v in r)
        return [[v - m for v in r] for r in a]

    def at(self, minute: int) -> None:
        d = minute // 1440
        if d == self.day:
            return
        if self.log_j is not None:
            # carry the world level; note whether today looked like the price war
            dm = self.day_marginal()
            war = sum(dm[j] for j, dv in enumerate(self.D) if dv < -0.15) > 0.6
            if war:
                self.war_seen = True   # a price-war day says little about the usual level: keep the old one
            else:
                self.log_l = [self.c["temper"] * (max(r) + math.log(sum(math.exp(v - max(r)) for v in r))) for r in self.log_j]
            # The competitor reacts to our recent share at each dealer every morning.  Retain a
            # little of the inferred local level, then move it in the documented opposite
            # direction of yesterday's win share.
            for st in self.dealer.values():
                share = (st["wins"] / st["n"] - 0.5) * 2 if st["n"] else 0.0
                st["z"] = self.clip(st["z"] * self.c["dealerCarry"] - self.c["dealerReact"] * share)
                st["wins"], st["n"] = 0, 0
        self.day = d
        dp = self.day_prior()
        self.log_j = self.norm([[self.log_l[i] + dp[j] for j in range(len(self.D))] for i in range(len(self.L))])

    def clip(self, z: float) -> float:
        return max(-self.c["dealerCap"], min(self.c["dealerCap"], z))

    def dealer_state(self, dealer_id: Optional[str]) -> dict:
        if not dealer_id:
            return {"z": 0.0, "wins": 0, "n": 0}
        return self.dealer.setdefault(dealer_id, {"z": 0.0, "wins": 0, "n": 0})

    def weights(self) -> list:
        e = [[math.exp(v) for v in r] for r in self.log_j]
        s = 0.0
        for r in e:
            for v in r:
                s += v
        return [[v / s for v in r] for r in e]

    def day_marginal(self) -> list:
        w = self.weights()
        return [sum(r[j] for r in w) for j in range(len(self.D))]

    def observe(self, minute: int, x: float, won: bool, dealer_id: Optional[str] = None, weight: float = 1.0) -> None:
        self.at(minute)
        k, L, D = self.c["k"], self.L, self.D
        st = self.dealer_state(dealer_id)
        z = st["z"]
        predicted = self.p_win(minute, [x], dealer_id)[0]
        out = []
        for i, r in enumerate(self.log_j):
            row = []
            for j, v in enumerate(r):
                p = min(1 - 1e-6, max(1e-6, sig(k * (L[i] + D[j] + z - x))))
                row.append(v + weight * math.log(p if won else 1 - p))
            out.append(row)
        self.log_j = self.norm(out)

        # A gradient step on the local log-price term makes the correction react after only a few
        # requests.  The global posterior above still gets the full, correctly adjusted evidence.
        st["z"] = self.clip(z + self.c["dealerEta"] * weight * ((1.0 if won else 0.0) - predicted))
        st["wins"] += weight if won else 0
        st["n"] += weight

    def p_win(self, minute: int, xs: list, dealer_id: Optional[str] = None) -> list:
        """xs: log(price/list) for each candidate; returns P(win) for each."""
        self.at(minute)
        w = self.weights()
        k, L, D = self.c["k"], self.L, self.D
        z = self.dealer_state(dealer_id)["z"]
        out = []
        for x in xs:
            p = 0.0
            for i in range(len(L)):
                for j in range(len(D)):
                    if w[i][j] > 1e-7:
                        p += w[i][j] * sig(k * (L[i] + D[j] + z - x))
            out.append(p)
        return out

    def get_state(self) -> dict:
        return {"logL": self.log_l, "logJ": self.log_j, "day": self.day, "warSeen": self.war_seen, "dealer": self.dealer}

    def set_state(self, st: dict) -> None:
        self.log_l, self.log_j, self.day, self.war_seen = st["logL"], st["logJ"], st["day"], st["warSeen"]
        self.dealer = st.get("dealer") or {}

    def summary(self, minute: int) -> dict:
        self.at(minute)
        w = self.weights()
        el = ed = 0.0
        for i, r in enumerate(w):
            for j, v in enumerate(r):
                el += v * self.L[i]
                ed += v * self.D[j]
        dm = self.day_marginal()
        return {"level": round(math.exp(el), 3), "day": round(math.exp(ed), 3),
                "pWar": round(sum(dm[j] for j, dv in enumerate(self.D) if dv < -0.15), 2), "warSeen": self.war_seen}


def make_grid(u_min: float, u_max: float) -> list:
    """The markups tried for each quote, accumulated in steps of 0.02 as the JS does."""
    grid, u = [], u_min
    while u <= u_max + 1e-9:
        grid.append(u)
        u += 0.02
    return grid


def leg_minutes(req: dict) -> int:
    m = 0
    for i, l in enumerate(req["legs"]):
        m += road(l["pickup"]["placeId"], l["delivery"]["placeId"])["minutes"]
        if i > 0:
            m += road(req["legs"][i - 1]["delivery"]["placeId"], l["pickup"]["placeId"])["minutes"]
    return m


def eff_deadline(req: dict, deadline: int) -> int:
    h = req["legs"][-1]["delivery"].get("hours")
    return min(deadline, deadline // 1440 * 1440 + h["closes"]) if h else deadline


def update_trust(trust: dict, desk, e: dict) -> None:
    """On the last delivery of an order: +1 on time (to 25), halved when late (RULES.md, dealer trust)."""
    o = desk.orders.get(e["orderId"])
    if not o:
        return
    done = [e["at"] if i == e["leg"] else d for i, d in enumerate(o["deliveredAt"])]
    if all(d is not None for d in done):
        on_time = all(d <= o["deadline"] for d in done)
        t = trust.get(o["dealerId"], 0)
        trust[o["dealerId"]] = min(25, t + 1) if on_time else t // 2


class PolicyV2:
    DEFAULTS = {
        "maxKm": 400, "minSlack": 480, "minSlackPriv": 600, "tradeinPriv": False,
        "f0": 1.0, "f1": 1.30, "f1Lead": 360,
        "costMult": 1.08,     # expected fee paid / ref for a car we take (incl. occasional raises)
        "lateLoss": 1.2,      # expected loss of a late order, in ref units (refund + steep fees + trust)
        "pLate": 0.04,
        "uMin": 1.0, "uMax": 2.3, "minEv": 0,
        "k": 11, "war": 0.15, "daySd": 0.12,
        "declineMult": 5,
    }

    def __init__(self, params: Optional[dict] = None):
        p = self.params = {**self.DEFAULTS, **(params or {})}
        self.comp = CompModel(k=p["k"], war=p["war"], daySd=p["daySd"])
        self.trust: dict = {}      # dealer -> on-time count (0..25)
        self.quoted: dict = {}     # request id -> x (log price/list, trust-adjusted)
        self.grid = make_grid(p["uMin"], p["uMax"])
        self.stats = {"quoted": 0, "declined": 0, "evNeg": 0}

    def slack_of(self, req: dict, opt: dict) -> int:
        start = max(req["respondBy"], req["legs"][0]["pickup"]["window"]["from"])
        s = eff_deadline(req, opt["deadline"]) - leg_minutes(req) - 30 - start
        pw = req["legs"][0]["pickup"]
        if pw["privateAddress"]:
            s = min(s, pw["window"]["to"] - start)
        return s

    def quote(self, desk, req: dict) -> dict:
        p = self.params
        km = max(road(l["pickup"]["placeId"], l["delivery"]["placeId"])["km"] for l in req["legs"])
        priv = any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in req["legs"])
        ok_opts = [o for o in req["options"] if km <= p["maxKm"] and self.slack_of(req, o) >= (p["minSlackPriv"] if priv else p["minSlack"])
                   and not (len(req["legs"]) > 1 and priv and not p["tradeinPriv"])]
        prices = {o["kind"]: round_half_up(o["listPriceCents"] * p["declineMult"] / 100) * 100 for o in req["options"]}
        if not ok_opts:
            self.stats["declined"] += 1
            return prices
        # one markup u for the accepted options, chosen on the first accepted option's list price
        o, ref = ok_opts[0], req["referenceFeeCents"]
        lst = o["listPriceCents"]
        t = 1 + 0.01 * self.trust.get(req["dealerId"], 0)
        pw = self.comp.p_win(req["arrivedAt"], [math.log(u / t) for u in self.grid])
        cost = p["costMult"] * ref + p["pLate"] * p["lateLoss"] * ref
        best, bu = -1, None
        for i, u in enumerate(self.grid):
            ev = pw[i] * (u * lst - cost)
            if ev > best:
                best, bu = ev, u
        if best <= p["minEv"]:
            self.stats["evNeg"] += 1
            return prices
        for oo in ok_opts:
            prices[oo["kind"]] = round_half_up(oo["listPriceCents"] * bu / 100) * 100
        self.quoted[req["id"]] = math.log(bu / t)
        self.stats["quoted"] += 1
        return prices

    def on_event(self, desk, e: dict, replay: bool = False) -> None:
        if e["kind"] == "order_won" and e["order"]["id"] in self.quoted:
            r = desk.requests.get(e["order"]["id"])
            self.comp.observe(r["arrivedAt"] if r else e["at"], self.quoted.pop(e["order"]["id"]), True)
        elif e["kind"] == "order_lost" and e["requestId"] in self.quoted:
            r = desk.requests.get(e["requestId"])
            self.comp.observe(r["arrivedAt"] if r else e["at"], self.quoted.pop(e["requestId"]), False)
        elif e["kind"] == "delivered":
            update_trust(self.trust, desk, e)

    def fees(self, desk, order: dict, cars: int, urgent: bool) -> list:
        p = self.params
        ref = desk.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        if urgent:
            return steep(desk.now, ref, 8)
        return [{"at": desk.now, "feeCents": round_half_up(ref * p["f0"])},
                {"at": max(desk.now + 1, order["lastPickupAt"] - p["f1Lead"]), "feeCents": round_half_up(ref * p["f1"])}]
