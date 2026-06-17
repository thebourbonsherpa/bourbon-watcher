#!/usr/bin/env python3
"""
Bourbon phone watcher (v4 - keyword search + products.json fallback,
price ceilings, alert notes, weekly heartbeat with health reporting).

Primary path: search each roster shop via Shopify /search/suggest.json.
Fallback: if a shop's suggest returns nothing (common when a store replaces
native search with an app like Searchanise/Boost/Algolia), scan its public
/products.json feed instead. Either way, match the right product, apply the
price ceiling, and Telegram-alert when it goes in-stock at/under the ceiling.
A periodic heartbeat reports how many shops we can actually see, so silent or
"alive-but-blind" failure can't hide.

Env vars required:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your Telegram numeric chat id
"""
import json, os, sys, datetime, urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BourbonWatch/4.0)"}
FEED_PAGES = 2  # products.json pages to scan in fallback (250 each, newest-first)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def get_json(url):
    try:
        r = requests.get(url, headers=UA, timeout=25)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def suggest_products(domain, query):
    """Native Shopify search. Returns list of products, or None if the endpoint
    failed/isn't usable."""
    q = urllib.parse.quote(query)
    url = (f"https://{domain}/search/suggest.json?q={q}"
           f"&resources[type]=product&resources[limit]=10")
    data = get_json(url)
    if data is None:
        return None
    try:
        return data["resources"]["results"]["products"]
    except Exception:
        return None


def feed_products(domain, max_pages=FEED_PAGES):
    """Fallback: pull the public /products.json catalog (works even when a shop
    uses a third-party search app). Returns a list of products."""
    out = []
    for page in range(1, max_pages + 1):
        data = get_json(f"https://{domain}/products.json?limit=250&page={page}")
        if not data:
            break
        prods = data.get("products", [])
        if not prods:
            break
        out.extend(prods)
        if len(prods) < 250:
            break
    return out


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
            "chat_id": chat_id, "text": text, "disable_web_page_preview": "false",
        })
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text, file=sys.stderr)
    except Exception as e:
        print("Telegram send failed:", e, file=sys.stderr)


def consider(bottle, shop_name, shop_note, purl, title, available, price,
             in_stock, alerts, min_price=0):
    """Decide whether this candidate is a fresh, affordable, in-stock hit.

    A qualifying hit must have a REAL price that clears a sanity floor and
    sits at/under the ceiling. Requiring a present price >= min_price blocks
    the $0.00 / $2.00 placeholder listings some shops use for allocated
    bottles - without this, a junk listing flipping to 'available' would
    fire a false alert (a blank or zero price used to slip past the ceiling
    check). Trade-off: a legitimate listing that exposes no price at all is
    skipped rather than alerted; in practice in-stock allocated bottles
    always carry a price."""
    max_price = bottle.get("max_price")
    price_ok = (price is not None and price >= min_price
                and (max_price is None or price <= max_price))
    qualifies = bool(available) and price_ok
    was = in_stock.get(purl, False)
    if qualifies and not was:
        price_str = f"${price:,.2f}" if price else "price n/a"
        msrp = bottle.get("msrp")
        msrp_str = f" (MSRP ${msrp})" if msrp else ""
        lines = [
            f"\U0001F983 IN STOCK: {bottle.get('name')}",
            f"{shop_name} - {price_str}{msrp_str}",
            f"\"{title}\"",
            purl,
        ]
        if shop_note:
            lines.append(f"⚠ {shop_note}")
        lines.append("Confirm it ships to MI and the final landed price at checkout.")
        alerts.append("\n".join(lines))
    in_stock[purl] = qualifies


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
    default_floor = config.get("min_price", 0)  # global junk-price floor

    monitored_domains = set()
    feed_used = []
    alerts = []

    for shop in shops:
        domain = shop.get("domain")
        shop_name = shop.get("name", domain)
        shop_note = shop.get("note")
        if not domain:
            continue

        got_suggest = False
        for bottle in bottles:
            prods = suggest_products(domain, bottle.get("query", bottle.get("name", "")))
            if prods:
                got_suggest = True
                for p in prods:
                    if not title_matches(p.get("title", ""), bottle):
                        continue
                    rel = (p.get("url") or "").split("?")[0]
                    purl = f"https://{domain}{rel}"
                    consider(bottle, shop_name, shop_note, purl, p.get("title"),
                             p.get("available"), to_price(p.get("price")),
                             in_stock, alerts,
                             bottle.get("min_price", default_floor))

        # Fallback: suggest gave nothing (search-app shop or no matches at all).
        if got_suggest:
            monitored_domains.add(domain)
        else:
            feed = feed_products(domain)
            if feed:
                monitored_domains.add(domain)
                feed_used.append(domain)
                for bottle in bottles:
                    for p in feed:
                        if not title_matches(p.get("title", ""), bottle):
                            continue
                        handle = p.get("handle", "")
                        purl = f"https://{domain}/products/{handle}"
                        variants = p.get("variants", []) or []
                        available = any(v.get("available") for v in variants)
                        prices = [to_price(v.get("price")) for v in variants]
                        prices = [x for x in prices if x]
                        price = min(prices) if prices else None
                        consider(bottle, shop_name, shop_note, purl, p.get("title"),
                                 available, price, in_stock, alerts,
                                 bottle.get("min_price", default_floor))

    for msg in alerts:
        send_telegram(token, chat_id, msg)
        print("ALERT:", msg.replace("\n", " | "))

    total = len(shops)
    monitored = len(monitored_domains)
    print(f"Monitored {monitored}/{total} shops "
          f"({len(feed_used)} via products.json fallback). {len(alerts)} new alert(s).")

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
        blind = total - monitored
        blind_note = f" {blind} not visible - check config." if blind else ""
        send_telegram(token, chat_id,
            f"\U0001F7E2 Bourbon watcher alive - {today.isoformat()}. "
            f"{monitored}/{total} shops visible.{blind_note} "
            f"Hits arrive separately; if this stops, something broke.")
        print(f"Heartbeat sent ({monitored}/{total} visible).")
        state["last_heartbeat"] = today.isoformat()

    state["in_stock"] = in_stock
    state["date"] = today.isoformat()
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
