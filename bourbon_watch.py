#!/usr/bin/env python3
"""
Bourbon phone watcher (v5.3 - hybrid feed + per-bottle search, per-shop pacing,
parallel shops, price ceilings, alert notes, weekly heartbeat with health
reporting).

Every shop is scanned TWO ways each run:
  1. Its public /products.json feed (newest ~750 products) - catches newly
     created listings fast.
  2. Its native Shopify /search/suggest.json, once per bottle - catches
     RESTOCKS of listings created long ago. On big catalogs those sit far
     beyond the feed window (verified live: a large roster shop's feed is
     newest-first and thousands deep; BTAC-era product pages never appear
     in the first 750), so the feed alone is blind to the classic allocated
     restock. The search pass closes that gap.

Shops are scanned in parallel threads with TWO layers of pacing (v5.1):
  - per shop: MIN_INTERVAL between requests to the same store, and
  - GLOBAL: an aggregate cap across ALL workers. Most roster stores sit
    behind Shopify's shared edge, which rate-limits per client IP ACROSS
    stores - v5.0 paced only per shop, and 8 workers from one runner IP
    tripped the platform limit, blinding 30+ shops at once with 429s.
The per-bottle search (restock) pass is also budgeted: each run searches a
rotating slice of the roster (suggest_shops_per_run in config, default 15),
so every shop gets a restock sweep every ~3 runs while the feed pass still
covers every shop every run. Shops whose feed fails or is empty are always
searched, since search is their only coverage.

Env vars required:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your Telegram numeric chat id
"""
import json, os, sys, time, datetime, threading, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BourbonWatch/5.3)"}
FEED_PAGES = 3       # products.json pages per shop (250 each, newest-first)
MIN_INTERVAL = 0.5   # min seconds between requests TO THE SAME shop
GLOBAL_INTERVAL = 0.4  # min seconds between requests ACROSS ALL shops
                       # (~2.5 req/s aggregate - under Shopify's per-IP edge limit)
ATTEMPTS = 3         # tries per request; retries cover 429s AND timeouts /
                     # connection blips, which used to fail a shop instantly
MAX_WORKERS = 8      # shops scanned concurrently (global pacer governs volume)
SUGGEST_DEFAULT = 15 # shops given the per-bottle search pass per run

VERSION = "5.3"
RUN_HISTORY = 100    # compact per-run records kept in state.json
_tg_failures = [0]   # Telegram sends that never confirmed, this run

_global_lock = threading.Lock()
_global_next = [0.0]  # next allowed request time, shared by all workers


class ShopClient:
    """One per shop (and per worker thread): paces requests to that shop and
    reuses its connection. Rate limits are per shop, so pacing is per shop
    too - a global throttle just made every shop wait on every other one."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(UA)
        self.last = 0.0

    def _pace(self):
        # Per-shop spacing first.
        wait = MIN_INTERVAL - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        # Then reserve a slot in the GLOBAL schedule. Slots are handed out
        # under a lock so workers queue instead of bursting; the sleep happens
        # outside the lock so it doesn't serialize everyone else.
        with _global_lock:
            slot = max(time.monotonic(), _global_next[0])
            _global_next[0] = slot + GLOBAL_INTERVAL
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def get_json(self, url):
        """Fetch JSON. Returns (data, error): data is the parsed body on
        success and error is None; on failure data is None and error is a
        short human reason ('timeout', 'HTTP 429', 'connection error') so
        callers can explain why a shop went dark. 429s honor Retry-After;
        timeouts and connection errors get retried too instead of counting
        the shop out on the first blip."""
        err = None
        for attempt in range(ATTEMPTS):
            self._pace()
            try:
                r = self.session.get(url, timeout=15)
            except requests.exceptions.Timeout:
                err = "timeout"
                continue
            except requests.exceptions.ConnectionError:
                err = "connection error"
                continue
            except Exception as e:
                return None, type(e).__name__
            if r.status_code in (429, 430):
                # 429 = rate limited; 430 = Shopify's custom "security
                # rejection" for suspected bot traffic. Both mean back off
                # and retry - failing instantly on 430 would blind a shop
                # exactly when it is telling us to slow down.
                err = f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else 2.0 ** attempt
                except ValueError:
                    wait = 2.0 ** attempt
                time.sleep(min(wait, 5))
                continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            try:
                return r.json(), None
            except ValueError:
                return None, "bad response"
        return None, err


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def suggest_products(client, domain, query):
    """Native Shopify search. Returns (products, error). products is a list
    (possibly empty) when the endpoint actually responded; None when the
    request failed, with error giving the reason. An empty list means
    'reachable, no hits' - distinct from 'could not reach the shop'."""
    q = urllib.parse.quote(query)
    url = (f"https://{domain}/search/suggest.json?q={q}"
           f"&resources[type]=product&resources[limit]=10")
    data, err = client.get_json(url)
    if data is None:
        return None, err
    try:
        return data["resources"]["results"]["products"], None
    except Exception:
        return [], None


def feed_products(client, domain, max_pages=FEED_PAGES):
    """Pull the newest pages of the public /products.json catalog. Returns
    (products, error). products is a list (possibly empty) if any page
    responded; None if the first request failed, with error giving the
    reason. The feed is newest-first, so this window is ideal for fresh
    drops; restocks of older listings are covered by the search pass."""
    out = []
    responded = False
    first_err = None
    for page in range(1, max_pages + 1):
        data, err = client.get_json(
            f"https://{domain}/products.json?limit=250&page={page}")
        if data is None:
            if not responded:
                first_err = err
            break
        responded = True
        prods = data.get("products", [])
        if not prods:
            break
        out.extend(prods)
        if len(prods) < 250:
            break
    if not responded:
        return None, first_err
    return out, None


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


def tags_of(product):
    """Shopify tags as a lowercase set. products.json gives a list; some
    endpoints give a comma-separated string."""
    t = product.get("tags") or []
    if isinstance(t, str):
        t = t.split(",")
    return {x.strip().lower() for x in t if x and x.strip()}


def confirm_listing(client, purl, gate_tags):
    """Re-read one product's .js before it can alert. The search endpoint
    carries no tags and can lag, and some shops (Liquor Barn) keep allocated
    bottles 'available' at MSRP while a tag like 'unavailable' disables the
    cart. Returns (available, price) from the live product, with available
    forced False if a gate tag is present; None if the check itself failed
    (caller keeps what it had rather than dropping a real hit)."""
    data, _ = client.get_json(purl + ".js")
    if not isinstance(data, dict) or "variants" not in data:
        return None
    if tags_of(data) & gate_tags:
        return False, None
    variants = [dict(v, price=(v.get("price") or 0) / 100.0)
                for v in data.get("variants", [])]
    return variant_pricing(variants)


def is_recent(product, hours):
    """True if the product was created or published within `hours`. Used by
    the early warning so a listing that merely re-surfaces in search (the
    search endpoint returns only the top 10) isn't mistaken for a new one."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for key in ("published_at", "created_at"):
        raw = product.get(key)
        if not raw:
            continue
        try:
            ts = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if (now - ts).total_seconds() <= hours * 3600:
            return True
    return False


def variant_pricing(variants):
    """(available, price) for a feed product. available = any variant is.
    price = the cheapest AVAILABLE variant when in stock, so an alert quotes
    a price you can actually pay - min() across all variants could quote a
    sold-out cheaper variant while a pricier one is what's buyable. For
    all-out products it's the cheapest listed price (used for watch targets)."""
    available = any(v.get("available") for v in variants)
    avail_prices = [p for p in
                    (to_price(v.get("price")) for v in variants
                     if v.get("available")) if p]
    if available and avail_prices:
        return True, min(avail_prices)
    all_prices = [p for p in (to_price(v.get("price")) for v in variants) if p]
    return available, (min(all_prices) if all_prices else None)


def scan_shop(shop, bottles, do_suggest=True, gate_tags=frozenset(),
              default_floor=0, seen=frozenset(), baselined=frozenset(),
              fresh_hours=0):
    """Scan one shop. Runs in a worker thread and touches no shared state;
    returns (shop, any_ok, last_err, feed_ok, candidates, searched) where
    candidates is a list of (bottle, purl, title, available, price, fresh)
    and searched names the bottles whose search pass completed. fresh marks a
    never-seen listing created/published within fresh_hours (early warning;
    0 disables). Candidates are deduped by
    (bottle, purl) with the feed version preferred (richer variant data).
    The feed pass always runs; the per-bottle search pass runs when
    do_suggest is True (this run's rotation slice) OR when the feed failed
    or was empty, since search is that shop's only coverage then."""
    domain = shop["domain"]
    client = ShopClient()
    any_ok = False
    last_err = None
    feed_ok = False
    dedupe = set()
    cands = []
    searched = []

    # Pass 1: the feed - newly created listings.
    feed, err = feed_products(client, domain)
    if feed is None:
        last_err = err
    else:
        any_ok = True
        feed_ok = bool(feed)
        for p in feed:
            title = p.get("title", "")
            available, price = variant_pricing(p.get("variants", []) or [])
            if available and tags_of(p) & gate_tags:
                available = False   # tag-gated: listed, but cart disabled
            purl = f"https://{domain}/products/{p.get('handle', '')}"
            fresh = bool(fresh_hours) and purl not in seen \
                and is_recent(p, fresh_hours)
            for b in bottles:
                if title_matches(title, b):
                    dedupe.add((b.get("name"), purl))
                    cands.append((b, purl, title, available, price, fresh))

    # Pass 2: native search, once per bottle - restocks of older listings
    # that sit beyond the feed window. Shops that disable products.json are
    # covered entirely by this pass. Two consecutive hard failures (429 or
    # timeout) abort the pass for this shop this run: a throttled IP just
    # burns the global budget, and a hanging shop would otherwise stall its
    # worker chain for minutes (timeouts are the slow failure - 15s x
    # retries each).
    if not do_suggest and not feed:
        do_suggest = True   # feed gave nothing; search is the only coverage
    consecutive_fail = 0
    for b in (bottles if do_suggest else []):
        prods, err = suggest_products(client, domain,
                                      b.get("query", b.get("name", "")))
        if prods is None:
            last_err = err or last_err
            if err in ("HTTP 429", "HTTP 430", "timeout"):
                consecutive_fail += 1
                if consecutive_fail >= 2:
                    break
            else:
                consecutive_fail = 0
            continue
        consecutive_fail = 0
        any_ok = True
        searched.append(b.get("name"))
        for p in prods:
            title = p.get("title", "")
            if not title_matches(title, b):
                continue
            rel = (p.get("url") or "").split("?")[0]
            purl = f"https://{domain}{rel}"
            if (b.get("name"), purl) in dedupe:
                continue
            dedupe.add((b.get("name"), purl))
            available = bool(p.get("available"))
            price = to_price(p.get("price"))
            cap = b.get("max_price")
            floor = b.get("min_price", default_floor)
            if (available and price is not None and price >= floor
                    and (cap is None or price <= cap)):
                # Would alert: confirm against the live product first
                # (tags, real variant availability and price). Rare, so the
                # extra request is cheap.
                checked = confirm_listing(client, purl, gate_tags)
                if checked is not None:
                    available, price = checked
            fresh = False
            if (fresh_hours and purl not in seen
                    and b.get("name") in baselined):
                # Unseen via search at a shop/bottle already baselined:
                # check the product's age before calling it new.
                data, _ = client.get_json(purl + ".js")
                if isinstance(data, dict):
                    fresh = is_recent(data, fresh_hours)
            cands.append((b, purl, title, available, price, fresh))
    return shop, any_ok, last_err, feed_ok, cands, searched


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
    _tg_failures[0] += 1
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


def snapshot_state(matches, bottles):
    """Per-bottle {available, price} for the cheapest in-stock listing, used to
    detect run-to-run changes (new stock, price moves). price is None when
    nothing is in stock. Bottles with no listings at all this run are omitted so
    the caller can carry the last known state forward - a shop timing out should
    not read as a bottle going out and then 'coming back' next run."""
    st = {}
    for b in bottles:
        name = b.get("name")
        ms = [m for m in matches if m["bottle"] == name]
        if not ms:
            continue
        instock = [m for m in ms if m["available"] and m["price"]]
        if instock:
            cheap = min(instock, key=lambda m: m["price"])
            st[name] = {"available": True, "price": cheap["price"]}
        else:
            st[name] = {"available": False, "price": None}
    return st


def build_snapshot(matches, bottles, monitored, total, today, unreachable=None,
                   prev=None, persistent=None):
    """Phone-friendly snapshot. Two-line card per bottle, sorted so the most
    actionable bottles float to the top: in stock and under cap first, then in
    stock over cap, then all out, then no listings; newly in-stock bottles rise
    within their group. The header states how many shops were reached and, when
    some were not, names them with the reason so a thin run (e.g. 19/29) is
    explained rather than mysterious. When a prior baseline (prev) is supplied,
    bottles that flipped out->in are flagged 🆕 and price moves since the last
    scan show a green-down/red-up arrow with the delta."""

    def tier(b):
        """0 = in stock & under cap, 1 = in stock over cap, 2 = out, 3 = none."""
        name = b.get("name")
        cap = b.get("max_price")
        ms = [m for m in matches if m["bottle"] == name]
        if not ms:
            return 3
        instock = [m for m in ms if m["available"] and m["price"]]
        if not instock:
            return 2
        under = [m for m in instock
                 if (cap is None or m["price"] <= cap)
                 and m["price"] >= m.get("floor", 0)]
        return 0 if under else 1

    header = [f"\U0001F4F8 Snapshot · {today}",
              f"{monitored}/{total} shops visible"]
    if unreachable:
        from collections import Counter
        reasons = Counter(r for _, r in unreachable)
        reason_str = ", ".join(f"{n}× {why}" for why, n in reasons.most_common())
        names = ", ".join(sorted(n for n, _ in unreachable))
        header.append(f"⚠️ {len(unreachable)} not reached ({reason_str})")
        header.append(f"   {names}")
        transient = [(n, r) for n, r in unreachable
                     if n not in set(persistent or [])]
        if any(r in ("timeout", "connection error") or r.startswith("HTTP 5")
               or r in ("HTTP 429", "HTTP 430") for _, r in transient):
            header.append("   likely transient - usually clears next run")
    if persistent:
        header.append(f"\U0001F6A8 dark 1+ days (not transient - check): "
                      f"{', '.join(persistent)}")

    prev = prev or {}
    new_names = set()
    for b in bottles:
        name = b.get("name")
        ms = [m for m in matches if m["bottle"] == name]
        instock = [m for m in ms if m["available"] and m["price"]]
        p = prev.get(name)
        if instock and p and p.get("available") is False:
            new_names.add(name)

    lines = list(header)
    for b in sorted(bottles, key=lambda b: (tier(b),
                                            0 if b.get("name") in new_names else 1,
                                            b.get("name", ""))):
        name = b.get("name")
        cap = b.get("max_price")
        cap_str = f"${cap}" if cap else "no cap"
        ms = [m for m in matches if m["bottle"] == name]
        instock = [m for m in ms if m["available"] and m["price"]]
        under = [m for m in instock
                 if (cap is None or m["price"] <= cap)
                 and m["price"] >= m.get("floor", 0)]

        lines.append("")
        if not ms:
            lines.append(f"▫️ {name} · cap {cap_str}")
            lines.append("   no listings found")
        elif instock:
            cheap = min(instock, key=lambda m: m["price"])
            shop = cheap["shop"].replace("www.", "")
            icon = "✅" if under else "\U0001F53A"
            cap_word = "under cap" if under else "over cap"
            is_new = name in new_names
            change = ""
            p = prev.get(name)
            if not is_new and p and p.get("available") and p.get("price"):
                d = cheap["price"] - p["price"]
                if d < 0:
                    change = f"  \U0001F7E2 ↓ ${abs(d):,.0f}"
                elif d > 0:
                    change = f"  \U0001F534 ↑ ${d:,.0f}"
            new_badge = "  \U0001F195" if is_new else ""
            lines.append(f"{icon} {name} · cap {cap_str}{new_badge}")
            lines.append(f"   ${cheap['price']:,.0f} @ {shop} · {cap_word} "
                         f"· {len(instock)}/{len(ms)} in stock{change}")
            if not under:
                out_under = [m for m in ms if not m["available"] and m["price"]
                             and (cap is None or m["price"] <= cap)
                             and m["price"] >= m.get("floor", 0)]
                if out_under:
                    w = min(out_under, key=lambda m: m["price"])
                    lines.append(f"   \U0001F440 watch {w['shop'].replace('www.','')} "
                                 f"${w['price']:,.0f} (out, under cap)")
        else:
            cheap = min(ms, key=lambda m: m["price"] or 9e9)
            price_str = f"${cheap['price']:,.0f}" if cheap["price"] else "n/a"
            shop = cheap["shop"].replace("www.", "")
            lines.append(f"⚪ {name} · cap {cap_str}")
            lines.append(f"   all out · low {price_str} @ {shop} "
                         f"· 0/{len(ms)} in stock")
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

    t0 = time.monotonic()
    started = datetime.datetime.now(datetime.timezone.utc)
    config = load_json(CONFIG, None)
    state = load_json(STATE, {})

    # ---- Config guard: a JSON typo used to fail SILENTLY (load_json falls
    # back to empty, the watcher scans nothing, and the weekly heartbeat is
    # the first tell - up to 7 days blind). Instead: Telegram once a day and
    # keep the run green so state (with the dedupe marker) still persists.
    if (not config or not config.get("bottles")
            or not [s for s in config.get("shops", []) if s.get("domain")]):
        today_s = datetime.date.today().isoformat()
        if state.get("config_alert") != today_s:
            send_telegram(token, chat_id,
                          "\U0001F6D1 Bourbon watcher: config.json is missing, "
                          "invalid JSON, or has no bottles/shops. The watcher "
                          "is scanning NOTHING until it's fixed. (This pings "
                          "once a day.)")
            state["config_alert"] = today_s
            with open(STATE, "w") as f:
                json.dump(state, f, indent=2)
        print("Config invalid or empty - scanned nothing.", file=sys.stderr)
        return

    in_stock = state.get("in_stock", {})
    bottles = config.get("bottles", [])
    shops = [s for s in config.get("shops", []) if s.get("domain")]
    default_floor = config.get("min_price", 0)  # global junk-price floor
    # Global excludes (bundles, combos, empties...) ride on every bottle so
    # a 5-pack or a combo can never alert as the bottle itself.
    global_ex = config.get("global_exclude", [])
    for b in bottles:
        b["exclude"] = list(b.get("exclude", [])) + list(global_ex)
    gate_tags = frozenset(t.lower() for t in
                          config.get("gate_tags", ["unavailable"]))
    # Early warning: ping once when a matching listing first appears but
    # isn't buyable yet (placeholder price, sold out, coming soon, gated) -
    # the shop is about to drop. seen_listings remembers every matched URL;
    # baselined records which (shop, bottle) pairs have had a full search
    # pass, so an old listing surfacing in search isn't called new.
    early = config.get("early_warning", True)
    fresh_hours = config.get("early_warning_hours", 72) if early else 0
    seen_listings = state.get("seen_listings", {})
    baselined = state.get("baselined", {})

    # Prune state for shops no longer on the roster - their URLs can never
    # match again and just accumulate (old cut shops were still in state).
    roster_hosts = {s["domain"].replace("www.", "") for s in shops}
    in_stock = {u: v for u, v in in_stock.items()
                if urllib.parse.urlparse(u).netloc.replace("www.", "")
                in roster_hosts}

    monitored_domains = set()
    feed_used = []
    unreachable = []   # (shop_name, reason) for shops no endpoint would answer
    alerts = []
    matches = []   # full current landscape, for on-demand /snapshot replies

    # Rotate the per-bottle search (restock) pass across the roster so each
    # run stays within the global request budget. Every shop still gets the
    # feed pass every run; each gets the search pass every ~3 runs.
    k = min(config.get("suggest_shops_per_run", SUGGEST_DEFAULT),
            max(len(shops), 1))
    cursor = int(state.get("suggest_cursor", 0)) % max(len(shops), 1)
    suggest_domains = {shops[(cursor + i) % len(shops)]["domain"]
                       for i in range(k)} if shops else set()
    state["suggest_cursor"] = (cursor + k) % max(len(shops), 1)

    # Scan shops in parallel; merge results sequentially so in_stock/alerts
    # stay single-threaded.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(
            lambda s: scan_shop(s, bottles, s["domain"] in suggest_domains,
                                gate_tags, default_floor,
                                frozenset(seen_listings),
                                frozenset(baselined.get(s["domain"], [])),
                                fresh_hours),
            shops))

    today_s = datetime.date.today().isoformat()
    early_pings = []   # (purl, message) for fresh, not-yet-buyable listings
    for shop, any_ok, last_err, feed_ok, cands, searched in results:
        domain = shop["domain"]
        shop_name = shop.get("name", domain)
        shop_note = shop.get("note")
        if feed_ok:
            feed_used.append(domain)
        names = {b.get("name") for b in bottles}
        baselined[domain] = sorted((set(baselined.get(domain, []))
                                    | set(searched)) & names)
        for b, purl, title, available, price, fresh in cands:
            floor = b.get("min_price", default_floor)
            consider(b, shop_name, shop_note, purl, title, available, price,
                     in_stock, alerts, floor)
            cap = b.get("max_price")
            buyable_listed = bool(available) and price is not None \
                and price >= floor
            if (fresh and not buyable_listed
                    and not any(u == purl for u, _ in early_pings)):
                if price is None or price < floor:
                    status = (f"placeholder price ${price:,.2f}" if price
                              else "no price yet")
                else:
                    status = f"not buyable yet, listed at ${price:,.2f}"
                cap_str = f" (cap ${cap})" if cap else ""
                lines = [f"\U0001F440 NEW LISTING: {b.get('name')}{cap_str}",
                         f"{shop_name} - {status}", f"\"{title}\"", purl,
                         "Heads-up only. The in-stock alert fires if it goes "
                         "live under cap."]
                if shop_note:
                    lines.append(f"⚠ {shop_note}")
                early_pings.append((purl, "\n".join(lines)))
            else:
                seen_listings.setdefault(purl, today_s)
            matches.append({"bottle": b.get("name"), "shop": shop_name,
                            "available": available, "price": price,
                            "floor": floor})
        if any_ok:
            monitored_domains.add(domain)
        else:
            unreachable.append((shop_name, last_err or "no response"))

    # ---- Persistent-darkness tracking: one missed run is noise; a shop dark
    # for a day+ is a coverage hole (moved off Shopify, blocking us, dead
    # domain) that "likely transient" wording would keep excusing forever.
    # Count consecutive missed runs per domain; flag crossers.
    dark_names = {n for n, _ in unreachable}
    old_misses = state.get("shop_misses", {})
    misses = {}
    for shop in shops:
        d = shop["domain"]
        dark = shop.get("name", d) in dark_names
        misses[d] = old_misses.get(d, 0) + 1 if dark else 0
    state["shop_misses"] = misses
    DARK_RUNS = 250   # ~a day at 5-minute cadence
    persistent = [s.get("name", s["domain"]) for s in shops
                  if misses[s["domain"]] >= DARK_RUNS]

    alerts_sent = 0
    for purl, msg in alerts:
        if send_telegram(token, chat_id, msg):
            alerts_sent += 1
            in_stock[purl] = True   # record as alerted only on confirmed delivery
            print("ALERT:", msg.replace("\n", " | "))
        else:
            print("ALERT NOT DELIVERED (will retry next run):", purl,
                  file=sys.stderr)

    # Early-warning pings: capped per run so a shop importing its catalog
    # can't flood the phone; the overflow goes in one summary message.
    EARLY_MAX = 5
    early_sent = 0
    for purl, msg in early_pings[:EARLY_MAX]:
        if send_telegram(token, chat_id, msg):
            early_sent += 1
            seen_listings[purl] = today_s   # only once delivered
    rest = early_pings[EARLY_MAX:]
    if rest:
        summary = (f"\U0001F440 +{len(rest)} more new listings:\n" +
                   "\n".join(m.split("\n")[0][2:].replace("NEW LISTING: ", "")
                             + " - " + m.split("\n")[3] for _, m in rest[:20]))
        if send_telegram(token, chat_id, summary):
            for purl, _ in rest:
                seen_listings[purl] = today_s

    total = len(shops)
    monitored = len(monitored_domains)
    print(f"Monitored {monitored}/{total} shops "
          f"({len(feed_used)} feeds visible). {len(alerts)} new alert(s).")

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
        blind_note = ""
        if unreachable:
            names = ", ".join(n for n, _ in unreachable)
            blind_note = f" {blind} not reached: {names}."
        elif blind:
            blind_note = f" {blind} not visible - check config."
        if persistent:
            blind_note += (f" ⚠ DARK 1+ DAYS (needs a look, not "
                           f"transient): {', '.join(persistent)}.")
        if send_telegram(token, chat_id,
            f"\U0001F7E2 Bourbon watcher alive - {today.isoformat()}. "
            f"{monitored}/{total} shops visible.{blind_note} "
            f"Hits arrive separately; if this stops, something broke."):
            print(f"Heartbeat sent ({monitored}/{total} visible).")
            state["last_heartbeat"] = today.isoformat()  # only if delivered

    # ---- Change tracking: baseline of each bottle's stock/price from last run.
    # We compare against it to flag new stock and price moves, then advance it.
    # Bottles absent this run keep their prior value (carry-forward) so a shop
    # outage does not masquerade as a bottle going out and coming back.
    # Bottles no longer in the config are dropped.
    prev_state = state.get("bottle_state", {})
    cur_state = snapshot_state(matches, bottles)
    bottle_names = {b.get("name") for b in bottles}

    # ---- On-demand snapshot: if you texted /snapshot or /status since the
    # last run, reply with the current per-bottle landscape from this scan.
    if check_snapshot_request(token, chat_id, state):
        summary = build_snapshot(matches, bottles, monitored, total,
                                 today.isoformat(), unreachable, prev_state,
                                 persistent)
        if send_telegram(token, chat_id, summary):
            print("Snapshot sent on request.")
        else:
            print("Snapshot send failed.", file=sys.stderr)

    # Only 'already alerted' (True) entries carry information: absent and
    # False behave identically in consider(), so dropping False keeps
    # state.json small (it had grown to ~600 entries, 98% False).
    state["in_stock"] = {u: True for u, v in in_stock.items() if v}
    state["bottle_state"] = {k: v for k, v in {**prev_state, **cur_state}.items()
                             if k in bottle_names}
    state["date"] = today.isoformat()
    seen_listings = {u: d for u, d in seen_listings.items()
                     if urllib.parse.urlparse(u).netloc.replace("www.", "")
                     in roster_hosts}
    if len(seen_listings) > 5000:   # keep the newest
        seen_listings = dict(sorted(seen_listings.items(),
                                    key=lambda kv: kv[1])[-5000:])
    state["seen_listings"] = seen_listings
    state["baselined"] = {d: v for d, v in baselined.items()
                          if d in {s["domain"] for s in shops}}

    # ---- Run summary: a public, unauthenticated health record. The repo is
    # public, so raw state.json can be read without a GitHub login - no log
    # downloads needed to check how the watcher is doing.
    by_bottle = {}
    for b in bottles:
        name = b.get("name")
        cap = b.get("max_price")
        ms = [m for m in matches if m["bottle"] == name]
        ins = [m for m in ms if m["available"] and m["price"]]
        under = [m for m in ins if (cap is None or m["price"] <= cap)
                 and m["price"] >= m.get("floor", 0)]
        low = min((m["price"] for m in ins), default=None)
        by_bottle[name] = {"listings": len(ms), "in_stock": len(ins),
                           "under_cap": len(under), "low_in_stock": low}
    dur = round(time.monotonic() - t0, 1)
    state["last_run"] = {
        "utc": started.isoformat(timespec="seconds"), "version": VERSION,
        "duration_s": dur, "shops_total": total, "shops_reached": monitored,
        "feeds_visible": len(feed_used),
        "unreachable": [[n, r] for n, r in unreachable],
        "dark_1_day_plus": persistent,
        "alerts_found": len(alerts), "alerts_sent": alerts_sent,
        "early_warnings": len(early_pings), "early_warnings_sent": early_sent,
        "telegram_failures": _tg_failures[0], "bottles": by_bottle,
    }
    hist = state.get("run_history", [])
    hist.append([started.strftime("%m-%d %H:%M"), monitored, total,
                 len(alerts), dur, _tg_failures[0]])
    state["run_history"] = hist[-RUN_HISTORY:]
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
