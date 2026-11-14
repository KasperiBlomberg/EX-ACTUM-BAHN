# Dispatch optimization roadmap

The rule for every experiment is: maximise gross margin **subject to** a conservative
on-time floor. The official floor is 90%; use 93% as the selection floor until a model
is proven robust. Never choose a configuration from average margin alone.

## 0. Validate the two quote-side additions of 14 Nov — do this first

The trust bonus and the lane table are now the CLI default. Per-seed tables are in `HANDOFF.md`.

- [x] Lane table (`laneScale`, `spread_bot/lanes.json`, fitted by `spread_bot/lane_fit.py` from 220
      practice reveals). Held-out-seed log loss 0.5354 → 0.4846; model-based value about +6% margin.
- [x] Live pilot of lanes on six seeds: mean +€73 a week against G, worst on time 92.8%. Inconclusive.
- [x] Dealer-trust investment (`trustBonus`). Six seeds: mean +€326 a week (+3.3%) against G, four
      of six seeds up, worst on time 95.1%. **Promoted** to `PolicyV4.RANKED_DEPLOYMENT` (CLI default).
- [x] `lanes_trust` on v-9, v-17, v-19: +€1,023, −€276, +€651 against trust alone (mean +€466);
      +€780 (+8.8%) against G; worst on time 94.4%.
- [x] `lanes_trust` on v-13, v-18, v-20: +€864, +€194, +€153 against trust alone. All six seeds:
      mean +€435 against trust alone and +€761 (+7.8%) against G, worst on time 94.4%.
      **Promoted**: `laneScale=1.0` is in `RANKED_DEPLOYMENT` since 18:15.
- [ ] Confirm on fresh seeds: `baseline_g` against `lanes_trust` on `v-21…v-32`. Go back to G if the
      gain does not hold or the worst seed falls under 93% on time.
- [ ] Try `laneScale` 0.5 and `trustBonus` 0.5 if the full-strength versions cost on-time headroom.
- [ ] Refit the lane table when more worlds have ended, and check the offsets stay stable.

Measured noise: the same change gives −€989 on one seed and +€1,349 on another (paired standard
deviation about €700 a seed). Three seeds cannot see an effect under ±€800; a 5% effect needs 10 or
more paired seeds. Screen pricing ideas offline on the reveal quotes first, as `lane_fit.py` does.

Also open: the deployed G differs from the G of the v-9…v-15 table (multi-option losses are learnt
at `multiLossWeight` 0.55, not 1.0). Run `--params '{"multiLossWeight":1.0}'` on the 12 seeds too.

## 0b. Return-leg pairs — the largest lever found so far, NOT built yet (18:30)

Measured on the gigs, views and bookings of 243 finished practice worlds:

- A trade-in (A→B then B→A in one gig) has a typical fee of **0.49 ×** the two single cars on the
  same lanes (525 requests; quartiles 0.44–0.60): a round trip costs a driver about one one-way gig.
  The 11 trade-in gigs we ever posted all booked at that fee (median 3 minutes; 4 were later dropped).
- Of our won single-car orders, **18%** find a return leg if held up to 60 minutes (delivery within
  30 km of the next pickup, end within 30 km of the start), 29% within 4 hours; 32% and 43% at 60 km.
  Rough fee saving on today's order flow: €650 to €2,000 a week.
- The larger gain is in the quote: a request that completes a pair with a held order costs little
  extra, so it can be priced to win. That is probably how the leaders win 440–490 orders a week.
- Plain fee cuts are not the lever: 87–92% of gigs book at the opening fee anywhere from 0.80 to
  1.15 × typical fee, but drops double under 0.88 (14% against 6%).

To build: (1) hold a fresh single-car order with enough lead and match it with a held or open
unbooked order as x→y (use `Desk.post_bundle`; same-day feasibility with windows and the 12-hour
rule); (2) pair fee about 0.62 × the two typical fees, rising to the single-car level before the
warning; (3) dissolve an unbooked pair after about an hour and post the cars singly; (4) quote with
the marginal cost when a request completes a pair. Analysis scripts are in the session scratchpad
(`pairs1.py`, `pairs2.py`, `fee1.py`), not in the repo.

## 1. Establish a trustworthy experiment loop — in progress

- [x] Keep the known G baseline (`maxKm=550`, `backlogB=12`) as a named configuration.
- [x] Add named ablations for option pricing, dealer effects, chains, direct offers, and snow.
- [x] Add a repeatable runner that records one row per seed/configuration and writes a JSON report.
- [ ] Finish baseline and one-feature paired runs on fresh seeds `v-17...v-32` (paused for the ranked round).
- [x] Record mean/median margin, worst on-time share, orders won, and ranking count for the completed ablations.
- [ ] Promote only configurations whose worst seed is at least 93% on time.

### Pilot note — not enough evidence to select a policy

On fresh seed `v-17`, G scored **€6,185.91**, won 216 orders and was **95.8%** on time.
The all-features policy scored **€6,480.40**, won 209 orders and was **94.3%** on time.
That is +€294.49 margin but −1.6 percentage points of on-time headroom on one seed. Expand
to independent seeds before keeping or rejecting any feature. The all-features runner was
interrupted after its world ended, so this first comparison has no retained telemetry rows.

### Control checkpoint — v-18...v-20 (partial validation)

G scored **€9,551 / €9,386 / €8,249** on `v-18 / v-19 / v-20`, with **98.2%** worst-seed
on-time performance and 278 / 295 / 308 orders won. These are control measurements only;
compare each feature on these same seeds before attributing a difference to it.

### Direct-offer ablation — v-18...v-20 (do not promote yet)

With direct offers as the only change, mean margin was **€9,062.19** versus the control's
**€9,062.28**; worst on-time was unchanged at **98.2%** and all three weeks ranked. The policy
sent 71 offers: 67 were answered, only **1 (1.5% of answers)** was accepted, and that order was
on time. The policy is not harmful on this small sample, but this fee/targeting rule has not
earned its cost. Keep it off until a changed candidate beats the control on further held-out seeds.

### Option-price ablation — v-18...v-20 (current speed values rejected)

Option-specific pricing averaged **€8,671.51**, versus **€9,062.28** for G (−€390.77), won
286.0 versus 293.7 orders, and reduced worst-seed on-time from **98.2%** to **97.4%**. It remains
safe on this sample but is not competitive with the current hand-set speed values. Do not promote
it; revisit only through the planned speed-value sweep / dealer-choice fit.

### Dealer offsets and chains — interrupted deliberately before the ranked round

Dealer offsets were positive on the two completed paired seeds: `v-18` was **+€160.70** margin
with 98.6% on time (G: 98.2%), and `v-20` was **+€199.87** with 100.0% on time (G: 99.4%). `v-19`
was stopped, so this is promising but not promotion evidence. The three chain worlds were stopped
before completion, so chains have no result. All practice worlds were cleaned up; see
`HANDOFF.md` for the ranked-round deployment choice and restart commands.

## 2. Tune existing knobs — next

Use common seeds for every candidate. Search small related groups, not every parameter at once.

- [ ] Selection/risk sweep: `maxKm`, `minSlack`, `backlogB`, `targetLate`, `f0`.
- [ ] Price sweep: same-day speed value, within-three-days value, price-grid limits.
- [ ] Chain sweep: hold time, minimum lead, maximum cars, marginal-car fee share.
- [ ] Fee sweep: opening fee/risk uplift and both escalation times.
- [ ] Use a constrained objective: discard candidates below the on-time floor, then rank by mean margin.

### Ranked-round deployment choice

- [x] Make normal CLI and `--final` runs use the validated G control by default.
- [x] Keep option prices, dealer offsets, chains, direct offers and snow opt-in pending their own ablation.
- [ ] After the ranked round, finish dealer-offset and chain validation before enabling either.

## 3. Calibrate booking risk — highest-value model work

Replace hand-tuned town risk with a calibrated probability that a gig books before
`deadline_close`, then use that probability in quote and fee expected value.

- [x] Log an order-time feature row when every order is won.
- [x] Label whether it booked before warning and whether it arrived on time.
- [x] Start with a regularised hierarchical logistic model: pickup/delivery town, slack,
      distance, fee, backlog, weather, vehicle/note constraints, and driver density.
- [x] Emit Brier score and reliability bins for the current town-risk heuristic in experiment reports.
- [ ] Validate probability calibration (Brier score / reliability bins), not only classification accuracy.
- [ ] Add carefully bounded fee exploration on safe cars if needed to identify the fee effect.

## 4. Make chains a routing/matching problem

The current same-yard chain is intentionally conservative. Extend only after its ablation is safe.

- [ ] Start with two-car chains.
- [ ] Score an edge `A → B` by expected individual fees minus combined fee minus lateness risk.
- [ ] Consider return legs, a delivery near the next pickup, and compatible time windows.
- [ ] Enforce the 12-hour road-driving rule before posting.
- [ ] Compare chain margin, booking rate, and on-time share against individual gigs.

## 5. Improve the dealer-choice model

The present Bayesian model is global/day/dealer price level with hand-set speed values.

- [ ] Fit option speed values from observed selected options.
- [x] Add regularised pickup/delivery lane effects rather than one global market level
      (offline table, section 0). Pair effects and dealer effects did not help on held-out seeds.
- [ ] Evaluate a choice model over our delivery options and the hidden competitor alternative.
- [ ] Keep multi-option losses as censored evidence; do not pretend we know the competitor's chosen option.

## 6. Turn direct offers into a measured policy

- [x] Log offer context, fee above app price, driver position, response, and later order outcome.
- [ ] Compare candidate selection rules on safe held-out experiments.
- [ ] Only increase offer volume if marginal accepted offers improve on-time margin.

### Preliminary booking-model check — v-18...v-20

The held-out-seed partial-pooling model had Brier **0.0134** on 881 labelled orders, versus
**0.0407** for the town heuristic. But booking failures were rare (about 1.4%) and every model
prediction landed below 20% risk; this is a promising calibration baseline, not evidence that it
can yet separate genuinely risky cars. Collect more varied weeks before feeding it into live quotes.

## 7. Dealer trust — new

- [x] Measure concentration: D01 sends 30% of requests, D02 13%, D03 8%, in every world.
- [x] Value one more on-time order in the quote (`trust_bonus`, off by default).
- [ ] Price the loss as well: a late order halves the count, which for D01 at 25 costs far more
      than the `failRef`/`failPrice` terms assume. Add it to the failure cost for high-trust dealers.
- [ ] Use the documented level range (26–54% above list) as the competitor-level prior on Monday.

## 8. Suggestions from a teammate (email, 14 Nov 17:24) — assessed, not yet built

1. **Hard capacity reservations before quoting** (per-day, per-pickup-region ledger of open orders
   plus acceptable quotes; tighten as projected late share rises). Our weeks run 93–99% on time and
   the backlog term already prices local load, so this is a guardrail, not margin. Worth building
   only if `trustBonus`/`laneScale` push the worst seed under 93%: it would let them stay on.
2. **Calibrated on-time model instead of linear booking risk**, choosing the opening fee that
   minimises fee + P(late) × (penalty + shadow price). `booking_model.py` is the base; failures are
   about 1.4% of orders, so it needs many more labelled weeks. The reveal `views` (who saw a gig at
   which fee) are unused and are the right data for the fee side.
3. **Competitor learning by lane, and event detection.** The lane part is done offline (section 0).
   Still open: the outage (one region, one working day, the backup costs about 30% more: a regional
   +0.26 in log price) and a sharper price-war detector. Model-based gain of an outage detector is
   small (about €100–300 a week), because the day factor already absorbs part of it.
4. **Two-car rescue pairs** (pair a fresh order with an open unbooked gig when delivery → next
   pickup is close; dissolve after an hour). Chains have no completed ablation at all; run the
   existing `chains` experiment before building a second bundling mode.
5. **Supply-shock tomorrow state** (the flu: 30% of drivers stay home for a day). Detect unusual
   overnight drops and no-shows, then raise fees and tighten admissions for that day. Plausible and
   cheap; needs the flu day identified in past weeks to test.

Reference implementations named in the email (not reviewed): `aapolipponen/spread-dispatcher`
(`bot/capacity.ts`), `ink-waffle/junction26-spread` (`dispatch/fleet.py`, `market.py`, `pairs.py`).
