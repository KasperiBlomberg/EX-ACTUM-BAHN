# Spread bot

`spread-bot.mjs` is one file with no dependencies. It needs Node 22 or later. **Read `OPTIMIZATION_LOG.md` for everything that was tried and measured.**

```sh
export SPREAD_URL=https://spread.bahnexpress.com
export SPREAD_KEY=<team key>

node spread-bot.mjs --level 8 --seed v-1                      # one practice week
node spread-bot.mjs --seeds v-9,v-10,v-11,v-12,v-13,v-14,v-15,v-16   # 8 weeks in parallel + total
node spread-bot.mjs --seeds v-1,v-2 --params '{"backlogB":12,"maxKm":550}'   # override settings (here: G)
node spread-bot.mjs --level 8 --board                         # next ranking run (it counts!)
caffeinate -i node spread-bot.mjs --final                     # test final / final: waits, then plays
```

**In the final**, the bot polls `/events`, answers each event straight away, and saves what it has learnt to `spread-state.json` every 2 seconds. After a crash, run the same command again: it rebuilds its orders from the server and reloads the state.

- **Edit** the files in `src/`, then run `./build.sh` to rebuild `spread-bot.mjs`.
- **Test** the note reader with `node tests/notes.test.mjs`.
- **`tools/`** holds the browser harness used to simulate a final clock, and the direct-offer policy that didn't pay off.

## Default (setting set F) on level 8 seeds v-1…v-8
Average **+€11.2k per week**. Worst on-time share 95.3%, and all eight weeks ranked. The starter loses €72.6k on spread-1.

## How it decides (short version)
1. **Which cars to take.** Distance and slack are hard limits (450 km, 6 hours). Slack comes from the deadline, the dealer's note, the site hours and the drive time.
2. **Risk of no driver.** Per pickup town, learnt live, with a prior from the roster. The bot's own unbooked backlog in the town adds to it, as do structural risks from short slack and long distance.
3. **The price.** A Bayesian competitor model per world and per day, with a price-war prior and dealer trust. The bot quotes the price that maximises P(win) × (price − fee − risk cost × lateness shadow price). Slower options are offered only when the fast option is much riskier.
4. **Fees.** Flat (0.92 + 0.30 × risk) × the typical fee, then 1.25× at 7 hours and 1.5× at 4.5 hours before the last safe pickup. The fee goes up at once after a drop, and to the steep schedule after the deadline warning.
5. **Notes.** They set the stop windows, plates and the manual-licence audience.
