"""
watcher.py
Universal change detection with cached, self-healing per-target strategies.

Tier order on first visit to a target (cheapest/most-specific first):
  1. Shopify .json                 -> line_items table (fine-grained, per-variant)
  2. JSON-LD Product schema        -> raw_state_json (single blob)
  3. Open Graph / microdata tags   -> raw_state_json
  4. Playwright network sniff      -> raw_state_json (also caches api_endpoint_url
                                       so future polls skip the browser entirely)
  5. Full-page SHA-256 hash        -> raw_state_json (last resort, no structured price)

On every poll after the first, `resolve_target_state` skips straight to
whichever strategy is cached on watch_targets.resolved_strategy — the tier
cascade above only runs again if the cached strategy comes back empty
(site changed shape), which is the self-healing path.
"""
import asyncio
import hashlib
import json
import re
import warnings
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

import config
from db import DB

db = DB()

PRODUCT_PATH_HINTS = [
    "/product", "/products", "/shop", "/item", "/collections",
    "/store", "/pricing", "/shopify", "/cart", "/checkout",
]
PRICE_KEY_PATTERN = re.compile(r"price|cost|amount|value", re.IGNORECASE)
CURRENCY_PATTERN = re.compile(
    r"[$₹€£]\s?\d[\d,]*(?:\.\d+)?\s*(?:/\s*(?:month|year|mo|yr))?", re.IGNORECASE
)
PRICING_PATH_CANDIDATES = ["/pricing", "/plans", "/price", "/subscribe"]


# ---------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------
class BrowserManager:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.context = None

    async def start(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def get_page(self):
        if self.context is None:
            raise RuntimeError("BrowserManager.start() must be called before get_page()")
        return await self.context.new_page()


browsermgr = BrowserManager()


# ---------------------------------------------------------------------
# Shared utility — used as a fallback *within* multiple tiers below,
# not a tier on its own.
# ---------------------------------------------------------------------
def find_price_field(obj, path: str = "") -> dict | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_path = f"{path}.{key}" if path else key
            if PRICE_KEY_PATTERN.search(key) and isinstance(value, (int, float, str)):
                return {"path": new_path, "value": value}
            if isinstance(value, (dict, list)):
                result = find_price_field(value, new_path)
                if result:
                    return result
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:5]):
            result = find_price_field(item, f"{path}[{i}]")
            if result:
                return result
    return None


# ---------------------------------------------------------------------
# Tier: whole-page hash (last resort)
# ---------------------------------------------------------------------
def fetch_hash_fingerprint(url: str) -> str | None:
    try:
        res = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return None
    return hashlib.sha256(res.content).hexdigest()[:16]


# ---------------------------------------------------------------------
# Tier: HTTP validators — cheap gate, only useful once you already know
# the target has no structured price (i.e. its cached strategy is 'hash').
# ---------------------------------------------------------------------
def fetch_http_validator_fingerprint(url: str) -> tuple[str | None, str | None] | None:
    try:
        res = requests.head(url, timeout=config.REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return None
    etag = res.headers.get("ETag")
    last_modified = res.headers.get("Last-Modified")
    if not etag and not last_modified:
        return None
    return (etag, last_modified)


# ---------------------------------------------------------------------
# Tier: Shopify .json -> list of line items (product_id, variant_id, price, available)
# ---------------------------------------------------------------------
def is_shopify_product_or_collection(url: str) -> bool:
    return "/products/" in url or "/collections/" in url


def fetch_shopify_line_items(url: str) -> list[dict] | None:
    json_url = url.rstrip("/") + ".json"
    try:
        resp = requests.get(json_url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    items = []
    if "product" in data:
        p = data["product"]
        for v in p.get("variants", []):
            items.append({
                "product_id": p.get("id"),
                "variant_id": v.get("id"),
                "product_title": p.get("title"),
                "price": v.get("price"),
                "available": v.get("available"),
            })
    elif "products" in data:
        for p in data["products"]:
            for v in p.get("variants", []):
                items.append({
                    "product_id": p.get("id"),
                    "variant_id": v.get("id"),
                    "product_title": p.get("title"),
                    "price": v.get("price"),
                    "available": v.get("available"),
                })
    else:
        return None
    return items if items else None


# ---------------------------------------------------------------------
# Tier: JSON-LD Product schema
# ---------------------------------------------------------------------
def _parse_html(content: bytes) -> BeautifulSoup:
    """BeautifulSoup wrapped to suppress UnicodeDammit's encoding-guess
    warnings — harmless noise on pages with inconsistent charset headers
    (common on small regional sites with non-Latin text), not worth
    surfacing to the console on every check."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return BeautifulSoup(content, "html.parser")


def fetch_jsonld_product_state(url: str) -> dict | None:
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = _parse_html(resp.content)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else data.get("@graph", [data])
        for entry in candidates:
            if entry.get("@type") == "Product":
                offers = entry.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price")
                if price is None:
                    match = find_price_field(entry)
                    price = match["value"] if match else None
                return {
                    "name": entry.get("name"),
                    "price": price,
                    "availability": offers.get("availability"),
                }
    return None


# ---------------------------------------------------------------------
# Tier: Open Graph / microdata meta tags
# ---------------------------------------------------------------------
def fetch_meta_tag_price_state(url: str) -> dict | None:
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = _parse_html(resp.content)
    price = None
    availability = None

    og_price = soup.find("meta", property="product:price:amount")
    if og_price:
        price = og_price.get("content")

    microdata_price = soup.find(attrs={"itemprop": "price"})
    if microdata_price and not price:
        price = microdata_price.get("content") or microdata_price.get_text(strip=True)

    availability_tag = soup.find(attrs={"itemprop": "availability"})
    if availability_tag:
        availability = availability_tag.get("content") or availability_tag.get_text(strip=True)

    if price is None:
        return None
    return {"price": price, "availability": availability}


# ---------------------------------------------------------------------
# Tier: subscription pricing table. For SaaS pages where price isn't in
# JSON-LD or meta tags — usually because it's a multi-tier pricing table
# (not a single schema.org Product) and/or the price text only exists
# after client-side JS renders it. Renders with Playwright, scans the
# resulting DOM for currency-shaped text, walks up to the nearest heading
# as a best-effort tier name. Broad net, not a precise parser — this is
# meant to catch "does this page have real prices on it at all".
# ---------------------------------------------------------------------
def is_probably_pricing_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(hint in path for hint in PRICING_PATH_CANDIDATES)


async def fetch_pricing_table_state(url: str) -> dict | None:
    page = None
    try:
        page = await browsermgr.get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # SPAs with persistent background requests never go idle
        html = await page.content()
    except Exception as e:
        print(f"[ERROR] fetch_pricing_table_state failed for {url}: {e}")
        return None
    finally:
        if page:
            await page.close()

    soup = _parse_html(html.encode("utf-8"))
    tiers = []
    for text_node in soup.find_all(string=CURRENCY_PATTERN):
        match = CURRENCY_PATTERN.search(text_node)
        if not match:
            continue
        price_text = match.group(0).strip()

        # walk up a few ancestors looking for a heading as the tier name
        # (e.g. "Pro" above a "$29/mo" price in a pricing card)
        tier_name = None
        node = text_node.parent
        for _ in range(4):
            if node is None:
                break
            heading = node.find(["h1", "h2", "h3", "h4"])
            if heading and heading.get_text(strip=True):
                tier_name = heading.get_text(strip=True)
                break
            node = node.parent

        tiers.append({"tier": tier_name or "unknown", "price": price_text})

    if not tiers:
        return None

    seen = set()
    unique_tiers = []
    for t in tiers:
        key = (t["tier"], t["price"])
        if key not in seen:
            seen.add(key)
            unique_tiers.append(t)

    return {"tiers": unique_tiers}


async def probe_pricing_paths(domain: str) -> list[str]:
    """Try common pricing-page paths directly (/pricing, /plans, ...)
    rather than relying on sitemap/link discovery to surface them —
    SaaS marketing sites often have exactly one pricing page that isn't
    linked in a way your sitemap crawl or product-hint filter would catch."""
    found = []
    for path in PRICING_PATH_CANDIDATES:
        candidate = urljoin(domain, path)
        try:
            resp = requests.head(candidate, timeout=config.REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            if resp.status_code < 400:
                found.append(candidate)
        except requests.RequestException:
            continue
    return found


# ---------------------------------------------------------------------
# Tier: Playwright network sniff. Renders the page, intercepts any JSON
# XHR/fetch response, walks it for a price-shaped field. This is the
# expensive tier — only reached when everything above finds nothing.
# The api_url it returns gets cached so future polls skip the browser.
# ---------------------------------------------------------------------
async def fetch_json_product_data(url: str) -> dict | None:
    page = None
    captured = []

    def on_response(response):
        ctype = response.headers.get("content-type") or ""
        if "application/json" in ctype:
            captured.append(response)

    try:
        page = await browsermgr.get_page()
        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # don't block forever on sites with persistent background XHRs

        for response in captured:
            try:
                body = await response.json()
            except Exception:
                continue
            match = find_price_field(body)
            if match:
                return {"price": match["value"], "api_url": response.url}
        return None
    except Exception as e:
        print(f"[ERROR] fetch_json_product_data failed for {url}: {e}")
        return None
    finally:
        if page:
            await page.close()


# ---------------------------------------------------------------------
# Sitemap / on-page product URL discovery (for selector.py's onboarding
# flow, not called during regular polling)
# ---------------------------------------------------------------------
def _fetch_sitemap_locs(url: str) -> list[str]:
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        return [loc.text.strip() for loc in root.iter() if loc.tag.endswith("loc") and loc.text]
    except (requests.RequestException, ET.ParseError):
        return []


NON_PRODUCT_PATH_HINTS = [
    "/about", "/contact", "/blog", "/privacy", "/terms", "/faq",
    "/account", "/login", "/cart", "/checkout", "/wp-", "/category",
    "/tag/", "/page/", "/policy", "/shipping", "/returns", "/careers",
]


def discover_product_urls_from_sitemap(domain: str, limit: int = 30) -> list[str]:
    """
    Walks /sitemap.xml or /sitemap_index.xml, recurses into any nested
    sitemap files. Returns candidate page URLs, excluding known
    non-product paths — deliberately permissive rather than requiring a
    known 'product' pattern, since small/custom sites use whatever URL
    taxonomy they want (e.g. '/pickles/foo' instead of '/products/foo').
    validate_product_candidates() does the real filtering downstream by
    checking for actual extractable price data.
    """
    candidates = []
    visited_sitemaps = set()

    def is_sitemap_file(u: str) -> bool:
        u_lower = u.lower()
        return u_lower.endswith(".xml") and "sitemap" in u_lower

    def is_non_product(u: str) -> bool:
        u_lower = u.lower()
        return any(hint in u_lower for hint in NON_PRODUCT_PATH_HINTS)

    def crawl(sitemap_url: str, depth: int = 0):
        if sitemap_url in visited_sitemaps or depth > 2:
            return
        visited_sitemaps.add(sitemap_url)
        for loc in _fetch_sitemap_locs(sitemap_url):
            if is_sitemap_file(loc):
                crawl(loc, depth + 1)
            elif not is_non_product(loc) and loc.rstrip("/") != domain.rstrip("/"):
                candidates.append(loc)

    for sitemap_path in ["/sitemap.xml", "/sitemap_index.xml"]:
        if candidates:
            break
        crawl(urljoin(domain, sitemap_path))

    return candidates[:limit]


async def fetch_product_urls_from_page(url: str, limit: int = 10) -> list[str]:
    page = None
    try:
        page = await browsermgr.get_page()
        await page.goto(url)
        await page.wait_for_load_state("networkidle")
        links = await page.query_selector_all("a")
        product_links = []
        for link in links:
            href = await link.get_attribute("href")
            if href and any(hint in href.lower() for hint in PRODUCT_PATH_HINTS):
                product_links.append(urljoin(url, href))
        return product_links[:limit]
    except Exception as e:
        print(f"[ERROR] Failed to fetch product URLs from {url}: {e}")
        return []
    finally:
        if page:
            await page.close()


# ---------------------------------------------------------------------
# Router — ALWAYS returns a 4-tuple: (kind, data, strategy_used, api_url)
#   kind: "shopify" | "state" | None
#   data: list[line_item] for shopify, dict for state, None if nothing found
# ---------------------------------------------------------------------
async def resolve_target_state(
    url: str, cached_strategy: str | None = None, cached_api_url: str | None = None
):
    # --- fast path: skip discovery, jump straight to what worked last time ---
    if cached_strategy == "shopify":
        items = fetch_shopify_line_items(url)
        if items:
            return "shopify", items, "shopify", None
        # fell through — site changed shape, drop into full discovery below

    elif cached_strategy == "jsonld":
        state = fetch_jsonld_product_state(url)
        if state:
            return "state", state, "jsonld", None

    elif cached_strategy == "meta_tag":
        state = fetch_meta_tag_price_state(url)
        if state:
            return "state", state, "meta_tag", None

    elif cached_strategy == "sniffed_api" and cached_api_url:
        try:
            resp = requests.get(cached_api_url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            match = find_price_field(resp.json())
            if match:
                return "state", {"price": match["value"]}, "sniffed_api", cached_api_url
        except (requests.RequestException, ValueError):
            pass  # cache went stale — fall through to full discovery below

    elif cached_strategy == "pricing_table":
        state = await fetch_pricing_table_state(url)
        if state:
            return "state", state, "pricing_table", None

    elif cached_strategy == "hash":
        digest = fetch_hash_fingerprint(url)
        if digest:
            return "state", {"page_hash": digest}, "hash", None

    # --- slow path: first visit, or cache miss because the site changed ---
    if is_shopify_product_or_collection(url):
        items = fetch_shopify_line_items(url)
        if items:
            return "shopify", items, "shopify", None

    state = fetch_jsonld_product_state(url)
    if state:
        return "state", state, "jsonld", None

    state = fetch_meta_tag_price_state(url)
    if state:
        return "state", state, "meta_tag", None

    sniffed = await fetch_json_product_data(url)
    if sniffed:
        return "state", {"price": sniffed["price"]}, "sniffed_api", sniffed["api_url"]

    pricing_state = await fetch_pricing_table_state(url)
    if pricing_state:
        return "state", pricing_state, "pricing_table", None

    digest = fetch_hash_fingerprint(url)
    if digest:
        return "state", {"page_hash": digest}, "hash", None

    return None, None, None, None


# ---------------------------------------------------------------------
# check_target — loads baseline from DB, resolves state (cache-aware),
# diffs, persists. This is the piece that actually makes the cache useful.
# ---------------------------------------------------------------------
async def check_target(url: str) -> None:
    baseline = db.get_baseline(url)
    cached_strategy = baseline["resolved_strategy"] if baseline else None
    cached_api_url = baseline["api_endpoint_url"] if baseline else None

    kind, data, strategy_used, api_url = await resolve_target_state(
        url, cached_strategy, cached_api_url
    )

    if kind is None:
        print(f"[watcher] could not resolve any state for {url}")
        return

    if kind == "shopify":
        current_items = {str(item["variant_id"]): item for item in data}
        baseline_items = db.get_line_items(url)

        for variant_id, item in current_items.items():
            old = baseline_items.get(variant_id)
            if old is None:
                continue  # new variant — nothing to diff against yet
            if str(old["price"]) != str(item["price"]):
                db.log_item_change(
                    url, item["product_id"], variant_id, item["product_title"],
                    "price", old["price"], item["price"],
                )
                print(f"[watcher] PRICE CHANGE {url} variant {variant_id}: {old['price']} -> {item['price']}")
            if bool(old["available"]) != bool(item["available"]):
                db.log_item_change(
                    url, item["product_id"], variant_id, item["product_title"],
                    "availability", bool(old["available"]), bool(item["available"]),
                )
                print(f"[watcher] AVAILABILITY CHANGE {url} variant {variant_id}")

        db.upsert_line_items(url, data)
        db.update_baseline(
            url, "shopify_json", "", None,
            resolved_strategy=strategy_used, api_endpoint_url=api_url,
        )

    else:  # kind == "state"
        old_state_json = baseline["raw_state_json"] if baseline else None
        new_state_json = json.dumps(data, sort_keys=True)

        if old_state_json and old_state_json != new_state_json:
            db.log_page_change(url, old_state_json, new_state_json)
            print(f"[watcher] STATE CHANGE {url}: {old_state_json} -> {new_state_json}")
        elif not old_state_json:
            print(f"[watcher] baseline established for {url} (strategy={strategy_used})")
        else:
            print(f"[watcher] no change: {url} (strategy={strategy_used})")

        db.update_baseline(
            url, strategy_used, new_state_json, new_state_json,
            resolved_strategy=strategy_used, api_endpoint_url=api_url,
        )


async def run_once() -> None:
    """Check every registered target once. Browser launches once for the
    whole batch, not per-target — targets whose cached strategy doesn't
    need Playwright never touch it at all."""
    targets = db.get_all_targets()
    await browsermgr.start()
    try:
        for row in targets:
            try:
                await check_target(row["url"])
            except Exception as e:
                print(f"[watcher] error checking {row['url']}: {e}")
    finally:
        await browsermgr.stop()


async def run_forever(interval: int = config.POLL_INTERVAL_SECONDS) -> None:
    print(f"[watcher] polling every {interval}s")
    while True:
        await run_once()
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(run_once())