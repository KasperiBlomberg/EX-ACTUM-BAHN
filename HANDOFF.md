# Ranked-round handoff — 14 Nov 2026 (updated 18:15)

## Play the ranking round with `--board`, not `--final`

A ranking round is five secret level-8 practice weeks that **our code advances**. Play them one
after another (the server refuses a second ranking run while one is still running):

```sh
for i in 1 2 3 4 5; do python3 -m spread_bot --level 8 --board; done
```

- The 18:00 round **opens** at 18:00 and stays open until the next round opens at 21:00. A run
  counts in the round in which it was created, so there is no need to start at 18:00 sharp.
- A team is ranked in a round only when all five weeks are at least 90% on time. Every run counts,
  also a deleted one. The rounds do not count for the final; they show where we stand.
- `--final` is only for the live-clock worlds: the test final at 20:00 and the final on Sunday
  08:00. Start it several minutes before the start time; it waits for the world by itself, and
  joining late misses the requests. Its state persists in `spread-state.json`.

The normal CLI now plays **G + the dealer-trust investment + the lane table** with no parameters:
`maxKm=550`, `minSlack=360`, `backlogB=12`, the structural-risk price, the late-share brake,
`trustBonus=1.0` and (since 18:15) `laneScale=1.0`. `make_policy()` in `spread_bot/__main__.py`
applies `PolicyV4.RANKED_DEPLOYMENT` (= `SAFE_DEPLOYMENT`, the G control, plus what has been
promoted). On six seeds this is **€10,491 a week against €9,731 for G (+7.8%)**, worst on time
94.4%. To play the plain G control:

```sh
python3 -m spread_bot --level 8 --board --params '{"trustBonus":0,"laneScale":0}'
```

A script that constructs `PolicyV4` directly must pass the switches itself.

## Two new mechanisms (17:30–18:15): both promoted

### 1. Dealer-trust investment — promoted, `trustBonus=1.0`

At level 8 each on-time order lets its dealer accept 1% more, to 25%. Requests are very
concentrated: **D01 sends 30% of all requests, D02 13%, D03 8%** (the same in every world), and
D01 is 35–44% of our orders. G is myopic; it reaches the cap of 25 with D01 only on Tuesday evening
or Wednesday. `trust_bonus()` adds the future value of one more counted order to the quote's
expected value (zero once the dealer's count plus its orders under way reach 25), so the bot bids
lower for busy dealers early in the week and wins more orders.

| Seed | G | G + trust | Difference | On time G → trust | Orders G → trust |
|---|---:|---:|---:|---|---|
| v-9 | €11,109 | €11,064 | −€45 | 94.7% → 95.5% | 301 → 330 |
| v-13 | €13,903 | €14,204 | +€301 | 97.6% → 98.2% | 381 → 399 |
| v-17 | €6,186 | €7,160 | +€974 | 95.8% → 95.1% | 216 → 267 |
| v-18 | €9,551 | €9,117 | −€434 | 98.2% → 96.6% | 278 → 293 |
| v-19 | €9,386 | €9,400 | +€14 | 98.6% → 98.2% | 295 → 325 |
| v-20 | €8,249 | €9,394 | +€1,145 | 99.4% → 98.9% | 308 → 357 |
| **mean / worst** | **€9,731** | **€10,056** | **+€326 (+3.3%)** | 94.7% → **95.1%** | |

It clears the promotion rule (worst seed 95.1%, above the 93% floor; higher mean), so it is in
`RANKED_DEPLOYMENT`. Be honest about the strength: six seeds, a standard error of about €250 on the
mean difference, so the true effect could be near zero. Confirm on fresh seeds before the final.

### 2. Lane table — promoted together with the trust bonus, `laneScale=1.0`

The handbook says the competitor's price has a lane factor of which one part "is almost the same
in every world", and that our quotes over several worlds show it. Every ended practice world keeps
its reveal, so `spread_bot/lane_fit.py` downloaded 220 reveals (168,764 real quotes with their
outcome, 25 seeds) and fitted a pickup-place and a delivery-place offset; the result is
`spread_bot/lanes.json`. The quote subtracts the offset before it asks the live model for the win
chance, and the live model learns only what is new in this world.

- The competitor is dearest for **deliveries to** Rovaniemi, Kemi, Kajaani (+16–17%), Kotka,
  Ylivieska, Savonlinna, Kokkola (+11–13%), and cheapest for deliveries to Vantaa, Hyvinkää, Espoo,
  Helsinki (−6 to −8%). Pickup effects are smaller (Oulu and Joensuu −9%, Vantaa +5%).
- **Offline, held-out seeds:** log loss on 17,086 quotes from six seeds the fit never saw falls
  from 0.5354 to 0.4846. Pair effects, dealer effects, hour, list-price band and option mix add
  nothing on held-out seeds, and the slope `k=11` is confirmed (9–11 is the optimum).
- **Model-based size:** pricing with the table is worth about +6% expected margin per request.
- **Live, six seeds against G:** mean **+€73** a week, worst on time **92.8%** (G: 94.7%).

| Seed | G | G + lanes | Difference | On time G → lanes |
|---|---:|---:|---:|---|
| v-9 | €11,109 | €10,817 | −€292 | 94.7% → 92.8% |
| v-13 | €13,903 | €15,252 | +€1,349 | 97.6% → 98.2% |
| v-17 | €6,186 | €6,196 | +€10 | 95.8% → 96.3% |
| v-18 | €9,551 | €9,589 | +€38 | 98.2% → 97.5% |
| v-19 | €9,386 | €8,397 | −€989 | 98.6% → 97.8% |
| v-20 | €8,249 | €8,572 | +€323 | 99.4% → 99.0% |

Six seeds cannot show a 6% effect (see the noise note below), and v-9 is under the 93% promotion
floor. Verdict: real signal offline, unproven live on its own.

**Lanes on top of the trust bonus (`lanes_trust`), first three seeds, 18:05:**

| Seed | G | G + trust | G + trust + lanes | vs trust | vs G | On time |
|---|---:|---:|---:|---:|---:|---|
| v-9 | €11,109 | €11,064 | €12,087 | +€1,023 | +€978 | 95.0% |
| v-17 | €6,186 | €7,160 | €6,884 | −€276 | +€698 | 94.4% |
| v-19 | €9,386 | €9,400 | €10,051 | +€651 | +€665 | 98.2% |

| v-13 | €13,903 | €14,204 | €15,068 | +€864 | +€1,165 | 98.5% |
| v-18 | €9,551 | €9,117 | €9,311 | +€194 | −€240 | 97.6% |
| v-20 | €8,249 | €9,394 | €9,547 | +€153 | +€1,298 | 99.7% |
| **mean / worst** | **€9,731** | **€10,056** | **€10,491** | **+€435** | **+€761 (+7.8%)** | **94.4%** |

Five of six seeds are up against G and five of six against the trust bonus alone; the standard
error of the mean difference against G is about €225. It clears the promotion rule, so
`laneScale=1.0` joined `RANKED_DEPLOYMENT` at 18:15. Lanes alone were flat on the same seeds
(+€73), so read this as "lanes help once the trust bonus is on", not as two independent gains.

## What else was measured today

- **The deployed G is not the G of the v-9…v-15 table.** The same seeds now give v-9 €11,109 /
  94.7% (was €10,754 / 93.0%) and v-13 €13,903 / 97.6% (was €14,958 / 98.2%). One cause is certain:
  the control path now stores its quote per option, so a loss on a request with several options is
  learnt at `multiLossWeight` 0.55 instead of 1.0. Neither version is clearly better on two seeds;
  the v-17…v-20 controls below were measured with the current code and are the valid baseline.
- **Dealer competitor offsets, now six paired seeds:** +€83, +€161, −€540, +€200 (v-17…v-20),
  −€910 (v-9), +€718 (v-13): mean **−€48**, worst on time 92.8%. Rejected; the two positive seeds at
  the last handoff were noise.
- **Noise.** The same change gives −€989 on one seed and +€1,349 on another. Paired differences
  have a standard deviation of about €700 a seed, so three seeds cannot see anything under ±€800.
  A 5% effect needs 10 or more paired seeds; judge small changes offline first (`lane_fit.py` shows
  how, on 168,000 quotes).

Current-code G controls: v-9 €11,109 / 94.7%, v-13 €13,903 / 97.6%, v-17 €6,186 / 95.8%,
v-18 €9,551 / 98.2%, v-19 €9,386 / 98.6%, v-20 €8,249 / 99.4%. The official minimum is 90%; the
working promotion floor is 93% worst-seed on time. Never choose on mean margin alone.

## Earlier experiments (unchanged)

| Change vs G, same seeds | Result | Decision |
|---|---|---|
| Direct offers | €9,062.19 mean, 98.2% worst on time; 71 sent / 67 answered / 1 accepted | Do not promote. |
| Option-specific prices | €8,671.51 mean, 97.4% worst on time; fewer wins | Reject current `speedValue` values. |
| Chains | No completed world | No evidence; keep off. |
| All features, v-17 only | +€294.49 vs G but −1.6 pp on-time headroom | One seed is insufficient. |

`spread_bot/experiments.py` runs named ablations (now also `lanes`, `trust_bonus`, `lanes_trust`)
and checkpoints every world. `spread_bot/booking_model.py` is an offline-only booking-risk model.

## Best next steps

1. **The gap to the top 10 is the real problem.** In the 15:00 round the tenth team made about
   €17,000 a week and the first €21,400, with 440–490 orders a week at €39–43 an order. We make about
   €10,500 with about 330 orders at about €32. House best wins as few orders as we do but earns €42
   an order. So look for the large levers first: what drivers really need as a fee (the reveal
   `views` of our 220 worlds, unused so far), return-leg pairing, and the 23–33% of requests we
   decline outright.
2. Confirm the new default on fresh seeds: `baseline_g` against `lanes_trust` on `v-21…v-32`
   (24 worlds, about half an hour at four slots). Go back to G if it does not hold.
3. Refit the lane table after more worlds have ended (`lane_fit --fetch` adds them).
4. Run `--final` in the 20:00 test final to re-check the live loop; play the later rounds
   (21:00, 00:00, 03:00, 06:00) with whatever is promoted by then.
5. The teammate's five suggestions are assessed in `TODO.md` section 8.

```sh
python3 -m spread_bot --experiment lanes_trust --experiment-workers 4 \
  --seeds v-21,v-22,v-23,v-24 --experiment-output /private/tmp/lanes-trust-v21-v24.json
python3 -m spread_bot.lane_fit --fetch /private/tmp/reveals   # then: --fit /private/tmp/reveals --holdout
```

**Practice slots:** the team has eight. Use at most **four** at a time from one machine so a
colleague can test in parallel (`--experiment-workers 4`, or at most four `--seeds`).

Practice reports from this machine are under `/private/tmp/` and are not versioned. The numeric
findings above are the durable record. `TODO.md` is the active backlog.
