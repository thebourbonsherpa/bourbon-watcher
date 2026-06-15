# Bourbon Phone Watcher

A tiny 24/7 watcher that pings your phone via Telegram the moment one of your
target bottles flips to in-stock. Runs free on GitHub Actions. No computer of
yours needs to be on. This is the complement to the Cowork search: Cowork does
discovery and the 2-hour broad sweep; this hammers the exact product URLs every
~15 minutes for fast push alerts.

## What you get
- Checks each product URL (Shopify stock endpoint first, HTML fallback).
- Alerts only when a bottle goes from sold-out to in-stock (no repeat spam).
- Message includes bottle, shop, price, MSRP, and a tap-through buy link.

## One-time setup (about 15 minutes)

### 1. Make a Telegram bot
1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, follow the prompts, name it anything (e.g. "Bourbon Watch").
3. BotFather gives you a **bot token** like `8123456789:AAH...`. Save it.
4. Open a chat with your new bot and send it any message (this lets it DM you).
5. Get your **chat id**: in Telegram, message **@userinfobot**, it replies with
   your numeric id (e.g. `123456789`). That's your `TELEGRAM_CHAT_ID`.

### 2. Put the files in a GitHub repo
1. Create a free GitHub account if you don't have one.
2. Create a new **public** repository (e.g. `bourbon-watcher`). Public keeps
   GitHub Actions unlimited-free; your secrets stay encrypted regardless, and
   only the bourbon URLs are visible.
3. Upload the contents of this `phone-watcher` folder to the repo root, keeping
   the `.github/workflows/watch.yml` path intact. (Drag-and-drop in the GitHub
   web UI works; make sure the `.github` folder uploads.)

### 3. Add your secrets
In the repo: **Settings -> Secrets and variables -> Actions -> New repository secret**.
Add two:
- `TELEGRAM_BOT_TOKEN` = the token from BotFather
- `TELEGRAM_CHAT_ID` = your numeric chat id

### 4. Turn it on and test
1. Go to the **Actions** tab, enable workflows if prompted.
2. Click **bourbon-watch -> Run workflow** to fire it once now.
3. Check the run log. If a bottle is in stock you'll get a Telegram message. If
   not, it logs "No new in-stock flips this run" (that's success too).
4. From then on it runs automatically every ~15 minutes.

## Adding or changing bottles
Edit `config.json`. Each bottle has a name, MSRP, and a list of `urls` with a
`shop` label and the product `url`. Add a `"bundle": true` flag if the listing
is a bundle. Commit the change and the next run picks it up. Get fresh product
URLs from the Cowork search anytime.

## Good to know
- **Cost:** free. Use a public repo so Actions minutes are unlimited (a private
  repo's 2,000 free min/month would not cover a 15-min cadence). Telegram is free.
- **Timing:** GitHub may delay a scheduled run a few minutes under load. Normal.
- **Staying alive:** the watcher writes a dated heartbeat to `state.json` so the
  repo gets a commit ~daily, which keeps GitHub from auto-pausing the schedule.
- **Want real SMS instead of Telegram?** Swap the `send_telegram` function in
  `bourbon_watch.py` for a Twilio call (needs a Twilio account, a number, and
  US A2P registration). Telegram is recommended: free and no registration.
- **New URLs:** this watches specific product pages. A brand-new listing at a
  new URL won't be caught until you add it. The Cowork 2-hour sweep is what
  finds those, then you paste them into `config.json`.
