# Ranked-round handoff — 14 Nov 2026

## Deploy this policy

Use the normal CLI / final entry point with no experimental parameters:

```sh
python -m spread_bot --final
```

It now deploys **G**, the best validated policy: `maxKm=550`, `minSlack=360`,
`backlogB=12`, the existing structural-risk price and late-share brake. The CLI explicitly
turns off option-specific pricing, dealer effects, chains, direct offers and snow until each
addition earns a promotion. Start `--final` **before** the ranked round begins: it waits for the
world, whereas joining late can miss the short request-response window. The final state persists
in `spread-state.json`.

Important implementation detail: `PolicyV4()` by itself keeps experimental features enabled for
focused tests. `make_policy()` in `spread_bot/__main__.py` applies `PolicyV4.SAFE_DEPLOYMENT`, so
the normal CLI is safe. A script that constructs `PolicyV4` directly must pass those five safe
switches itself.

## Why G is the baseline

G beat F on every completed truly unseen `v-9...v-15` practice week: **€10,257 average**, worst
on-time **93.0%**. It reached **€14,958 on v-13**, so the recent €8–10k weeks are scenario
variation, not a cap on the strategy. Earlier `s1...s8` runs include €14,218, €13,946 and €13,525
weeks. The full historical table and the final timing/request-budget notes are in
`OPTIMIZATION_LOG.md`.

Fresh paired control (`v-18...v-20`) was:

| Seed | G margin | On time | Won orders |
|---|---:|---:|---:|
| v-18 | €9,551.43 | 98.2% | 278 |
| v-19 | €9,386.34 | 98.6% | 295 |
| v-20 | €8,249.07 | 99.4% | 308 |
| mean / worst | €9,062.28 | 98.2% | 293.7 |

The official minimum is 90%; the working promotion floor is 93% worst-seed on time. Never choose
on mean margin alone.

## Recent experiments

| Change vs G, same seeds | Result | Decision |
|---|---|---|
| Direct offers | €9,062.19 mean, 98.2% worst on time; 71 sent / 67 answered / 1 accepted | Do not promote. The current targets and fee add no measurable margin. |
| Option-specific prices | €8,671.51 mean, 97.4% worst on time; fewer wins | Reject current `speedValue` values. Revisit only with a separate value sweep / choice fit. |
| Dealer competitor offsets | `v-18`: +€160.70 and +0.4 pp; `v-20`: +€199.87 and +0.6 pp | Promising but **incomplete**: `v-19` was deliberately stopped. Do not enable for ranked without completing paired validation. |
| Chains | Started but all three worlds were deliberately stopped before a score | No evidence; keep off. |
| All features, v-17 only | +€294.49 vs G but −1.6 pp on-time headroom | One seed is insufficient; do not use. |

The stopped-world errors in the dealer/chains JSON reports are intentional cleanup, not policy
errors. There are **no active practice worlds** at handoff.

## Model and measurement work now available

- `spread_bot/experiments.py` runs named one-feature ablations, checkpoints every completed and
  in-flight world, and reports margin, worst on-time, booking calibration and offer outcomes.
- `spread_bot/booking_model.py` adds a dependency-free, partially pooled logistic booking-risk
  model (Gaussian-prior / L2-shrunk pickup, delivery, dealer and option effects). It is
  **offline-only** and cross-validates by holding out an entire seed.
- On the 881 labelled G bookings from `v-18...v-20`, model Brier was 0.0134 vs 0.0407 for the
  town-risk heuristic. Failures were only ~1.4% and all model scores were below 20%, so this is
  a calibration baseline—not proof it can identify high-risk cars. Do not put it in live pricing
  before more varied held-out data.
- Direct-offer telemetry now logs app fee, offered fee/premium, driver location/distance/profile,
  answer and later per-order outcome. It correctly treats an app booking while an offer is open
  as context, not as evidence that the offer caused it.

## Best next steps after the round

1. Re-run `dealer_effects` on `v-18...v-32` with 3–4 workers; include a fresh G control if the
   old reports are unavailable. Promote only if worst on-time stays at least 93%.
2. Run the already-implemented `chains` ablation on the same seeds. Chains are the only remaining
   high-upside feature with an implementation ready to test.
3. If neither is robust, keep G. Then tune its existing knobs using constrained, common-seed
   sweeps—not a large all-feature search.
4. Only after sufficient diverse reports, consider using the booking model to adjust risk. Keep
   calibration and held-out Brier as acceptance criteria.

Example experiment command (uses four of eight practice slots):

```sh
python -m spread_bot --experiment dealer_effects --experiment-workers 4 \
  --seeds v-18,v-19,v-20,v-21 --experiment-output /private/tmp/dealer-v18-v21.json
```

Practice reports from this machine were written under `/private/tmp/` and are not versioned. The
numeric findings above are the durable record. `TODO.md` is the active backlog; `HANDOFF.md` is
the time-critical ranked-round context.
