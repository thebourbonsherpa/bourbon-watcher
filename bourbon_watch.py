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
import json, os, sys, time, datetime, urllib.parse
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


def send_telegram(token, chat_id, text, retries=3):
    """Send a Telegram message. Returns True only if delivery is confirmed
    (HTTP 200). Retries a few times with backoff so a transient blip doesn't
    silently drop the message. The caller uses the return value to decide
    whether to mark a hit as 'already alerted' - a hit is never recorded as
    sent unless it actually was."""
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(api, timeout=20, data={
                "chat_id": chat_id, "text": text, "disable_web_page_preview": "false",
            })
            if r.status_code == 200:
                return True
            print(f"Telegram error (attempt {attempt}/{retries}):",
                  r.status_code, r.text, file=sys.stderr)
        except Exception as e:
            print(f"Telegram send failed (attempt {attempt}/{retries}):", e,
                  file=sys.stderr)
        if attempt < retries:
            time.sleep(2 * attempt)
    return False


def check_snapshot_request(token, chat_id, state):
    """Poll Telegram for a /snapshot (or /status) command from the authorized
    chat since we last checked. Advances the stored update offset so each
    command is handled exactly once. Returns True if a snapshot was requested.
    Only the configured chat_id is honored - the bot ignores everyone else."""
    last = state.get("last_update_id", 0)
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, timeout=20,
                         params={"offset": last + 1, "timeout": 0,
                                 "allowed_updates": '["message"]'})
        data = r.json()
    except Exception as e:
        print("getUpdates failed:", e, file=sys.stderr)
        return False
    if not data.get("ok"):
        return False
    requested = False
    max_id = last
    for upd in data.get("result", []):
        uid = upd.get("update_id", 0)
        if uid > max_id:
            max_id = uid
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        cmd = text.split("@")[0].split()[0].lower() if text else ""
        if chat == str(chat_id) and cmd in ("/snapshot", "/status"):
            requested = True
    state["last_update_id"] = max_id
    return requested


def build_snapshot(matches, bottles, monitored, total, today):
    """One concise line per bottle: in-stock price + shop and whether it's
    under cap; if all out, the cheapest listing to watch. Phone-friendly."""
    lines = [f"\U0001F4F8 Snapshot {today} - {monitored}/{total} shops visible"]
    if monitored < total:
        lines.append(f"(note: {total - monitored} shop(s) not reachable this run)")
    for b in bottles:
        name = b.get("name")
        cap = b.get("max_price")
        cap_str = f"${cap}" if cap else "no cap"
        ms = [m for m in matches if m["bottle"] == name]
        instock = [m for m in ms if m["available"] and m["price"]]
        under = [m for m in instock
                 if (cap is None or m["price"] <= cap) and m["price"] >= m.get("floor", 0)]
        if not ms:
            lines.append(f"• {name} ({cap_str}): no listings found")
        elif instock:
            cheap = min(instock, key=lambda m: m["price"])
            shop = cheap["shop"].replace("www.", "")
            flag = "  ✅ UNDER CAP" if under else " (over cap)"
            extra = ""
            out_under = [m for m in ms if not m["available"] and m["price"]
                         and (cap is None or m["price"] <= cap)
                         and m["price"] >= m.get("floor", 0)]
            if not under and out_under:
                w = min(out_under, key=lambda m: m["price"])
                extra = (f"; watch {w['shop'].replace('www.','')} "
                         f"${w['price']:,.0f} (OUT, under cap)")
            lines.append(f"• {name} ({cap_str}): in stock ${cheap['price']:,.0f} "
                         f"@ {shop}{flag} [{len(instock)}/{len(ms)} in stock]{extra}")
        else:
            cheap = min(ms, key=lambda m: m["price"] or 9e9)
            price_str = f"${cheap['price']:,.0f}" if cheap["price"] else "n/a"
            shop = cheap["shop"].replace("www.", "")
            lines.append(f"• {name} ({cap_str}): all out ({len(ms)} listed) "
                         f"- lowest {price_str} @ {shop}")
    return "\n".join(lines)


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
        alerts.append((purl, "\n".join(lines)))
        # Do NOT mark this purl as alerted yet. main() sets in_stock[purl]=True
        # only after Telegram confirms delivery; if the send fails, the hit
        # stays un-recorded and re-fires next run instead of being lost.
        return
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
    matches = []   # full current landscape, for on-demand /snapshot replies

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
                    matches.append({"bottle": bottle.get("name"), "shop": shop_name,
                                    "available": bool(p.get("available")),
                                    "price": to_price(p.get("price")),
                                    "floor": bottle.get("min_price", default_floor)})

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
                        matches.append({"bottle": bottle.get("name"),
                                        "shop": shop_name, "available": available,
                                        "price": price,
                                        "floor": bottle.get("min_price", default_floor)})

    for purl, msg in alerts:
        if send_telegram(token, chat_id, msg):
            in_stock[purl] = True   # record as alerted only on confirmed delivery
            print("ALERT:", msg.replace("\n", " | "))
        else:
            print("ALERT NOT DELIVERED (will retry next run):", purl,
                  file=sys.stderr)

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
        if send_telegram(token, chat_id,
            f"\U0001F7E2 Bourbon watcher alive - {today.isoformat()}. "
            f"{monitored}/{total} shops visible.{blind_note} "
            f"Hits arrive separately; if this stops, something broke."):
            print(f"Heartbeat sent ({monitored}/{total} visible).")
            state["last_heartbeat"] = today.isoformat()  # only if delivered

    # ---- On-demand snapshot: if you texted /snapshot or /status since the
    # last run, reply with the current per-bottle landscape from this scan.
    if check_snapshot_request(token, chat_id, state):
        summary = build_snapshot(matches, bottles, monitored, total,
                                 today.isoformat())
        if send_telegram(token, chat_id, summary):
            print("Snapshot sent on request.")
        else:
            print("Snapshot send failed.", file=sys.stderr)

    state["in_stock"] = in_stock
    state["date"] = today.isoformat()
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
