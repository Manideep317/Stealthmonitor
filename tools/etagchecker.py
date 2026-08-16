import hashlib
import sqlite3
import requests
from contextlib import contextmanager

DB_PATH = "etag_cache.db"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


@contextmanager
def get_db(db_path: str = DB_PATH):
    """Yield a sqlite3 connection, ensuring it's closed afterwards."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS site_state (
                url TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT
            )
            """
        )
        conn.commit()


def fetch_headers_or_hash(url: str, timeout: int = 10) -> dict:
    """
    Fetch a URL once. Prefer ETag/Last-Modified if present;
    otherwise fall back to a content hash. Only ever does ONE request.
    """
    result = {"url": url, "etag": None, "last_modified": None, "content_hash": None}

    try:
        res = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        res.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return result
    etag=res.headers.get('ETag') or res.headers.get('etag') or res.headers.get('ETAG') or res.headers.get('eTag') or res.headers.get('Etag') or res.headers.get('ETag')
    last_modified = res.headers.get('Last-Modified') or res.headers.get('last-modified')

    result["etag"] = etag
    result["last_modified"] = last_modified

    if not etag and not last_modified:
        result["content_hash"] = hashlib.sha256(res.content).hexdigest()[:16]

    return result


def get_stored_state(url: str, db_path: str = DB_PATH):
    with get_db(db_path) as conn:
        cursor = conn.execute(
            "SELECT etag, last_modified, content_hash FROM site_state WHERE url = ?",
            (url,),
        )
        row = cursor.fetchone()
    if row:
        return {"etag": row[0], "last_modified": row[1], "content_hash": row[2]}
    return None


def upsert_state(state: dict, db_path: str = DB_PATH) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO site_state (url, etag, last_modified, content_hash)
            VALUES (:url, :etag, :last_modified, :content_hash)
            ON CONFLICT(url) DO UPDATE SET
                etag=excluded.etag,
                last_modified=excluded.last_modified,
                content_hash=excluded.content_hash
            """,
            state,
        )
        conn.commit()


def has_site_changed(url: str, db_path: str = DB_PATH) -> bool:
    """
    Checks whether the site's ETag / Last-Modified / content hash differs
    from what's stored. Prints a CLI report and updates the stored state.
    Returns True if changed.
    """
    current = fetch_headers_or_hash(url)
    previous = get_stored_state(url, db_path)

    is_first_check = previous is None
    changed = is_first_check or (
        current["etag"] != previous["etag"]
        or current["last_modified"] != previous["last_modified"]
        or current["content_hash"] != previous["content_hash"]
    )

    # ---- CLI output ----
    print("=" * 60)
    print(f"URL: {url}")
    print("-" * 60)

    old_etag = previous["etag"] if previous else None
    old_lm = previous["last_modified"] if previous else None
    old_hash = previous["content_hash"] if previous else None

    print(f"Previous ETag:         {old_etag}")
    print(f"Current  ETag:         {current['etag']}")
    print(f"Previous Last-Modified: {old_lm}")
    print(f"Current  Last-Modified: {current['last_modified']}")
    print(f"Previous Content Hash: {old_hash}")
    print(f"Current  Content Hash: {current['content_hash']}")
    print("-" * 60)

    if is_first_check:
        print("STATUS: First check — baseline stored. No prior state to compare.")
    elif changed:
        print("STATUS: ✅ CHANGED — the site content/headers are different.")
    else:
        print("STATUS: ⏸️  UNCHANGED — no difference detected.")
    print("=" * 60)

    upsert_state(current, db_path)
    return changed


if __name__ == "__main__":
    init_db()
    test_url = "https://ramadevifoods.com/"
    has_site_changed(test_url)