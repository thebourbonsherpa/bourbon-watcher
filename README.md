# Bourbon Phone Watcher (v2)

A tiny 24/7 watcher that pings your phone via Telegram the moment one of your
target bottles flips to in-stock. Runs free on GitHub Actions. No computer of
yours needs to be on.

v2 change: instead of tracking fixed product URLs, it **searches every shop on
your roster for each bottle's keywords every run** and alerts on any match that
goes in-stock. New listings at any roster shop are found automatically. You only
ever edit the config to add or drop a whole shop or a whole bottle.

This is the complement to the Cowork search: Cowork does broad discovery across
the web every 2 hours; this hammers your known roster every ~15 minutes for fast
push alerts.

## What you get
- Searches each shop (Shopify search endpoint) for each bottle's keywords.
- Tight name-matching so it fires on the right bottle (e.g. only the Eddie 45th
  RR13, not the standard 13 or last year's release).
- Alerts only when a match goes from sold-out to in-stock (no repeat spam).
- Message includes bottle, shop, the exact product title, price vs MSRP, and a
  tap-through buy link.
- Text the bot `/status` any time for a full snapshot of where every bottle
  stands right now (see below).

## One-time setup (about 15 minutes)

### 1. Make a Telegram bot
1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, follow the prompts, name it anything (e.g. "Bourbon Watch").
3. BotFather gives you a **bot token** like `8123456789:AAH...`. Save it.
4. Open a chat with your new bot and send it any message (lets it DM you).
5. Get your **chat id**: message **@userinfobot**, it replies with your numeric
   id. That's your `TELEGRAM_CHAT_ID`.

### 2. Put the files in a GitHub repo
1. Create a free GitHub account if you don't have one.
2. Create a new **public** repository (e.g. `bourbon-watcher`). Public keeps
   GitHub Actions unlimited-free; your secrets stay encrypted regardless, and
   only the bourbon config is visible.
3. Upload the contents of this `phone-watcher` folder to the repo root, keeping
   the `.github/workflows/watch.yml` path intact.

### 3. Add your secrets
Repo **Settings -> Secrets and variables -> Actions -> New repository secret**:
- `TELEGRAM_BOT_TOKEN` = the token from BotFather
- `TELEGRAM_CHAT_ID` = your numeric chat id

### 4. Turn it on and test
1. **Actions** tab, enable workflows if prompted.
2. **bourbon-watch -> Run workflow** to fire it once now.
3. Check the run log. It prints how many listings it checked and how many alerts
   fired. If nothing's in stock you get no Telegram message (that's success too).
4. From then on it runs automatically every ~15 minutes.

## Ask for a status any time
Text your bot `/status` (or `/snapshot`). On its next run it replies with a card
for every bottle, sorted so the ones worth acting on sit up top:

- **✅ under cap, in stock** first, then **🔺 over cap, in stock**, then **⚪ all
  out**, then bottles with no listings.
- **🆕** marks a bottle that flipped to in-stock since the last scan.
- A price move since the last scan shows a **🟢 down** or **🔴 up** arrow with
  the dollar change.
- Each card shows the cheapest price, the shop, whether it clears your cap, and
  how many listings are in stock.

The header says how many shops were reached. If any weren't, it names them and
why (timeout, rate-limit, etc.), so a thin run like 19/29 is explained rather
than a mystery. A shop that just times out is carried forward, so it won't fake
a bottle going out and coming back.

## Maintaining it (rare, batch edits only)
Edit `config.json`:
- **Add/drop a shop:** add or remove a line in `shops` (just a name + domain).
- **Add/drop a bottle:** add an entry under `bottles` with `query` (the search
  term) and match rules: `match_all` (every term must be in the title),
  `match_any` (at least one must be), `exclude` (none may be).
That's it. No per-URL upkeep, because it discovers listings by searching.

## Good to know
- **Cost:** free. Public repo = unlimited Actions minutes; Telegram is free.
- **Coverage:** uses each shop's Shopify search first; if that returns nothing
  (some shops replace native search with an app), it falls back to scanning the
  shop's public products.json feed, so search-app shops are still covered.
  Truly non-Shopify shops (e.g. ReserveBar, Corkery) are skipped here; the
  Cowork 2-hour sweep covers those. The weekly heartbeat reports how many shops
  were actually visible and names any it couldn't reach, so a shop going dark
  won't pass unnoticed. A `/status` reply shows the same reachability detail on
  demand.
- **Timing:** GitHub may delay a scheduled run a few minutes under load. Normal.
- **Staying alive:** the watcher writes a dated heartbeat to `state.json` so the
  repo gets a commit ~daily, keeping GitHub from auto-pausing the schedule.
- **Want real SMS instead of Telegram?** Swap the `send_telegram` function for a
  Twilio call (needs a Twilio account, a number, and US A2P registration).
  Telegram is recommended: free and no registration.
