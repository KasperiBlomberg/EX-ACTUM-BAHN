# Dispatch optimization roadmap

The rule for every experiment is: maximise gross margin **subject to** a conservative
on-time floor. The official floor is 90%; use 93% as the selection floor until a model
is proven robust. Never choose a configuration from average margin alone.

## 1. Establish a trustworthy experiment loop — in progress

- [x] Keep the known G baseline (`maxKm=550`, `backlogB=12`) as a named configuration.
- [x] Add named ablations for option pricing, dealer effects, chains, direct offers, and snow.
- [x] Add a repeatable runner that records one row per seed/configuration and writes a JSON report.
- [ ] Run baseline and one feature at a time on fresh seeds `v-17...v-32`.
- [ ] Record mean/median margin, worst on-time share, orders won, and ranking count.
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

## 2. Tune existing knobs — next

Use common seeds for every candidate. Search small related groups, not every parameter at once.

- [ ] Selection/risk sweep: `maxKm`, `minSlack`, `backlogB`, `targetLate`, `f0`.
- [ ] Price sweep: same-day speed value, within-three-days value, price-grid limits.
- [ ] Chain sweep: hold time, minimum lead, maximum cars, marginal-car fee share.
- [ ] Fee sweep: opening fee/risk uplift and both escalation times.
- [ ] Use a constrained objective: discard candidates below the on-time floor, then rank by mean margin.

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
