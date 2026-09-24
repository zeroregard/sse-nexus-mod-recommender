"""Populate the local index. Every step is resumable: each unit of work commits
on its own, and anything already cached at the current revision is skipped, so
Ctrl-C at any point loses at most one in-flight request."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable

from . import config, db
from .config import Game
from .nexus import (
    COLLECTIONS_QUERY,
    MODS_QUERY,
    REVISION_QUERY,
    SORTS,
    GraphQLClient,
    NexusError,
    QuotaExhausted,
    RestClient,
    global_count_query,
    mod_uid,
)

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def list_collections(
    conn: sqlite3.Connection,
    gql: GraphQLClient,
    game: Game,
    limit: int,
    sort: str = "endorsements",
    include_adult: bool = config.INCLUDE_ADULT,
    progress: Progress = _noop,
) -> list[int]:
    """Refresh metadata for the top `limit` collections (cheap: 50 per request).
    Returns their ids in crawl order."""
    ids: list[int] = []
    offset = 0
    while len(ids) < limit:
        data = gql.query(
            COLLECTIONS_QUERY,
            {
                "domain": game.domain,
                "offset": offset,
                "count": config.COLLECTION_PAGE_SIZE,
                "sort": SORTS[sort],
            },
        )["collectionsV2"]
        nodes = data["nodes"]
        db.set_meta(conn, "game_collection_total", data["totalCount"])
        if not nodes:
            break
        for node in nodes:
            if not include_adult and node.get("adultContent"):
                continue
            db.upsert_collection(conn, node, rank=offset + len(ids))
            ids.append(node["id"])
            if len(ids) >= limit:
                break
        conn.commit()
        offset += len(nodes)
        progress(f"listed {len(ids)}/{limit} collections")
        if offset >= data["totalCount"]:
            break
    return ids


def fetch_collection(conn: sqlite3.Connection, gql: GraphQLClient, game: Game, row) -> int:
    data = gql.query(
        REVISION_QUERY,
        {"slug": row["slug"], "domain": game.domain, "revision": row["latest_revision"]},
    )
    rev = data["collectionRevision"]
    mods: dict[int, bool] = {}
    for entry in rev["modFiles"]:
        mod = (entry.get("file") or {}).get("mod")
        if not mod:  # file or mod deleted/hidden since the revision was published
            continue
        mid = int(mod["modId"])
        db.upsert_mod(conn, mod)
        # A mod appearing as both required and optional counts as required.
        mods[mid] = mods.get(mid, True) and bool(entry.get("optional"))
    db.replace_collection_mods(conn, row["id"], rev["revisionNumber"], mods.items())
    conn.commit()
    return len(mods)


def fetch_collections(
    conn: sqlite3.Connection,
    gql: GraphQLClient,
    game: Game,
    ids: list[int],
    refresh: bool = False,
    progress: Progress = _noop,
) -> tuple[int, int]:
    """Fetch mod lists for collections whose latest revision isn't cached."""
    todo = []
    for cid in ids:
        row = conn.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
        if (row["mod_count"] or 0) < config.MIN_COLLECTION_SIZE:
            continue
        if not refresh and row["fetched_revision"] == row["latest_revision"]:
            continue
        todo.append(row)
    fetched = failed = 0
    for i, row in enumerate(todo, 1):
        try:
            n = fetch_collection(conn, gql, game, row)
            fetched += 1
            progress(f"[{i}/{len(todo)}] {row['name']} ({n} mods)")
        except NexusError as exc:
            conn.rollback()
            failed += 1
            log.warning("skipping collection %s (%s): %s", row["id"], row["name"], exc)
    return fetched, failed


def _chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def fetch_mod_details(
    conn: sqlite3.Connection,
    gql: GraphQLClient,
    game: Game,
    mod_ids: Iterable[int],
    refresh: bool = False,
    progress: Progress = _noop,
) -> int:
    """Metadata + requirements (both directions) for the given mods."""
    ids = sorted(set(mod_ids))
    if not refresh:
        have = {
            r[0] for r in conn.execute("SELECT mod_id FROM mods WHERE reqs_fetched_at IS NOT NULL")
        }
        ids = [m for m in ids if m not in have]
    done = 0
    for batch in _chunks(ids, config.MOD_BATCH_SIZE):
        data = gql.query(
            MODS_QUERY,
            {"ids": [{"gameId": game.game_id, "modId": m} for m in batch], "count": len(batch)},
        )
        ts = db.now()
        for node in data["legacyMods"]["nodes"]:
            mid = int(node["modId"])
            db.upsert_mod(conn, node)
            reqs = node.get("modRequirements") or {}
            conn.execute("DELETE FROM mod_requirements WHERE mod_id=?", (mid,))
            for req in (reqs.get("nexusRequirements") or {}).get("nodes") or []:
                if req.get("externalRequirement") or str(req.get("gameId")) != str(game.game_id):
                    continue
                try:
                    rid = int(req["modId"])
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO mod_requirements VALUES (?, ?, ?, ?)",
                    (mid, rid, req.get("modName"), req.get("notes")),
                )
            requiring = (reqs.get("modsRequiringThisMod") or {}).get("totalCount")
            conn.execute(
                "UPDATE mods SET requiring_count=?, reqs_fetched_at=? WHERE mod_id=?",
                (requiring, ts, mid),
            )
        # Mods the API didn't return (deleted/hidden): mark so we don't retry forever.
        for mid in batch:
            conn.execute(
                "INSERT INTO mods(mod_id, status, reqs_fetched_at) VALUES (?, 'unavailable', ?) "
                "ON CONFLICT(mod_id) DO UPDATE SET reqs_fetched_at=COALESCE(reqs_fetched_at, ?)",
                (mid, ts, ts),
            )
        conn.commit()
        done += len(batch)
        progress(f"mod details {done}/{len(ids)}")
    return done


def fetch_global_counts(
    conn: sqlite3.Connection,
    gql: GraphQLClient,
    game: Game,
    mod_ids: Iterable[int],
    refresh: bool = False,
    progress: Progress = _noop,
) -> int:
    """How many collections across the *whole* game contain each mod — the
    reverse lookup the v2 schema provides. Feeds idf() for the user's mods."""
    ids = sorted(set(mod_ids))
    if not refresh:
        have = {
            r[0] for r in conn.execute("SELECT mod_id FROM mods WHERE gcc_fetched_at IS NOT NULL")
        }
        ids = [m for m in ids if m not in have]
    done = 0
    for batch in _chunks(ids, config.GLOBAL_COUNT_BATCH_SIZE):
        uids = [mod_uid(game.game_id, m) for m in batch]
        data = gql.query(global_count_query(game.domain, uids))
        ts = db.now()
        for i, mid in enumerate(batch):
            count = (data.get(f"m{i}") or {}).get("totalCount")
            conn.execute(
                "INSERT INTO mods(mod_id, global_collection_count, gcc_fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(mod_id) DO UPDATE SET global_collection_count=excluded.global_collection_count, "
                "gcc_fetched_at=excluded.gcc_fetched_at",
                (mid, count, ts),
            )
        conn.commit()
        done += len(batch)
        progress(f"global collection counts {done}/{len(ids)}")
    return done


def fetch_unique_downloads(
    conn: sqlite3.Connection,
    game: Game,
    mod_ids: Iterable[int],
    budget: int,
    progress: Progress = _noop,
) -> int:
    """v1 fallback: the unique-download figure isn't on the v2 Mod type. Costs
    one v1 request per mod, so it's budgeted and stops at the quota reserve."""
    have = {r[0] for r in conn.execute("SELECT mod_id FROM mods WHERE v1_fetched_at IS NOT NULL")}
    ids = [m for m in mod_ids if m not in have][:budget]
    if not ids:
        return 0
    rest = RestClient()
    done = 0
    try:
        for mid in ids:
            data = rest.mod(game.domain, mid)
            conn.execute(
                "UPDATE mods SET unique_downloads=?, v1_fetched_at=? WHERE mod_id=?",
                ((data or {}).get("mod_unique_downloads"), db.now(), mid),
            )
            conn.commit()
            done += 1
            if done % 25 == 0:
                progress(
                    f"v1 unique downloads {done}/{len(ids)} (hourly left: {rest.hourly_remaining})"
                )
    except QuotaExhausted as exc:
        log.warning("stopping v1 enrichment: %s", exc)
    finally:
        rest.close()
    return done


def candidate_mod_ids(conn: sqlite3.Connection, min_collections: int) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT mod_id FROM collection_mods GROUP BY mod_id HAVING COUNT(*) >= ?",
            (min_collections,),
        )
    ]
