"""SQLite crawl cache + co-occurrence source of truth. One DB per game, since
Nexus mod ids are only unique within a game."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    author TEXT,
    category TEXT,
    endorsements INTEGER NOT NULL DEFAULT 0,
    total_downloads INTEGER,
    unique_downloads INTEGER,
    rating REAL,
    rating_count INTEGER,
    adult INTEGER NOT NULL DEFAULT 0,
    mod_count INTEGER,
    latest_revision INTEGER,
    updated_at TEXT,
    last_published_at TEXT,
    listed_rank INTEGER,          -- position in the crawl ordering
    fetched_revision INTEGER,     -- revision whose mod list is in collection_mods
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS collection_mods (
    collection_id INTEGER NOT NULL,
    mod_id INTEGER NOT NULL,
    optional INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection_id, mod_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_mod ON collection_mods(mod_id);

CREATE TABLE IF NOT EXISTS mods (
    mod_id INTEGER PRIMARY KEY,
    uid TEXT,
    name TEXT,
    author TEXT,
    category TEXT,
    category_id INTEGER,
    status TEXT,
    adult INTEGER NOT NULL DEFAULT 0,
    endorsements INTEGER,
    downloads INTEGER,            -- v2 Mod.downloads (total)
    unique_downloads INTEGER,     -- v1 mod_unique_downloads, when fetched
    created_at TEXT,
    updated_at TEXT,
    fetched_at TEXT,
    requiring_count INTEGER,      -- how many Nexus mods require this one
    reqs_fetched_at TEXT,
    global_collection_count INTEGER,
    gcc_fetched_at TEXT,
    v1_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS mod_requirements (
    mod_id INTEGER NOT NULL,
    required_mod_id INTEGER NOT NULL,
    required_name TEXT,
    notes TEXT,
    PRIMARY KEY (mod_id, required_mod_id)
);
CREATE INDEX IF NOT EXISTS idx_req_required ON mod_requirements(required_mod_id);

CREATE TABLE IF NOT EXISTS installed (
    mod_id INTEGER PRIMARY KEY,
    name TEXT,
    adapter TEXT,
    scanned_at TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def upsert_collection(conn: sqlite3.Connection, node: dict[str, Any], rank: int) -> None:
    rev = node.get("latestPublishedRevision") or {}
    rating = node.get("overallRating")
    conn.execute(
        """
        INSERT INTO collections (id, slug, name, author, category, endorsements, total_downloads,
            unique_downloads, rating, rating_count, adult, mod_count, latest_revision, updated_at,
            last_published_at, listed_rank)
        VALUES (:id, :slug, :name, :author, :category, :endorsements, :total_downloads,
            :unique_downloads, :rating, :rating_count, :adult, :mod_count, :latest_revision,
            :updated_at, :last_published_at, :rank)
        ON CONFLICT(id) DO UPDATE SET
            slug=excluded.slug, name=excluded.name, author=excluded.author,
            category=excluded.category, endorsements=excluded.endorsements,
            total_downloads=excluded.total_downloads, unique_downloads=excluded.unique_downloads,
            rating=excluded.rating, rating_count=excluded.rating_count, adult=excluded.adult,
            mod_count=excluded.mod_count, latest_revision=excluded.latest_revision,
            updated_at=excluded.updated_at, last_published_at=excluded.last_published_at,
            listed_rank=excluded.listed_rank
        """,
        {
            "id": node["id"],
            "slug": node["slug"],
            "name": node["name"],
            "author": (node.get("user") or {}).get("name"),
            "category": (node.get("category") or {}).get("name"),
            "endorsements": node.get("endorsements") or 0,
            "total_downloads": node.get("totalDownloads"),
            "unique_downloads": node.get("uniqueDownloads"),
            "rating": float(rating) if rating not in (None, "") else None,
            "rating_count": node.get("overallRatingCount"),
            "adult": int(bool(node.get("adultContent"))),
            "mod_count": rev.get("modCount"),
            "latest_revision": rev.get("revisionNumber"),
            "updated_at": node.get("updatedAt"),
            "last_published_at": node.get("lastPublishedAt"),
            "rank": rank,
        },
    )


def upsert_mod(conn: sqlite3.Connection, mod: dict[str, Any]) -> None:
    cat = mod.get("modCategory") or {}
    conn.execute(
        """
        INSERT INTO mods (mod_id, uid, name, author, category, category_id, status, adult,
            endorsements, downloads, created_at, updated_at, fetched_at)
        VALUES (:mod_id, :uid, :name, :author, :category, :category_id, :status, :adult,
            :endorsements, :downloads, :created_at, :updated_at, :fetched_at)
        ON CONFLICT(mod_id) DO UPDATE SET
            uid=excluded.uid, name=excluded.name, author=excluded.author,
            category=excluded.category, category_id=excluded.category_id,
            status=excluded.status, adult=excluded.adult, endorsements=excluded.endorsements,
            downloads=excluded.downloads, created_at=excluded.created_at,
            updated_at=excluded.updated_at, fetched_at=excluded.fetched_at
        """,
        {
            "mod_id": int(mod["modId"]),
            "uid": mod.get("uid"),
            "name": mod.get("name"),
            "author": mod.get("author"),
            "category": cat.get("name") or mod.get("category"),
            "category_id": cat.get("categoryId"),
            "status": mod.get("status"),
            "adult": int(bool(mod.get("adultContent"))),
            "endorsements": mod.get("endorsements"),
            "downloads": mod.get("downloads"),
            "created_at": mod.get("createdAt"),
            "updated_at": mod.get("updatedAt"),
            "fetched_at": now(),
        },
    )


def replace_collection_mods(
    conn: sqlite3.Connection, collection_id: int, revision: int, mods: Iterable[tuple[int, bool]]
) -> None:
    conn.execute("DELETE FROM collection_mods WHERE collection_id=?", (collection_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO collection_mods(collection_id, mod_id, optional) VALUES (?, ?, ?)",
        [(collection_id, m, int(opt)) for m, opt in mods],
    )
    conn.execute(
        "UPDATE collections SET fetched_revision=?, fetched_at=? WHERE id=?",
        (revision, now(), collection_id),
    )


def replace_installed(conn: sqlite3.Connection, mods: Iterable[tuple[int, str, str]]) -> None:
    ts = now()
    conn.execute("DELETE FROM installed")
    conn.executemany(
        "INSERT OR REPLACE INTO installed(mod_id, name, adapter, scanned_at) VALUES (?, ?, ?, ?)",
        [(m, n, a, ts) for m, n, a in mods],
    )


def installed_ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT mod_id FROM installed ORDER BY mod_id")]
