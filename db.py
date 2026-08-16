"""
db.py
SQLite persistence layer for Stealth Monitor.

CHANGED from your version: watch_targets gains two columns so watcher.py
can cache *which extraction strategy worked*, not just the last fingerprint
value. Without this, resolve_target_state() has nothing to read on
subsequent polls and re-runs the full tier cascade every single time.

  - resolved_strategy: 'shopify' | 'jsonld' | 'meta_tag' | 'sniffed_api' | 'hash'
  - api_endpoint_url:  only set when resolved_strategy == 'sniffed_api'.
                        Lets future polls hit this directly with plain
                        requests.get() instead of launching Playwright again.

Three tables, unchanged otherwise:
  - watch_targets: one row per tracked URL.
  - line_items: current snapshot of Shopify product/variant state.
  - change_log: append-only audit trail, one row per changed field.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_targets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    url                TEXT NOT NULL UNIQUE,
    fingerprint_type   TEXT NOT NULL,       -- 'etag' | 'last_modified' | 'sha256' | 'shopify_json' | 'state'
    fingerprint_value  TEXT,
    raw_state_json     TEXT,                -- ONLY for non-shopify targets
    resolved_strategy  TEXT,                -- cached winning tier — see module docstring
    api_endpoint_url   TEXT,                -- cached sniffed API endpoint, if resolved_strategy = 'sniffed_api'
    last_checked       TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    variant_id     TEXT NOT NULL,
    product_title  TEXT,
    price          TEXT,
    available      INTEGER,
    updated_at     TEXT NOT NULL,
    UNIQUE(url, variant_id)
);
CREATE INDEX IF NOT EXISTS idx_line_items_url ON line_items(url);

CREATE TABLE IF NOT EXISTS change_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    product_id      TEXT,
    variant_id      TEXT,
    product_title   TEXT,
    field_changed   TEXT,
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_log_url ON change_log(url);
"""

# Columns that might be missing on a monitor.db created before this update.
# init_db() adds them with ALTER TABLE if they're not there yet, so you
# don't have to delete your existing monitor.db to pick this up.
MIGRATIONS = [
    "ALTER TABLE watch_targets ADD COLUMN resolved_strategy TEXT",
    "ALTER TABLE watch_targets ADD COLUMN api_endpoint_url TEXT",
]


class DB:
    def __init__(self, db_path: str = config.MONITOR_DB):
        self.db_path = db_path
        self.init_db()

    @contextmanager
    def get_db(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self):
        with self.get_db() as conn:
            conn.executescript(SCHEMA)
            for migration in MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---------------------------------------------------------------------
    # watch_targets
    # ---------------------------------------------------------------------

    def add_target(self, url: str) -> None:
        with self.get_db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO watch_targets
                   (url, fingerprint_type, fingerprint_value, raw_state_json, last_checked, created_at)
                   VALUES (?, '', '', '', NULL, ?)""",
                (url, self._now()),
            )

    def get_all_targets(self) -> list[sqlite3.Row]:
        with self.get_db() as conn:
            return conn.execute("SELECT * FROM watch_targets").fetchall()

    def get_baseline(self, url: str) -> sqlite3.Row | None:
        with self.get_db() as conn:
            return conn.execute(
                "SELECT * FROM watch_targets WHERE url = ?", (url,)
            ).fetchone()

    def update_baseline(
        self,
        url: str,
        fingerprint_type: str,
        fingerprint_value: str,
        raw_state_json: str | None = None,
        resolved_strategy: str | None = None,
        api_endpoint_url: str | None = None,
    ) -> None:
        with self.get_db() as conn:
            conn.execute(
                """UPDATE watch_targets
                   SET fingerprint_type = ?, fingerprint_value = ?,
                       raw_state_json = ?, resolved_strategy = ?,
                       api_endpoint_url = ?, last_checked = ?
                   WHERE url = ?""",
                (
                    fingerprint_type, fingerprint_value, raw_state_json,
                    resolved_strategy, api_endpoint_url, self._now(), url,
                ),
            )

    # ---------------------------------------------------------------------
    # line_items
    # ---------------------------------------------------------------------

    def get_line_items(self, url: str) -> dict[str, sqlite3.Row]:
        with self.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM line_items WHERE url = ?", (url,)
            ).fetchall()
        return {row["variant_id"]: row for row in rows}

    def upsert_line_items(self, url: str, items: list[dict]) -> None:
        now = self._now()
        with self.get_db() as conn:
            conn.executemany(
                """INSERT INTO line_items
                       (url, product_id, variant_id, product_title, price, available, updated_at)
                   VALUES (:url, :product_id, :variant_id, :product_title, :price, :available, :updated_at)
                   ON CONFLICT(url, variant_id) DO UPDATE SET
                       product_id    = excluded.product_id,
                       product_title = excluded.product_title,
                       price         = excluded.price,
                       available     = excluded.available,
                       updated_at    = excluded.updated_at""",
                [
                    {
                        "url": url,
                        "product_id": str(item["product_id"]),
                        "variant_id": str(item["variant_id"]),
                        "product_title": item.get("product_title"),
                        "price": item.get("price"),
                        "available": int(bool(item.get("available"))),
                        "updated_at": now,
                    }
                    for item in items
                ],
            )

    def remove_line_items(self, url: str, variant_ids: list[str]) -> None:
        if not variant_ids:
            return
        with self.get_db() as conn:
            conn.executemany(
                "DELETE FROM line_items WHERE url = ? AND variant_id = ?",
                [(url, vid) for vid in variant_ids],
            )

    # ---------------------------------------------------------------------
    # change_log
    # ---------------------------------------------------------------------

    def log_item_change(
        self, url: str, product_id: str, variant_id: str,
        product_title: str | None, field_changed: str, old_value, new_value,
    ) -> int:
        with self.get_db() as conn:
            cur = conn.execute(
                """INSERT INTO change_log
                       (url, product_id, variant_id, product_title, field_changed, old_value, new_value, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    url, str(product_id), str(variant_id), product_title,
                    field_changed, str(old_value), str(new_value), self._now(),
                ),
            )
            return cur.lastrowid

    def log_page_change(self, url: str, old_value: str, new_value: str) -> int:
        with self.get_db() as conn:
            cur = conn.execute(
                """INSERT INTO change_log
                       (url, product_id, variant_id, product_title, field_changed, old_value, new_value, detected_at)
                   VALUES (?, NULL, NULL, NULL, 'page_hash', ?, ?, ?)""",
                (url, old_value, new_value, self._now()),
            )
            return cur.lastrowid

    def get_recent_changes(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.get_db() as conn:
            return conn.execute(
                "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()


if __name__ == "__main__":
    db = DB()
    print(f"Initialized schema at {db.db_path}")