"""Load the crawl cache into a weighted, deduplicated collection × mod matrix."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import scipy.sparse as sp

from . import config


def years_since(ts: str | None, now: datetime | None = None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    now = now or datetime.now(UTC)
    return max(0.0, (now - dt).total_seconds() / (365.25 * 86400))


def decay(age_years: float | None, half_life: float, floor: float) -> float:
    if age_years is None:
        return 1.0
    return floor + (1.0 - floor) * 0.5 ** (age_years / half_life)


def collection_weight(row: dict, cfg=config) -> float:
    endorse = math.log1p(max(0, row.get("endorsements") or 0)) ** cfg.COLLECTION_ENDORSE_EXP
    size = row.get("size") or row.get("mod_count") or 0
    size_f = min(1.0, size / cfg.COLLECTION_FULL_WEIGHT_SIZE)
    if size > cfg.COLLECTION_LARGE_REF_SIZE:
        size_f *= (cfg.COLLECTION_LARGE_REF_SIZE / size) ** cfg.COLLECTION_LARGE_EXP
    rating = row.get("rating")
    if rating is None:
        rating = cfg.COLLECTION_DEFAULT_RATING
    rating_f = max(rating, 1.0) / 100.0
    rating_f **= cfg.COLLECTION_RATING_EXP
    recency_f = decay(
        years_since(row.get("last_published_at") or row.get("updated_at")),
        cfg.COLLECTION_RECENCY_HALF_LIFE_Y,
        cfg.COLLECTION_RECENCY_FLOOR,
    )
    # +1 so an unendorsed collection still counts a little.
    return (endorse + 1.0) * size_f * rating_f * recency_f


@dataclass
class Index:
    col_ids: np.ndarray  # (K,) kept collection ids
    weights: np.ndarray  # (K,)
    mod_ids: np.ndarray  # (M,)
    X: sp.csc_matrix  # (K, M) binary membership
    collections: dict[int, dict]  # id -> metadata row (all fetched, incl. duplicates)
    duplicates: dict[int, list[int]]  # kept id -> collapsed duplicate ids
    mods: dict[int, dict]  # mod_id -> metadata row
    requires: dict[int, set[int]]  # mod -> mods it requires
    total_game_collections: int
    mod_pos: dict[int, int] = field(default_factory=dict)
    col_pos: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.mod_pos = {int(m): i for i, m in enumerate(self.mod_ids)}
        self.col_pos = {int(c): i for i, c in enumerate(self.col_ids)}
        self.df = np.asarray(self.X.sum(axis=0)).ravel()  # distinct collections per mod
        self.wdf = np.asarray(self.X.T @ self.weights).ravel()  # weighted
        self.W = float(self.weights.sum())

    @property
    def K(self) -> int:
        return len(self.col_ids)

    def without_rows(self, rows: list[int]) -> Index:
        """A copy with some collections removed (for leave-one-out evaluation)."""
        keep = np.setdiff1d(np.arange(self.K), np.asarray(rows, dtype=int))
        return Index(
            col_ids=self.col_ids[keep],
            weights=self.weights[keep],
            mod_ids=self.mod_ids,
            X=self.X[keep].tocsc(),
            collections=self.collections,
            duplicates=self.duplicates,
            mods=self.mods,
            requires=self.requires,
            total_game_collections=self.total_game_collections,
        )

    def collections_of(self, mod_id: int) -> np.ndarray:
        """Row positions of kept collections containing mod_id."""
        j = self.mod_pos.get(mod_id)
        if j is None:
            return np.array([], dtype=int)
        col = self.X.getcol(j)
        return col.indices


def dedupe(
    X: sp.csr_matrix, weights: np.ndarray, threshold: float
) -> tuple[np.ndarray, dict[int, list[int]]]:
    """Greedy near-duplicate collapse. Visit collections heaviest-first; one whose
    Jaccard with an already-kept collection exceeds `threshold` is folded into it.
    Returns (kept row positions, {kept pos: [duplicate positions]})."""
    Xb = (X > 0).astype(np.float32).tocsr()
    sizes = np.asarray(Xb.sum(axis=1)).ravel()
    inter = (Xb @ Xb.T).tocsr()
    order = np.argsort(-weights, kind="stable")
    owner = np.full(X.shape[0], -1)
    dupes: dict[int, list[int]] = {}
    for i in order:
        row = inter.getrow(i)
        best, best_j = -1, 0.0
        for j, n in zip(row.indices, row.data):
            if j == i or owner[j] != j:
                continue  # only compare against kept collections
            union = sizes[i] + sizes[j] - n
            jac = n / union if union else 0.0
            if jac > threshold and jac > best_j:
                best, best_j = j, jac
        if best >= 0:
            owner[i] = best
            dupes.setdefault(int(best), []).append(int(i))
        else:
            owner[i] = i
    kept = np.array(sorted(i for i in range(X.shape[0]) if owner[i] == i), dtype=int)
    return kept, dupes


def load_index(
    conn: sqlite3.Connection, include_adult: bool = config.INCLUDE_ADULT, cfg=config
) -> Index:
    cols = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM collections WHERE fetched_revision IS NOT NULL ORDER BY id"
        )
    ]
    if not include_adult:
        cols = [c for c in cols if not c["adult"]]
    if not cols:
        raise RuntimeError("The index is empty. Run `modrec crawl` first.")
    col_index = {c["id"]: i for i, c in enumerate(cols)}

    pairs = conn.execute("SELECT collection_id, mod_id FROM collection_mods").fetchall()
    pairs = [(c, m) for c, m in pairs if c in col_index]
    mod_ids = np.array(sorted({m for _, m in pairs}), dtype=np.int64)
    mpos = {int(m): j for j, m in enumerate(mod_ids)}
    rows = np.array([col_index[c] for c, _ in pairs], dtype=np.int64)
    colsj = np.array([mpos[m] for _, m in pairs], dtype=np.int64)
    X = sp.csr_matrix(
        (np.ones(len(pairs), dtype=np.float32), (rows, colsj)), shape=(len(cols), len(mod_ids))
    )
    X.data[:] = 1.0  # collapse any duplicate entries

    sizes = np.asarray(X.sum(axis=1)).ravel()
    for c, s in zip(cols, sizes):
        c["size"] = int(s)
    weights = np.array([collection_weight(c, cfg) for c in cols], dtype=np.float64)
    for c, w in zip(cols, weights):
        c["weight"] = float(w)

    kept, dup_pos = dedupe(X, weights, cfg.DEDUPE_JACCARD)
    duplicates = {cols[k]["id"]: [cols[d]["id"] for d in ds] for k, ds in dup_pos.items()}

    mods = {r["mod_id"]: dict(r) for r in conn.execute("SELECT * FROM mods")}
    requires: dict[int, set[int]] = {}
    for mid, rid in conn.execute("SELECT mod_id, required_mod_id FROM mod_requirements"):
        if mid != rid:
            requires.setdefault(mid, set()).add(rid)

    from . import db  # local import: avoid a cycle at module load

    total = int(db.get_meta(conn, "game_collection_total", "0") or 0) or len(cols)
    return Index(
        col_ids=np.array([cols[k]["id"] for k in kept], dtype=np.int64),
        weights=weights[kept],
        mod_ids=mod_ids,
        X=X[kept].tocsc(),
        collections={c["id"]: c for c in cols},
        duplicates=duplicates,
        mods=mods,
        requires=requires,
        total_game_collections=total,
    )
