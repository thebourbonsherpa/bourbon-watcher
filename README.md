# Bourbon Phone Watcher (v5.2)

A tiny 24/7 watcher that pings your phone via Telegram the moment one of your
target bottles flips to in-stock at or under your price cap. Runs free on
GitHub Actions. No computer of yours needs to be on.

## How it scans (v5 hybrid)
Every run, each roster shop gets checked TWO ways:

1. **Feed pass (every shop, every run):** reads the shop's public
   products.json feed - the newest ~750 products. Catches newly created
   listings fast.
2. **Search pass (rotating slice):** queries the shop's native Shopify search
   once per bottle. Catches RESTOCKS of listings created long ago - on big
   catalogs those sit far beyond the feed window, so the feed alone would
   miss the classic allocated restock. Each run searches
   `suggest_shops_per_run` shops (default 15), so every shop gets a restock
   sweep every ~3 runs (~15 min at a 5-minute trigger cadence). Shops whose
   feed fails or is empty are always searched - it's their only coverage.

Shops are scanned in parallel, but requests are paced twice: per shop AND
globally (~2.5 requests/sec total). Most roster stores share Shopify's edge
network, which rate-limits per client IP ACROSS stores - without the global
cap, 30+ shops 429 at once. HTTP 429 and 430 (Shopify's bot-rejection code)
are retried with backoff; timeouts get one retry; two consecutive hard
failures abort that shop's search pass for the run. A run takes ~2-3 minutes.

## Alert rules
- A hit must be in stock AND have a real price between the junk floor
  (`min_price`, global $50, per-bottle override) and the bottle's
  `max_price`. The floor blocks $0.00/$2.00 placeholder listings that some
  shops use for allocated bottles.
- The quoted price is the cheapest AVAILABLE variant - never a sold-out
  cheaper variant.
- Alerts fire once per listing per stock cycle (no repeat spam) and are only
  marked "sent" after Telegram confirms delivery - a failed send re-fires
  next run.
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
  that rides along in alerts as a caution tag).
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
- Bottles live in TWO places: config.json (this watcher) and watchlist.md
  (the Cowork sweep). Keep them in sync or one layer goes blind.
- Non-Shopify shops (ReserveBar, Corkery, Trackside, The Liquor Book) can't
  be watched here - they're covered by the Cowork layer.
- cron-job.org's GitHub token expires ~June 2027; renew it or the trigger
  silently 401s (the heartbeat stopping is the tell).

## Cost
Free. Public repo = unlimited Actions minutes; Telegram is free.
