"""
selector.py
Onboarding flow: niche -> Exa-discovered competitors -> user picks companies
-> product-page discovery per company -> user picks products -> registered
as watch_targets in db.py.

This file only ever writes to the DB via db.add_target(). It never touches
watcher.py's polling loop or app.py's alerting directly — keeps the
onboarding flow fully decoupled from the rest of the pipeline, same as the
original architecture.

Run:
    python selector.py "premium dog toys DTC brands"
or just:
    python selector.py
and it'll prompt you for a niche.
"""
import asyncio
import sys
from urllib.parse import urlparse

import discover as discovery
import watcher
from db import DB

db = DB()

MARKETPLACE_DOMAINS = [
    "indiamart.com", "exportersindia.com", "amazon.", "amazon.in",
    "flipkart.com", "alibaba.com", "etsy.com", "meesho.com",
]


# ---------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """Consistent form so near-duplicates ('x.com/' vs 'x.com') don't
    both end up as separate watch_targets rows."""
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return f"https://{netloc}{path}"


def is_marketplace_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(marker in host for marker in MARKETPLACE_DOMAINS)


# ---------------------------------------------------------------------
# Company selection
# ---------------------------------------------------------------------
def prompt_company_selection(candidates: list[dict]) -> list[dict]:
    print(f"\nFound {len(candidates)} candidates:\n")
    for i, c in enumerate(candidates, start=1):
        flag = "  ⚠️  marketplace listing" if is_marketplace_domain(c["url"]) else ""
        print(f"{i}. {c['name']} — {c['url']}{flag}")
        if c.get("location"):
            print(f"   {c['location']}")
        if c.get("description"):
            print(f"   {c['description']}")

    while True:
        raw = input("\nPick companies to monitor (comma-separated numbers, e.g. 1,3,4): ").strip()
        if not raw:
            print("Enter at least one number, or Ctrl+C to quit.")
            continue
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            picked = [candidates[i - 1] for i in indices if 1 <= i <= len(candidates)]
            if picked:
                return picked
            print("No valid numbers in that selection, try again.")
        except ValueError:
            print("Couldn't parse that — use comma-separated numbers like 1,3,4.")


# ---------------------------------------------------------------------
# Product-page discovery + validation for a single company
# ---------------------------------------------------------------------
async def find_product_candidates(domain: str, limit: int = 20) -> list[str]:
    urls = watcher.discover_product_urls_from_sitemap(domain, limit=limit)
    if not urls:
        # sitemap came up empty — fall back to rendering the homepage and
        # pulling product-shaped links (needs browsermgr already started)
        urls = await watcher.fetch_product_urls_from_page(domain, limit=limit)

    # Always also try common pricing paths directly (/pricing, /plans) —
    # SaaS sites often have exactly one pricing page that neither the
    # sitemap nor a homepage link-crawl reliably surfaces.
    pricing_urls = await watcher.probe_pricing_paths(domain)
    for u in pricing_urls:
        if u not in urls:
            urls.append(u)

    return urls


async def validate_product_candidates(urls: list[str]) -> list[dict]:
    """Confirm each candidate actually has extractable price data. Tries
    the cheap tiers first (JSON-LD, meta tags — no browser). If nothing
    validates AND at least one candidate looks like a pricing page, falls
    back to rendering it with Playwright and scanning for a pricing table
    — this is what SaaS/subscription pages need, since they rarely carry
    JSON-LD Product markup and are often client-rendered."""
    validated = []
    for url in urls:
        state = watcher.fetch_jsonld_product_state(url) or watcher.fetch_meta_tag_price_state(url)
        if state and state.get("price"):
            validated.append({"url": url, **state})

    if validated:
        return validated

    # nothing validated with the cheap tiers — try pricing-table rendering
    # on any candidate that looks like a pricing/plans page
    pricing_candidates = [u for u in urls if watcher.is_probably_pricing_page(u)]
    for url in pricing_candidates:
        state = await watcher.fetch_pricing_table_state(url)
        if state and state.get("tiers"):
            validated.append({"url": url, "name": "pricing table", **state})

    return validated


def looks_like_url(text: str) -> bool:
    """Cheap sanity check for manual paste input — not full validation,
    just enough to reject 'skip', 'clear', 'n/a' etc. from becoming a
    registered target."""
    if not text or " " in text:
        return False
    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    return bool(parsed.netloc) and "." in parsed.netloc


def prompt_manual_url(company_name: str, domain: str) -> str | None:
    raw = input(
        f"Couldn't auto-find product pages for {company_name} ({domain}).\n"
        f"Paste a specific product/pricing URL to track, or press Enter to skip: "
    ).strip()
    if not raw or raw.lower() in ("skip", "none", "n", "n/a", "clear", "cancel"):
        return None
    if not looks_like_url(raw):
        print(f"'{raw}' doesn't look like a URL — skipping {company_name}.")
        return None
    return raw


DEFAULT_MAX_PRODUCTS_PER_COMPANY = 5


def prompt_product_selection(validated: list[dict]) -> list[dict]:
    print(f"\nFound {len(validated)} product page(s) with detectable pricing:\n")
    for i, p in enumerate(validated, start=1):
        name = p.get("name") or p["url"]
        if p.get("tiers"):
            tier_summary = ", ".join(f"{t['tier']}: {t['price']}" for t in p["tiers"][:3])
            print(f"{i}. {name} — {tier_summary} — {p['url']}")
        else:
            price = p.get("price", "?")
            print(f"{i}. {name} — {price} — {p['url']}")

    if len(validated) > DEFAULT_MAX_PRODUCTS_PER_COMPANY:
        print(
            f"\nThat's {len(validated)} products — probably more than you want to "
            f"track per competitor (each one is a separate poll target)."
        )
        raw = input(
            f"Pick specific ones (comma-separated numbers), or type 'all' to "
            f"register all {len(validated)}: "
        ).strip()
        if raw.lower() == "all":
            return validated
    else:
        raw = input("\nPick products to monitor (comma-separated numbers, or Enter for all): ").strip()
        if not raw:
            return validated

    if not raw:
        print("Nothing selected — skipping this company.")
        return []
    try:
        indices = [int(x.strip()) for x in raw.split(",")]
        picked = [validated[i - 1] for i in indices if 1 <= i <= len(validated)]
        if not picked:
            print("No valid numbers in that selection — skipping this company.")
        return picked
    except ValueError:
        print("Couldn't parse that — skipping this company. Re-run and try again.")
        return []


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------
async def onboard_niche(niche: str) -> None:
    candidates = discovery.find_competitors(niche)
    if not candidates:
        print("No competitors found for that niche — try a different phrasing.")
        return

    picked_companies = prompt_company_selection(candidates)

    await watcher.browsermgr.start()
    try:
        for company in picked_companies:
            domain = normalize_url(company["url"])
            print(f"\n--- {company['name']} ({domain}) ---")

            if is_marketplace_domain(domain):
                print(f"Skipping — marketplace listing, not {company['name']}'s own site.")
                continue

            product_urls = await find_product_candidates(domain)
            validated = await validate_product_candidates(product_urls)

            if not validated:
                manual = prompt_manual_url(company["name"], domain)
                if manual:
                    target_url = normalize_url(manual)
                    db.add_target(target_url)
                    print(f"Registered: {target_url}")
                else:
                    print(f"Skipped {company['name']} — no target registered.")
                continue

            picked_products = prompt_product_selection(validated)
            for p in picked_products:
                target_url = normalize_url(p["url"])
                db.add_target(target_url)
                print(f"Registered: {target_url}  (price seen: {p.get('price', '?')})")
    finally:
        await watcher.browsermgr.stop()

    all_targets = db.get_all_targets()
    print(f"\nDone. {len(all_targets)} target(s) total in watch_targets.")


if __name__ == "__main__":
    niche_arg = " ".join(sys.argv[1:]).strip()
    niche_input = niche_arg or input("What niche or company are you tracking competitors for? ").strip()
    if not niche_input:
        print("Need a niche to search for.")
        sys.exit(1)
    asyncio.run(onboard_niche(niche_input))