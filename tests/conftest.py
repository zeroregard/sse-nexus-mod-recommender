from __future__ import annotations

import pytest

from modrec import db


def add_collection(conn, cid: int, mods: list[int], endorsements: int = 1000, **kw) -> None:
    db.upsert_collection(
        conn,
        {
            "id": cid,
            "slug": f"c{cid}",
            "name": kw.get("name", f"Collection {cid}"),
            "endorsements": endorsements,
            "overallRating": kw.get("rating", "80"),
            "adultContent": False,
            "lastPublishedAt": kw.get("published", "2026-06-01T00:00:00Z"),
            "latestPublishedRevision": {"revisionNumber": 1, "modCount": len(mods)},
        },
        rank=cid,
    )
    db.replace_collection_mods(conn, cid, 1, [(m, False) for m in mods])


def add_mod(conn, mod_id: int, name: str | None = None, **kw) -> None:
    db.upsert_mod(
        conn,
        {
            "modId": mod_id,
            "name": name or f"Mod {mod_id}",
            "author": "someone",
            "status": "published",
            "endorsements": kw.get("endorsements", 100),
            "downloads": kw.get("downloads", 10000),
            "updatedAt": kw.get("updated", "2026-01-01T00:00:00Z"),
            "modCategory": {"categoryId": 1, "name": kw.get("category", f"Cat{mod_id}")},
        },
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """30 collections. Mod 1 (USSEP-like) is in all of them, mod 2 in most.
    A niche cluster {10, 11, 12} co-occurs in 6 collections; the user has 1, 10, 11.
    Mod 12 should beat popular mod 2 even though 2 co-occurs with the user's
    mods far more often in raw counts."""
    filler = []
    for cid in range(1, 31):
        own = [1000 + cid * 10 + i for i in range(8)]  # unique per collection: no dedupe
        filler += own
        mods = [1, *own]
        if cid <= 26:
            mods.append(2)
        if cid <= 6:
            mods += [10, 11, 12]
        if cid in (7, 8):
            mods += [10]
        add_collection(conn, cid, mods)
    for m in [1, 2, 10, 11, 12, *filler]:
        add_mod(conn, m)
    conn.commit()
    return conn
