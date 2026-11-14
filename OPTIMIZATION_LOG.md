# Spread: optimisation log (Friday 13 Nov, 20:40–22:05)

What was built, measured and tried, so the work can continue in Claude Code. All numbers are game euros from **level 8** practice weeks unless stated. "Seeds v-1…v-8" means `--seed v-1` to `--seed v-8`. Every version was compared on those same seeds.

## 1. How the testing was done

- The cloud sandbox couldn't reach `spread.bahnexpress.com`, so every practice week ran in the Claude desktop app's browser pane, as JavaScript on the Spread origin. That's why the bot is written in JavaScript. It runs unchanged under Node 22 (`spread-bot.mjs`).
- The kit starter was ported first and checked to the cent: −€72,612.51 on `spread-1`, level 8, exactly as the handbook says. That proves the desk/bookkeeping port is faithful.
- A week takes about 80–200 s depending on server load. Eight weeks run in parallel, which is the practice-slot limit.
- With Node on your Mac: `node spread-bot.mjs --seeds v-1,v-2,v-3,v-4,v-5,v-6,v-7,v-8 --params '{...}'` runs a sweep and prints the total.

## 2. What the data showed (exploration runs)

**The starter (spread-1)** wins 949 orders and is 45% on time. By lane:

| Lane | Margin per order | On time |
|---|---|---|
| < 50 km | −€15 | 41% |
| 100–200 km | −€39 | 61% |
| ≥ 500 km | −€310 | 15% |

**Price structure:** the list price is about 1.08× the typical fee (`referenceFeeCents`) on most lanes, and about 1.6× on lanes under 50 km. The average typical fee is about €76.

**Dealer acceptance**, from 4 worlds with random quotes at 1.0–1.8× list:

| Quote vs list | Win rate |
|---|---|
| 1.0–1.1× | 93% |
| 1.1–1.2× | 82% |
| 1.2–1.3× | 71% |
| 1.3–1.4× | 53% |
| 1.4–1.5× | 41% |
| 1.5–1.6× | 33% |
| 1.6–1.7× | 22% |
| 1.7–1.8× | 14% |

- Fitted within one world, the slope k is about 8–14 per unit of log price.
- The competitor's level per world was 1.34–1.51× list.
- The per-day factor varies a lot: 1.02× to 1.77× within one week. The low days are probably the price war.
- Distance hardly changes the competitor's markup.

**Which option dealers take:** when we quote same-day and next-day at the same markup, the dealer takes same-day 84% of the time.

**Driver fees** (random flat fee, no raises):

| Fee vs typical fee | Booked at that fee |
|---|---|
| 0.9–1.1× | 64% |
| 1.1–1.3× | 76% |
| 1.3–1.5× | 82% |
| 1.5–1.7× | 79% |
| 1.7–1.9× | 85% |

Paying more barely helps. The cars that never book are limited by **reach and timing, not price**.

**Slack × distance → on-time rate** (slack = minutes from when the car can be collected to the last safe pickup):

| Distance | Slack < 4 h | 4–8 h | 8–24 h | ≥ 24 h |
|---|---|---|---|---|
| < 120 km | 65% | 68% | 83% | 92% |
| 120–250 km | 53% | 72% | 87% | 93% |
| 250–400 km | 46% | 48% | 85% | 82% |
| ≥ 400 km | 35% | 50% | 38% | 59% |

Private addresses are worse: 61% vs 76% on time.

**Once the bot is selective (v1), the fee economics flip.** 93% of orders booked at the 1.05× opening fee. On-time orders earned a price of 1.56× ref and paid 1.05× ref, about €29 margin per order.

**Why orders are late (v2/v3):** mostly no driver before `deadline_close` ("warned"), then drops, no-shows and failed visits. In a supply-poor world, 200 of 485 orders got the warning.

**Next-day orders in towns short of drivers** can sit unbooked for over a day. The deadline warning comes only on the afternoon of the deadline day, which is too late a signal. (That's what the early-failure signal in v4c fixes.)

## 3. Versions

Margin is per week and on-time is the worst seed, all on seeds v-1…v-8 except where noted.

| Version | Change | Average margin | Worst on time | Note |
|---|---|---|---|---|
| starter | — | −€72.6k (spread-1) | 45% | |
| explore | random quotes and fees | −€12k to −€30k | 62% | data only |
| v1 u=1.45 | decline if km > 400 or slack < 8 h (10 h private); quote 1.45× list; fee 1.05× flat → 1.30× at last pickup −6 h | €6.4k (s1–s4) | 86.4% | s3 unranked |
| v1 u=1.55 | same, 1.55× | €7.3k (s1–s2) | 97% | |
| v1 u=1.65 / 1.75 | | s3 good, s4 starved (25 orders) | | **the best markup differs per world** |
| v2 | live Bayesian competitor model (world level × day factor, price-war prior), dealer trust, best-margin quote; f0 = 1.0 | €7.3–8.6k | 89.9% | s2 unranked |
| v2 f0=0.9 | | €7.8–9.6k | 81.4% | too many unbooked cars |
| v3 | per-town booking success (prior from roster drivers within 30 km), priced into quotes and the opening fee; lateness shadow price; two-step escalation (1.25× at −7 h, 1.5× at −4.5 h); f0 = 0.92 + 0.30·pFail | €8.7k (s1–s4) | 91.0% | all ranked |
| v3 f0=1.0 | | €7.8k | 89.5% | worse |
| v3 f0=0.85 | | €7.3k | 85.0% | worse |
| v3 safe | targetLate 0.035 | €8.6k | 91.4% | |
| v4 | note reader (stop windows, plates, manual licence, slack from the note) + reprice at once on drop or no-show | — | — | **bug**: repost loop, below |
| v4b | v4 + fix | €9.3k | 88.0% | s3 regressed |
| v4c | + early failure (unbooked 4 h in daytime = town failure) + own-backlog risk per town (backlogB = 6) | **€9.5k** | **94.7%** | in the first zip |
| C5 = A + direct offers | offer unbooked gigs to drivers finishing our gigs nearby, at the current fee | €10.2k | 94.0% | **3 of 1,274 offers accepted: useless** |
| A | v4c + targetLate 0.05, backlogB 8, minSlack 420 | €10.2k | 94.0% | |
| B | targetLate 0.06, backlogB 10, minSlack 360, maxKm 450 | €11.5k | 88.3% | s3 unranked |
| D | B + projected-lateness guard (counts warned and long-unbooked open orders) + hard brake | €11.4k | 88.8% | brake too late; slack < 8 h orders 27% late, > 300 km 21% late |
| E | A-like + **structural risk** (slack < 8 h +20%, < 12 h +7%; km > 300 +14%, > 200 +4%) priced into pFail; fast option offered only if ≤ 8 pp riskier; soft brake (only blocks risky cars, so safe ones still dilute) | €10.8k | 93.4% | |
| **F (shipped default)** | E + targetLate 0.06, backlogB 10, minSlack 360, maxKm 450 | **€11.2k** | **95.3%** | all 8 ranked |
| G | F + backlogB 12, maxKm 550 | €11.6k | 93.4% | more margin, less buffer |

F per seed:

| Seed | Margin | On time |
|---|---|---|
| s1 | €10,557 | 96.3% |
| s2 | €10,285 | 97.7% |
| s3 | €14,545 | 95.3% |
| s4 | €8,612 | 98.3% |
| s5 | €9,261 | 98.3% |
| s6 | €10,103 | 98.5% |
| s7 | €12,705 | 97.7% |
| s8 | €13,192 | 97.0% |

G per seed:

| Seed | Margin | On time |
|---|---|---|
| s1 | €11,123 | 93.8% |
| s2 | €10,925 | 95.8% |
| s3 | €14,218 | 93.4% |
| s4 | €9,626 | 98.4% |
| s5 | €9,759 | 97.7% |
| s6 | €9,786 | 99.4% |
| s7 | €13,525 | 97.2% |
| s8 | €13,946 | 96.7% |

For reference, House best scored €60.4k over the five ranking runs of the "Friday 20:01" round, about €12k per week at 94.2% on time. (Those are different worlds, so it's not a direct comparison.)

## 4. Note reader (`src/notes.js`)

Rule-based, Finnish / Swedish / English, following RULES.md:
- days count from arrival;
- a weekday name means 2–6 days ahead;
- digits are a 24 h clock unless am/pm follows;
- hours in words are working hours ("two" = 14:00; "puoli kolme" / "halv tre" = 14:30);
- corrections win, and denied times are ignored.

Scored against the reveal's `LegTruth` on about 1,000 won orders from 6 worlds:

| Fact | First version | After fixes |
|---|---|---|
| later ready / release time | 70 / 87 | **84 / 87, 0 false alarms** |
| pickup closing hour | 16 / 63 | **61 / 63** |
| delivery closing hour | 24 (57 false alarms) | **27 / 28, 0 false alarms** |
| tighter deadline | 23 / 51 | **49 / 51** (false alarms harmless: they equal the normal deadline) |
| plates | 11 / 16 | **16 / 16** |
| manual gearbox | 21 / 21 | 21 / 21 |
| summer tyres | 7 tp, 71 fp | 7 tp, 0 fp |

Fixes that mattered:
- **Opening hours default to the pickup site.** Without a destination word ("kohde", "vastaanot", "receiving", "mottag", "perillä"), they're about the pickup site, and "liike kiinni klo X" means the pickup site closes at X.
- **A time range is always opening hours**, never a ready time.
- **Abbreviations are expanded before sentences are split:** "viim." → viimeistään. "huom" means *note*, not "huomenna".
- **More Finnish number forms:** "kolmelta", "kahdesta", "yhdeksältä".
- **Day words count as time cues:** "huomenna yhdeksältä".
- **Weekday abbreviations without the dot** ("to klo 13"), and typos such as "homenna", "trostaina", "plaates", "trade plates".
- **Spare summer tyres** "in the trunk" are not the car's tyres.
- **A correction keeps the day of the value it corrects** ("tomorrow 17:30. Correction: 16:30").

Tests: `node tests/notes.test.mjs` (43 cases, all passing).

## 5. Bugs found and fixed

- **Repost loop.** When a gig expired, a repost with a note window that had already ended expired again the next minute, every minute, and the week crawled. Fix: a stop window that ends within 2 hours of now is dropped, so the order's own window applies, and after 3 reposts in 2 hours the bot posts without windows. Both are in `Desk.post` / `Desk.repost`.
- **Double counting on restart.** After a restart, the policy state was being rebuilt twice (replaying history *and* restoring the state file). Fix: `rejoin(snapshot, history, replayPolicy)`. The final runner restores `spread-state.json` and skips the replay.

## 6. Tried and dropped

- **One fixed markup for every world.** The best value ranges from 1.45× to 1.75× list, so the competitor has to be learned live.
- **Opening fee 0.85× or 1.0×.** 0.92× plus a risk term is best.
- **Direct offers to drivers finishing our gigs nearby, at the current fee.** Almost all were declined or expired.
- **A hard brake that blocks every quote.** It stops the safe orders that would dilute the late share. The soft brake (blocking only risky cars) is better.

## 7. Not done yet (suggested next steps)

1. **Validate F (and G) on unseen seeds,** for example v-9…v-24. Pick the default for the final only after that. Watch the worst on-time share more than the average.
2. **Test the `--final` loop.** It is smoke-tested offline but has **never run against a live clock**. Use the test final on Saturday 14:00 (start it around 13:45). A browser harness that drives a practice world's clock is in `tools/finaltest-browser.js`; it could be ported to Node.
3. **Fee sweep on F:** f0 0.88 / 0.96, f1Lead, f2Lead, f2.
4. **Price each option separately.** Same-day is worth more to the dealer, so charge it more and steer toward next-day, which has more slack. The competitor-model update then needs the chosen option's price.
5. **A per-dealer offset in the competitor model.** The competitor lowers its prices for dealers where we win most.
6. **The bulk order** (30–40 cars from one yard): check how the backlog signal handles it, and perhaps cap wins per yard.
7. **Use the forecast event** (snow; summer tyres in snow can't be collected).
8. **Ranking runs:** five per level per round, and they count even if deleted. Play them only with the validated default.

## 8. The Python port (Friday 13 Nov, 22:30–23:30)

The bot was ported 1:1 to Python (`spread_bot/`, 3.9+, no dependencies) so the team can read and change it; the JS bot stays as a verified fallback. Module for module, same names, same constants, no changes to the logic during the port. Both bots take a `--starter` flag that plays the kit starter's decisions (no policy), to check the desk on its own.

Verification on level 8, same seeds, both bots:

| Run | JS | Python |
|---|---|---|
| v-1 `--starter` | −€79,963.93, 949 orders, 48.2% on time | −€79,963.93, 949 orders, 48.2% on time |
| v-1 (v4 = F) | €10,557.42, 355 orders, 96.3% on time | €10,557.42, 355 orders, 96.3% on time |
| v-2…v-8 (v4 = F) | the F table above (to the euro) | the F table above (to the euro) |

Identical to the cent, the Bayesian model's floating-point included. Note tests: `python3 tests/test_notes.py`, 43/43, the same cases as the JS suite.

Porting notes: Python's `re` has no `\p{L}`, so `notes.py` builds the letter class and the ASCII `\w`/`\b` by hand to match the JS regexes exactly; JS `Math.round` is `floor(x + 0.5)`; the markup grid is accumulated in 0.02 steps as in JS, not computed, so the floats match; the API client keeps one connection per thread (urllib would open a new TLS connection per request, several times slower).

## 9. Settings reference (`makeV4` defaults = F)

`maxKm 450`, `minSlack 360`, `minSlackPriv 600`, `f0 0.92`, `f0Risk 0.30`, `f1 1.25 @ lastPickup−420`, `f2 1.5 @ lastPickup−270`, `failRef 1.0`, `failPrice 0.15`, `targetLate 0.06`, `shadowK 25`, `earlyFail 240`, `backlogB 10`, `warnW 0.4`, `pendW 0.15`, `hardLate 0.075`, `hardPFail 0.15`, `riskSlack [[480,.20],[720,.07],[∞,.02]]`, `riskKm [[200,0],[300,.04],[∞,.14]]`, `optRiskGap 0.08`, competitor model `k 11`, `war 0.15`, `daySd 0.12`, level grid 1.05–1.95, day grid −0.40…+0.35, `temper 0.8`.

G = F + `{"backlogB":12,"maxKm":550}`.

## 10. Option-specific quote experiment (14 Nov)

`spread_bot/policy_v4.py` now optimizes each acceptable delivery option separately. Its expected-value calculation uses that option's own structural lateness risk, and the competitor model learns from the option selected on a win. On a loss, it uses the most competitive option offered. `fastPremium` controls any extra relative same-day price.

Same level 8 seed `v-9`, four runs:

| Policy | `fastPremium` | Margin | Orders | On time | Ranked |
|---|---:|---:|---:|---:|---|
| Original F | — | €9,832.25 | 305 | 94.1% | yes |
| Per-option risk | 0% | **€10,146.26** | 302 | **94.4%** | yes |
| Per-option risk | 5% | €9,977.67 | 303 | 93.4% | yes |
| Per-option risk | 10% | €9,727.54 | 297 | 92.6% | yes |

The new calculation with no same-day premium beat original F by €314.01 and 0.3 percentage points on this seed. Increasing the premium reduced both margin and on-time share, so the default is `fastPremium: 0.0`. This is a promising one-seed result, not enough to establish a general improvement; compare on more unseen seeds before replacing F for a final.

The team's eight practice slots were occupied by regular, non-ranking worlds. With the user's authorization, the least advanced one was deleted; no ranking run was touched. Network access then worked and all four runs finished ranked.
