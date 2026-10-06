# Bourbon Phone Watcher (v5.4.1)

A tiny 24/7 watcher that pings your phone via Telegram the moment one of your
target bottles flips to in-stock at or under your price cap. Runs free on
GitHub Actions. No computer of yours needs to be on.

## How it scans (v5 hybrid)
Every run, each roster shop gets checked TWO ways:

1. **Feed pass (every shop, every run):** reads the shop's public
   products.json feed - the newest ~500 products. Catches newly created
   listings fast.
2. **Search pass (rotating slice):** queries the shop's native Shopify search
   once per bottle. Catches RESTOCKS of listings created long ago - on big
   catalogs those sit far beyond the feed window, so the feed alone would
   miss the classic allocated restock. Each run searches
   `suggest_shops_per_run` shops (default 15), so every shop gets a restock
   sweep every ~3 runs (~15 min at a 5-minute trigger cadence). Shops whose
   feed fails or is empty are always searched - it's their only coverage.

Shops are scanned in parallel, but requests are paced twice: per shop AND
globally. Most roster stores share Shopify's edge network, which rate-limits
per client IP ACROSS stores, and GitHub runners share IPs with other
scrapers, so some runs start with the IP's budget already half spent.

Throttle handling (v5.4):
- **Adaptive pacer.** Normal speed is ~2.5 requests/sec. Any HTTP 429/430
  slows the WHOLE watcher (interval doubles, up to 1.6s, and everyone
  pauses until 3s from now - pauses never stack); every 10 successes eases
  it back toward normal.
- **Hard time budget.** No request starts after 190s in the main scan or
  235s in the retry sweep, so a run throttled start to finish still ends
  in ~4 minutes, sends its alerts and saves state. Shops cut off by the
  budget show "run budget" in the unreachable list.
- **Hot shops first, rest shuffled.** Shops marked `"hot": true` in
  config.json are scanned first every run; the rest are shuffled. A
  throttled run used to lose the same tail of the roster every time.
- **Retry sweep.** After a 15s cool-down, any shop that went dark gets one
  more feed-only pass (no new retry work starts after ~230s).
- Two consecutive hard failures abort a shop's search pass for the run.

A clean run takes ~2 minutes; a throttled run up to ~4.

## Alert rules
- A hit must be in stock AND have a real price between the junk floor
  (`min_price`, global $50, per-bottle override) and the bottle's
  `max_price`. The floor blocks $0.00/$2.00 placeholder listings that some
  shops use for allocated bottles.
- The quoted price is the cheapest AVAILABLE variant - never a sold-out
  cheaper variant.
- `global_exclude` (config) rides on every bottle: bundles, combos, gift
  boxes, mystery boxes, "spend $X get it for $Y" listings, empties. Watch
  for substring traps when adding terms: "50ml" matched every "750ml" title.
- `gate_tags` (config, default `unavailable`): a product carrying one of
  these Shopify tags is treated as not buyable. Some shops (The Liquor Barn)
  keep allocated bottles listed at MSRP and "available" while a tag
  disables the cart. Pre-order tags are deliberately NOT gated: buyable
  pre-orders alert (that's how the Bulleit 20 was caught).
- Early warning (`early_warning`, `early_warning_hours`, default 72):
  a one-time NEW LISTING ping when a matching listing first appears but
  isn't buyable yet (placeholder price, sold out, coming soon, tagged). It
  means the shop is about to drop. Only listings created/published within
  the window count, and a shop/bottle pair must have had one full search
  pass first, so old listings never ping. Max 5 per run plus one overflow
  summary.
- Before a search-pass hit can alert, the watcher re-reads the product's
  `.js` (live tags, availability, price). Search results carry no tags and
  can lag; this costs one extra request per would-be alert. If that check
  itself fails, the hit is HELD, not sent (v5.3.1 - failing open let a
  tag-gated Liquor Barn listing alert twice), and re-checked next pass.
- Alerts fire once per listing per stock cycle (no repeat spam) and are only
  marked "sent" after Telegram confirms delivery - a failed send re-fires
  next run. state.json keeps only alerted listings (v5.3).
- Shop caution notes from config.json ride along in the alert, plus a
  "confirm it ships to you + landed price" footer. Some roster shops are
  no-MI/ship-to-NC - the note says so.

## Ask for a status any time
Text the bot `/status` (or `/snapshot`). The next run replies with a card per
bottle, sorted most-actionable first: ✅ in stock under cap, 🔺 in stock over
cap, ⚪ all out, then no listings. 🆕 marks a fresh flip to in-stock; 🟢/🔴
arrows show price moves since the last scan. The header reports how many
shops were reached, names the ones that weren't (with reasons), and
separately flags any shop dark 1+ days - that's a real coverage hole, not a
blip.

## Self-monitoring
- **Weekly heartbeat** to Telegram: "alive, X/N shops visible," naming
  unreached shops and flagging persistent-dark ones.
- **Broken config guard:** if config.json is missing, invalid JSON, or has no
  bottles/shops, the bot Telegrams you once a day instead of silently
  scanning nothing.
- **Job backstop:** the workflow kills any run at 10 minutes so a wedged run
  can't block the queue.
- **Public run summary.** Every run writes `last_run` and a rolling
  `run_history` (last 100 runs, ~8 hours) into state.json: shops reached,
  unreachable shops with reasons, alerts and early warnings found/sent,
  Telegram failures, throttle hits, pacer peak, shops recovered on retry,
  and per-bottle listings / in stock / under cap / cheapest. The repo is
  public, so this is readable without a GitHub login - ask Claude to
  "check the watcher."

## One-time setup (about 15 minutes)

### 1. Make a Telegram bot
1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, follow the prompts, name it anything (e.g. "Bourbon Watch").
3. BotFather gives you a **bot token** like `8123456789:AAH...`. Save it.
4. Open a chat with your new bot and send it any message (lets it DM you).
5. Get your **chat id**: message **@userinfobot**, it replies with your
   numeric id. That's your `TELEGRAM_CHAT_ID`.

### 2. Put the files in a GitHub repo
1. Create a new **public** repository (public = unlimited free Actions).
2. Upload the contents of this `phone-watcher` folder to the repo root,
   keeping the `.github/workflows/watch.yml` path intact.

### 3. Add your secrets
Repo **Settings -> Secrets and variables -> Actions -> New repository secret**:
- `TELEGRAM_BOT_TOKEN` = the token from BotFather
- `TELEGRAM_CHAT_ID` = your numeric chat id

### 4. Turn it on and test
**Actions** tab -> enable workflows -> **bourbon-watch -> Run workflow**.
Check the log: it prints "Monitored X/N shops" and the alert count. From then
on it runs on schedule (a cron-job.org job hitting workflow_dispatch every
~5 min beats GitHub's own 15-min cron; either works).

## Maintaining it (config.json)
- **Add/drop a shop:** one line in `shops` (name + domain; optional `note`
  that rides along in alerts as a caution tag; optional `"hot": true` to
  scan it first every run - keep hot shops to ~8, the worker count).
- **Add/drop a bottle:** a block in `bottles` with `query` + match rules:
  `match_all` (every term in title), `match_any` (at least one), `exclude`
  (none). ALWAYS live-test new rules against shop search first - the feed
  pass scans whole catalogs with match rules alone, so a loose rule
  (e.g. bare "beacon") matches decoys. Anchor on the most stable unique word.
- **Re-price:** `max_price` per bottle; junk floor via global `min_price` or
  per-bottle `min_price`.
- **Search-pass budget:** `suggest_shops_per_run` (default 15).
- config.json and bourbon_watch.py are a matched pair when price logic
  changes - commit them together.

## Warnings
- **NEVER re-upload a local/blank state.json.** The live one on GitHub holds
  alert history, the dark-shop counters, and the Telegram offset.
  Overwriting it re-fires old alerts and replays commands.
- config.json is the only live bottle list. The Cowork sweep was retired
  (Oct 2026); watchlist.md is reference only.
- Non-Shopify shops (ReserveBar, Corkery, Trackside, The Liquor Book) can't
  be watched here and are no longer covered by anything automated.
- cron-job.org's GitHub token expires ~June 2027; renew it or the trigger
  silently 401s (the heartbeat stopping is the tell).

## Cost
Free. Public repo = unlimited Actions minutes; Telegram is free.

## Version history
- **5.4.1** (2026-10-06) - hard run budget; throttle pauses no longer
  stack. 5.4 hung on a sustained storm until GitHub's 10-minute kill, so
  runs sent nothing and queued runs were cancelled.
- **5.4** (2026-10-06) - adaptive global pacer, hot shops first + shuffled
  order, end-of-run retry sweep, feed window 750 -> 500.
- **5.3.1** (2026-10-06) - search-pass confirm fails closed.
- **5.3** (2026-10-05) - global excludes, tag gating, early-warning NEW
  LISTING pings, pre-orders alert, public run summary, state trimmed.
- **5.2** (2026-07-09) - global pacer, 430 handling, config guard,
  dark-shop tracking.
