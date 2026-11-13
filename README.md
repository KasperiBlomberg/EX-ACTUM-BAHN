# Spread bot

A dispatcher bot for the Spread challenge (see `llms.txt` and `docs/`). **Read `OPTIMIZATION_LOG.md` for everything that was tried and measured.**

The bot is in **Python** (`spread_bot/`, Python 3.9+, no dependencies). It is a 1:1 port of the original JavaScript bot (`src/` → `spread-bot.mjs`), which is kept as a verified fallback and needs Node 22+.

## Run

Put the team key in `.env` (`SPREAD_KEY=...`; the file is gitignored) or export `SPREAD_URL` and `SPREAD_KEY`.

```sh
python3 -m spread_bot --level 8 --seed v-1                        # one practice week
python3 -m spread_bot --seeds v-9,v-10,v-11,v-12,v-13,v-14,v-15,v-16   # 8 weeks in parallel + total
python3 -m spread_bot --seeds v-1,v-2 --params '{"backlogB":12,"maxKm":550}'   # override settings (here: G)
python3 -m spread_bot --level 8 --board                           # next ranking run (it counts!)
caffeinate -i python3 -m spread_bot --final                       # test final / final: waits, then plays
python3 -m spread_bot --seed v-1 --starter                        # the kit starter's decisions, to check the desk
```

The JS bot takes the same flags: `node spread-bot.mjs --level 8 --seed v-1` (edit `src/`, then `./build.sh`).

**In the final**, the bot polls `/events`, answers each event straight away, and saves what it has learnt to `spread-state.json` every 2 seconds. After a crash, run the same command again: it rebuilds its orders from the server and reloads the state.

## Layout

| Python | JS original | What |
|---|---|---|
| `spread_bot/api.py` | `src/core.js` | HTTP client: retries on 429 and server restarts |
| `spread_bot/geo.py` | globals `P`, `ROAD`, `hav` | the map |
| `spread_bot/desk.py` | `src/desk.js` | bookkeeping and event answers; `play_practice` |
| `spread_bot/notes.py` | `src/notes.js` | dealer-note reader (fi/sv/en) |
| `spread_bot/policy_v2.py` | `src/policy_v2.js` | Bayesian competitor model, v2 policy |
| `spread_bot/policy_v4.py` | `src/policy_v4.js` | **the policy the bot plays** |
| `spread_bot/__main__.py` | `src/main.js` | CLI, sweeps, the final loop |
| `tests/test_notes.py` | `tests/notes.test.mjs` | 43 note cases: `python3 tests/test_notes.py` |

`docs/` holds the challenge handbook, rules, API reference and the official Python kit. `tools/` holds the JS browser harness used to simulate a final clock, and the direct-offer policy that didn't pay off.

## Port verification

See "Port" in `OPTIMIZATION_LOG.md` for the side-by-side runs of the JS and Python bots on the same seeds.

## Default (setting set F) on level 8 seeds v-1…v-8
Average **+€11.2k per week**. Worst on-time share 95.3%, and all eight weeks ranked. The starter loses €72.6k on spread-1.

## How it decides (short version)

1. **Which cars to take.** Distance and slack are hard limits (450 km, 6 hours). Slack comes from the deadline, the dealer's note, the site hours and the drive time.
2. **Risk of no driver.** Per pickup town, learnt live, with a prior from the roster. The bot's own unbooked backlog in the town adds to it, as do structural risks from short slack and long distance.
3. **The price.** A Bayesian competitor model per world and per day, with a price-war prior and dealer trust. The bot quotes the price that maximises P(win) × (price − fee − risk cost × lateness shadow price). Slower options are offered only when the fast option is much riskier.
4. **Fees.** Flat (0.92 + 0.30 × risk) × the typical fee, then 1.25× at 7 hours and 1.5× at 4.5 hours before the last safe pickup. The fee goes up at once after a drop, and to the steep schedule after the deadline warning.
5. **Notes.** They set the stop windows, plates and the manual-licence audience.
