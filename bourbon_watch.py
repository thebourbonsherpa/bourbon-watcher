#!/usr/bin/env python3
"""
Bourbon phone watcher (v3 - shop+keyword search, price ceilings, alert notes,
weekly heartbeat with endpoint-health reporting).

For each bottle, searches every roster shop (Shopify /search/suggest.json),
matches the right product, applies a price ceiling, and Telegram-alerts when a
match goes in-stock at/under the ceiling. Sends a periodic heartbeat so silent
failure can't hide, and reports how many shop endpoints are healthy.

Env vars required:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your Telegram numeric chat id
"""
import json, os, sys, datetime, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BourbonWatch/3.0)"}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def search_shop(domain, query):
    """Query a Shopify suggest endpoint.
    Returns (ok, products): ok=True if the endpoint returned valid Shopify JSON
    (even with zero products), False if it errored or isn't Shopify."""
    q = urllib.parse.quote(query)
    url = (f"https://{domain}/search/suggest.json?q={q}"
           f"&resources[type]=product&resources[limit]=10")
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return (False, [])
        data = r.json()
        products = data["resources"]["results"]["products"]
        return (True, products)
    except Exception:
        return (False, [])


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
    healthy_domains = set()
    alerts = []

    for shop in shops:
        domain = shop.get("domain")
        shop_name = shop.get("name", domain)
        shop_note = shop.get("note")
        if not domain:
            continue
        for bottle in bottles:
            ok, products = search_shop(domain, bottle.get("query", bottle.get("name", "")))
            if ok:
                healthy_domains.add(domain)
            for p in products:
                if not title_matches(p.get("title", ""), bottle):
                    continue
                rel = (p.get("url") or "").split("?")[0]
                purl = f"https://{domain}{rel}"
                price = to_price(p.get("price"))
                max_price = bottle.get("max_price")
                # within ceiling if no ceiling, price unknown (fail-open), or <= ceiling
                price_ok = (max_price is None) or (price is None) or (price <= max_price)
                qualifies = bool(p.get("available")) and price_ok
                was = in_stock.get(purl, False)
                if qualifies and not was:
                    price_str = f"${price:,.2f}" if price else "price n/a"
                    msrp = bottle.get("msrp")
                    msrp_str = f" (MSRP ${msrp})" if msrp else ""
                    lines = [
                        f"\U0001F983 IN STOCK: {bottle.get('name')}",
                        f"{shop_name} - {price_str}{msrp_str}",
                        f"\"{p.get('title')}\"",
                        purl,
                    ]
                    if shop_note:
                        lines.append(f"⚠ {shop_note}")
                    lines.append("Confirm it ships to MI and the final landed price at checkout.")
                    alerts.append("\n".join(lines))
                in_stock[purl] = qualifies

    for msg in alerts:
        send_telegram(token, chat_id, msg)
        print("ALERT:", msg.replace("\n", " | "))

    total = len(shops)
    responding = len(healthy_domains)
    print(f"Checked {total} shops, {responding} endpoints healthy. "
          f"{len(alerts)} new alert(s).")

    # ---- Heartbeat: periodic 'still alive' ping so silent failure can't hide.
    today = datetime.date.today()
    hb_days = config.get("heartbeat_days", 7)
    last_hb = state.get("last_heartbeat")
    due = True
    if last_hb:
        try:
            due = (today - datetime.date.fromisoformat(last_hb)).days >= hb_days
        except Exception:
            due = True
    if due:
        dead = total - responding
        dead_note = f" {dead} not responding - check config." if dead else ""
        send_telegram(token, chat_id,
            f"\U0001F7E2 Bourbon watcher alive - {today.isoformat()}\n"
            f"{responding}/{total} shop endpoints healthy.{dead_note}\n"
            f"Hits arrive separately. If this stops showing up, something broke.")
        print(f"Heartbeat sent ({responding}/{total} healthy).")
        state["last_heartbeat"] = today.isoformat()

    # daily date write keeps the repo active (backup schedule won't auto-disable)
    state["in_stock"] = in_stock
    state["date"] = today.isoformat()
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
