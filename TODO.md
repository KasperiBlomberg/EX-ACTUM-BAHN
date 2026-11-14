# Dispatch optimization roadmap

The rule for every experiment is: maximise gross margin **subject to** a conservative
on-time floor. The official floor is 90%; use 93% as the selection floor until a model
is proven robust. Never choose a configuration from average margin alone.

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
[`handoff.md`](handoff.md) for the prioritized model additions.

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
- [ ] Add regularised pickup/delivery lane effects rather than one global market level.
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

## 7. Priority additions from public implementation review

These are recommendations only; implement and promote each one through common-seed, held-out
experiments under the existing 93% on-time selection floor.

### 7.1 Hard capacity reservations before quoting

- [ ] Add a per-day and per-pickup-region capacity ledger.
- [ ] Reserve all cars for an acceptable quote until it is won or lost.
- [ ] Gate quotes when `open orders + reserved quotes` exceeds conservative local or global capacity.
- [ ] Tighten the capacity cap when projected late share rises.

This fills the current gap in `PolicyV4.quote`: risk affects price but a burst of quotes can still
be won before the resulting orders enter the backlog. Reference: [capacity.ts](https://github.com/aapolipponen/spread-dispatcher/blob/main/bot/capacity.ts).

### 7.2 Calibrated on-time model and fee selection

- [ ] Fit `P(late)` from fee/reference ratio, slack, route length, pickup/delivery supply,
      private/premium/trade-in flags, open-gig backlog, and gig waiting age.
- [ ] Choose the opening fee by minimizing `expected fee + P(late) × (refund/penalty + lateness shadow price)`.
- [ ] Include the estimated harm that a high-fee gig causes to other open gigs before changing a fee.

`PolicyV4` already has town failure learning and a lateness shadow; extend rather than replace
those controls. Reference: [fleet.py](https://github.com/ink-waffle/junction26-spread/blob/main/dispatch/fleet.py).

### 7.3 Competitor learning by lane and event detection

- [ ] Add partially pooled residuals for pickup town, delivery town, pickup→delivery lane, and
      pickup town on the current day to `CompModel`.
- [ ] Score price-war-day and regional competitor-outage hypotheses from early win/loss evidence.
- [ ] Quote higher locally during a likely outage and lower selectively during a likely price war.

Reference: [market.py](https://github.com/ink-waffle/junction26-spread/blob/main/dispatch/market.py).

### 7.4 Safer two-car rescue pairs

- [ ] Pair a fresh order with an already-open, unbooked one-car gig as a second bundling mode.
- [ ] Require delivery→next-pickup proximity, short total drive time, low idle time, and at least
      three hours of pickup lead.
- [ ] Dissolve an unbooked pair after roughly one hour and repost both cars individually.

Keep the current same-yard chains as a control; do not make longer chains the default. Reference:
[pairs.py](https://github.com/ink-waffle/junction26-spread/blob/main/dispatch/pairs.py).

### 7.5 Supply-shock-tomorrow state

- [ ] Detect unusual overnight booking drops and no-shows as evidence of a next-day supply shock.
- [ ] Pre-emptively raise fees and tighten admissions for the affected day, then decay the state
      as new bookings disprove it.

The existing live `p_fail` update is the base signal; this adds a forward-looking state rather
than waiting for next-day booking failures.
