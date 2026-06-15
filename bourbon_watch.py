#!/usr/bin/env python3
"""
Bourbon phone watcher.
Polls pinned product URLs (Shopify .js first, HTML fallback) and sends a
Telegram alert the moment a bottle flips to in-stock. Designed to run on a
schedule (e.g. GitHub Actions every 15 min). State is kept in state.json so
you only get alerted once per restock, not every run.

Env vars required:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your Telegram numeric chat id
"""
import json, os, sys, datetime, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BourbonWatch/1.0)"}
SOLD_MARKERS = ["sold out", "out of stock", "notify me when available",
                "currently unavailable", "coming soon"]
CART_MARKERS = ["add to cart", "add to bag", "buy now", "add_to_cart"]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def check_shopify(url):
    """Return dict if the URL is a Shopify product (.js works), else None."""
    base = url.split("?")[0].rstrip("/")
    js = base + ".js"
    try:
        r = requests.get(js, headers=UA, timeout=20)
        if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
            return None
        data = r.json()
    except Exception:
        return None
    variants = data.get("variants", []) or []
    available = any(v.get("available") for v in variants)
    price = None
    if isinstance(data.get("price"), int):
        price = data["price"] / 100.0
    elif variants:
        prices = [v.get("price") for v in variants if isinstance(v.get("price"), int)]
        if prices:
            price = min(prices) / 100.0
    return {"available": available, "price": price, "title": data.get("title")}


def check_html(url):
    """Fallback for non-Shopify pages. Lower confidence."""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        t = r.text.lower()
    except Exception:
        return None
    sold = any(m in t for m in SOLD_MARKERS)
    addable = any(m in t for m in CART_MARKERS)
    return {"available": addable and not sold, "price": None, "title": None}


def send_telegram(token, chat_id, text):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(api, timeout=20, data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
        })
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text, file=sys.stderr)
    except Exception as e:
        print("Telegram send failed:", e, file=sys.stderr)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    config = load_json(CONFIG, {"bottles": []})
    state = load_json(STATE, {})
    in_stock = state.get("in_stock", {})

    alerts = []
    for bottle in config.get("bottles", []):
        name = bottle.get("name", "Unknown bottle")
        msrp = bottle.get("msrp")
        for listing in bottle.get("urls", []):
            url = listing.get("url")
            shop = listing.get("shop", urllib.parse.urlparse(url).netloc)
            bundle = listing.get("bundle", False)
            if not url:
                continue
            result = check_shopify(url) or check_html(url)
            now_av = bool(result and result["available"])
            was_av = in_stock.get(url, False)
            if now_av and not was_av:
                price = result.get("price") if result else None
                price_str = f"${price:,.2f}" if price else "price n/a"
                msrp_str = f" (MSRP ${msrp})" if msrp else ""
                tag = " [BUNDLE]" if bundle else ""
                alerts.append(
                    f"\U0001F983 IN STOCK: {name}{tag}\n"
                    f"{shop} - {price_str}{msrp_str}\n{url}"
                )
            in_stock[url] = now_av

    for msg in alerts:
        send_telegram(token, chat_id, msg)
        print("ALERT:", msg.replace("\n", " | "))

    if not alerts:
        print("No new in-stock flips this run.")

    # heartbeat: date changes once/day so state.json gets a daily commit,
    # which keeps the GitHub Actions schedule from auto-disabling.
    state["in_stock"] = in_stock
    state["date"] = datetime.date.today().isoformat()
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
