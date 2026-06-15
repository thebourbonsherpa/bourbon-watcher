#!/usr/bin/env python3
"""
Bourbon phone watcher (v2 - shop + keyword auto-discovery).

Instead of pinning exact product URLs, this searches each shop on your roster
for each bottle's keywords every run (via Shopify's /search/suggest.json), then
alerts via Telegram when a matching product flips to in-stock. New listings at
any roster shop are found automatically - no per-URL maintenance.

You only edit config.json to add/remove a whole SHOP or a whole BOTTLE.

Env vars required:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your Telegram numeric chat id
"""
import json, os, sys, datetime, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BourbonWatch/2.0)"}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def search_shop(domain, query):
    """Query a Shopify shop's suggest endpoint. Returns a list of products
    (empty if the shop isn't Shopify or the call fails)."""
    q = urllib.parse.quote(query)
    url = (f"https://{domain}/search/suggest.json?q={q}"
           f"&resources[type]=product&resources[limit]=10")
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        return data["resources"]["results"]["products"]
    except Exception:
        return []


def title_matches(title, rule):
    t = (title or "").lower()
    for x in rule.get("exclude", []):
        if x.lower() in t:
            return False
    for x in rule.get("match_all", []):
        if x.lower() not in t:
            return False
    any_terms = rule.get("match_any")
    if any_terms and not any(x.lower() in t for x in any_terms):
        return False
    return True


def to_price(val):
    try:
        p = float(val)
        return p if p > 0 else None
    except Exception:
        return None


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

    config = load_json(CONFIG, {"bottles": [], "shops": []})
    state = load_json(STATE, {})
    in_stock = state.get("in_stock", {})

    bottles = config.get("bottles", [])
    shops = config.get("shops", [])
    alerts = []
    checked = 0

    for shop in shops:
        domain = shop.get("domain")
        shop_name = shop.get("name", domain)
        if not domain:
            continue
        for bottle in bottles:
            products = search_shop(domain, bottle.get("query", bottle.get("name", "")))
            for p in products:
                if not title_matches(p.get("title", ""), bottle):
                    continue
                checked += 1
                rel = (p.get("url") or "").split("?")[0]
                purl = f"https://{domain}{rel}"
                price = to_price(p.get("price"))
                max_price = bottle.get("max_price")
                # within ceiling if no ceiling set, price unknown (fail-open so
                # we don't miss a real one), or price at/under the ceiling
                price_ok = (max_price is None) or (price is None) or (price <= max_price)
                # "qualifies" = in stock AND affordable. We key dedup on this so
                # a later price drop into range still triggers a fresh alert.
                qualifies = bool(p.get("available")) and price_ok
                was = in_stock.get(purl, False)
                if qualifies and not was:
                    price_str = f"${price:,.2f}" if price else "price n/a"
                    msrp = bottle.get("msrp")
                    msrp_str = f" (MSRP ${msrp})" if msrp else ""
                    alerts.append(
                        f"\U0001F983 IN STOCK: {bottle.get('name')}\n"
                        f"{shop_name} - {price_str}{msrp_str}\n"
                        f"\"{p.get('title')}\"\n{purl}"
                    )
                in_stock[purl] = qualifies

    for msg in alerts:
        send_telegram(token, chat_id, msg)
        print("ALERT:", msg.replace("\n", " | "))

    print(f"Checked {checked} matching listings across {len(shops)} shops. "
          f"{len(alerts)} new in-stock alert(s).")

    # heartbeat: date changes once/day so state.json gets a daily commit,
    # which keeps the GitHub Actions schedule from auto-disabling.
    state["in_stock"] = in_stock
    state["date"] = datetime.date.today().isoformat()
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
