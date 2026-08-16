"""
discovery.py
Turns a niche description (or seed company name) into a list of candidate
competitor companies, using Exa's Deep search (type="deep") with a
structured output_schema — this is the actual API surface, confirmed
against Exa's docs, not the plain category="company" search.

Deliberately pure and side-effect-free: no DB writes, no prompting. Exa
charges per call (~$0.007 seen in testing) and Deep search has real latency
(sub-second to a few seconds), so don't loop-call this while iterating on
other parts of the pipeline.
"""
import config
from exa_py import Exa

exa = Exa(api_key=config.EXA_API_KEY)

COMPETITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "competitors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "location": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "One sentence on what they sell and what makes them notable.",
                    },
                },
                "required": ["name", "url"],
            },
        }
    },
    "required": ["competitors"],
}


def find_competitors(niche_or_company: str, num_results: int = 10) -> list[dict]:
    """
    niche_or_company: either a niche description ("premium dog toys DTC brands")
                       or a seed company/product ("competitors of Chewy"). Try a
                       couple of phrasings against your actual demo niche.

    Returns a list of dicts: {"name", "url", "location", "description"}.
    Returns [] on any API failure — callers (selector.py) should treat an
    empty list as "discovery failed, offer a manual URL instead", not as
    "there are genuinely zero competitors."
    """
    try:
        result = exa.search(
            niche_or_company,
            type="deep",
            num_results=num_results,
            output_schema=COMPETITOR_SCHEMA,
        )
    except Exception as e:
        print(f"[discovery] Exa search failed for {niche_or_company!r}: {e}")
        return []

    raw_hit_count = len(result.results) if getattr(result, "results", None) else 0

    output = getattr(result, "output", None)
    if not output or not getattr(output, "content", None):
        print(f"[discovery] Exa returned no structured output for {niche_or_company!r}")
        return []

    competitors = output.content.get("competitors", [])

    if raw_hit_count and len(competitors) < raw_hit_count:
        # Not necessarily a bug — the extraction step may correctly drop hits
        # that aren't real companies (marketplace listings, aggregator pages).
        # Worth a glance if this gap is consistently large for your niche.
        print(
            f"[discovery] {raw_hit_count} raw hits -> {len(competitors)} structured "
            f"competitors ({raw_hit_count - len(competitors)} dropped during extraction)"
        )

    return [
        {
            "name": c.get("name"),
            "url": c.get("url"),
            "location": c.get("location"),
            "description": c.get("description"),
        }
        for c in competitors
        if c.get("url")  # drop anything without a URL — selector.py can't do anything with it
    ]


def print_candidates(candidates: list[dict]) -> None:
    if not candidates:
        print("No candidates found.")
        return
    for i, c in enumerate(candidates, start=1):
        print(f"{i}. {c['name']} — {c['url']}")
        if c.get("location"):
            print(f"   {c['location']}")
        if c.get("description"):
            print(f"   {c['description']}")


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "premium dog toys DTC brands"
    print(f"Searching: {query!r}\n")
    results = find_competitors(query)
    print_candidates(results)