# Spread bot

A dispatcher bot for the Spread challenge (see `llms.txt` and `docs/`). **Read `OPTIMIZATION_LOG.md` for everything that was tried and measured.**

The bot is in **Python** (`spread_bot/`, Python 3.9+, no dependencies). It is a 1:1 port of the original JavaScript bot, verified to the cent against it on eight seeds; the JS is in git history up to commit `3112c0e`.

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

**In the final**, the bot polls `/events`, answers each event straight away, and saves what it has learnt to `spread-state.json` every 2 seconds. After a crash, run the same command again: it rebuilds its orders from the server and reloads the state.

## Request budget (from the organisers, Saturday 14 Nov)

- A fast system plays a week with about **350 requests**; a slow one uses 2,000.
- Put the actions in the `advance` call, up to 50 in one request.
- Use `untilEvent` to go to the next event; do not step minute by minute.
- `advance` returns the new events: do not call `/events` or `/score` after each step.
- The rate limit is for the **whole team**: 8 weeks in parallel are not faster than 8 weeks one after another once the team is at its limit. Parallel weeks only help while each week waits on the network round trip.

The bot already does the first three. It uses about **1,100 requests a week** (v-9, measured), because it wakes on every price request (about 1,030 a week). Answering requests in batches would bring it near 350; see `OPTIMIZATION_LOG.md` section 10.

## Layout

| File | What |
|---|---|
| `spread_bot/api.py` | HTTP client: retries on 429 and server restarts |
| `spread_bot/geo.py` | the map: places, roads, straight-line distance |
| `spread_bot/desk.py` | bookkeeping and event answers; `play_practice` |
| `spread_bot/notes.py` | dealer-note reader (fi/sv/en) |
| `spread_bot/policy_v2.py` | Bayesian competitor model, v2 policy |
| `spread_bot/policy_v4.py` | **the policy the bot plays** |
| `spread_bot/__main__.py` | CLI, sweeps, the final loop |
| `tests/test_notes.py` | 43 note cases: `python3 tests/test_notes.py` |

`docs/` holds the challenge handbook, rules, API reference and the official Python kit. The OPTIMIZATION_LOG mentions `src/*.js` and `tools/` in places: those are the JS files the Python modules were ported from, same names.

## Default (setting set F) on level 8 seeds v-1…v-8
Average **+€11.2k per week**. Worst on-time share 95.3%, and all eight weeks ranked. The starter loses €72.6k on spread-1.

## How it decides (short version)

1. **Which cars to take.** Distance and slack are hard limits (450 km, 6 hours). Slack comes from the deadline, the dealer's note, the site hours and the drive time.
2. **Risk of no driver.** Per pickup town, learnt live, with a prior from the roster. The bot's own unbooked backlog in the town adds to it, as do structural risks from short slack and long distance.
3. **The price.** A Bayesian competitor model per world and per day, with a price-war prior and dealer trust. The bot quotes the price that maximises P(win) × (price − fee − risk cost × lateness shadow price). Slower options are offered only when the fast option is much riskier.
4. **Fees.** Flat (0.92 + 0.30 × risk) × the typical fee, then 1.25× at 7 hours and 1.5× at 4.5 hours before the last safe pickup. The fee goes up at once after a drop, and to the steep schedule after the deadline warning.
5. **Notes.** They set the stop windows, plates and the manual-licence audience.
