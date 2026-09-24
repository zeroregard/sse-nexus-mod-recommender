"""Turn the co-occurrence index + the user's mods into ranked, explained
recommendations. All weights come from `config`."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

import numpy as np

from . import config
from .index import Index, decay, years_since

BLOCK = 64  # installed mods per sparse-product block


def nexus_url(domain: str, mod_id: int) -> str:
    return f"https://www.nexusmods.com/{domain}/mods/{mod_id}"


@dataclass
class SharedCollection:
    id: int
    name: str
    slug: str
    weight: float


@dataclass
class Driver:
    mod_id: int
    name: str | None
    contribution: float
    lift: float
    co_collections: int  # distinct deduped collections containing both
    idf: float
    collections: list[SharedCollection] = field(default_factory=list)
    # Your other mods in the same driver group (they share nearly all their
    # collections with this one, so they count once, not once each).
    similar: list[dict] = field(default_factory=list)


@dataclass
class Recommendation:
    mod_id: int
    name: str | None
    author: str | None
    category: str | None
    url: str
    score: float
    components: dict[str, float]
    collections: int  # deduped collections containing the mod
    drivers: list[Driver] = field(default_factory=list)
    missing_requirements: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Companion:
    mod_id: int
    name: str | None
    url: str
    required_by: list[dict]  # installed mods that list it as a requirement

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Scores:
    """Per-mod arrays aligned with index.mod_ids."""

    association: np.ndarray
    n_drivers: np.ndarray
    quality: np.ndarray
    recency: np.ndarray
    library: np.ndarray
    missing_req: np.ndarray
    final: np.ndarray
    eligible: np.ndarray  # bool
    required_by_installed: dict[int, list[int]]
    idf: dict[int, float]  # installed mod -> idf
    known_installed: list[int]
    unknown_installed: list[int]
    group_of: dict[int, int]  # installed mod -> driver group leader


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def idf_for(index: Index, mod_id: int, cfg=config) -> float:
    m = index.mods.get(mod_id) or {}
    gcc = m.get("global_collection_count")
    if cfg.IDF_USE_GLOBAL and gcc is not None and index.total_game_collections:
        n, df = index.total_game_collections, gcc
    else:
        j = index.mod_pos.get(mod_id)
        n, df = index.K, (index.df[j] if j is not None else 0)
    return max(cfg.IDF_FLOOR, math.log((n + 1) / (df + 1)))


def association(lift: np.ndarray, cfg=config) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        if cfg.ASSOCIATION == "lift":
            out = lift - 1.0
        elif cfg.ASSOCIATION == "ppmi":
            out = np.log(lift)
        else:
            raise ValueError(f"Unknown ASSOCIATION {cfg.ASSOCIATION!r}")
    return np.nan_to_num(np.clip(out, 0.0, None), nan=0.0, posinf=0.0)


def _mod_array(index: Index, key: str, default=np.nan) -> np.ndarray:
    return np.array(
        [
            (index.mods.get(int(m)) or {}).get(key)
            if (index.mods.get(int(m)) or {}).get(key) is not None
            else default
            for m in index.mod_ids
        ],
        dtype=np.float64,
    )


def quality_scores(index: Index, cfg=config) -> np.ndarray:
    """Endorsement *rate*, shrunk toward the index median, relative to it."""
    endorse = _mod_array(index, "endorsements", 0.0)
    total = _mod_array(index, "downloads")
    unique = _mod_array(index, "unique_downloads")
    dls = np.where(np.isnan(unique), total * cfg.UNIQUE_TO_TOTAL_RATIO, unique)
    valid = dls > 0
    if not valid.any():
        return np.ones(len(index.mod_ids))
    prior = float(np.median(endorse[valid] / dls[valid]))
    prior = prior if prior > 0 else 1e-3
    k = cfg.QUALITY_PRIOR_STRENGTH
    rate = (endorse + k * prior) / (np.nan_to_num(dls, nan=0.0) + k)
    q = (rate / prior) ** cfg.QUALITY_EXP
    return np.clip(q, cfg.QUALITY_MIN, cfg.QUALITY_MAX)


def recency_scores(index: Index, cfg=config) -> np.ndarray:
    return np.array(
        [
            decay(
                years_since((index.mods.get(int(m)) or {}).get("updated_at")),
                cfg.RECENCY_HALF_LIFE_Y,
                cfg.RECENCY_FLOOR,
            )
            for m in index.mod_ids
        ]
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def driver_groups(
    index: Index, known: list[int], idf: dict[int, float], cfg=config
) -> dict[int, int]:
    """Group the user's mods that live in nearly the same collections (a mod
    series, a mod + its patches). Without this, 40 mods from one author that
    only ever appear together count the same two collections 40 times.
    Returns {mod: group leader}; the leader is the member with the highest idf."""
    if not known:
        return {}
    pos = [index.mod_pos[m] for m in known]
    Xk = (index.X[:, pos] > 0).astype(np.float32).tocsc()
    sizes = np.asarray(Xk.sum(axis=0)).ravel()
    inter = (Xk.T @ Xk).tocsr()
    order = sorted(range(len(known)), key=lambda i: (-idf[known[i]], known[i]))
    leader_of = np.full(len(known), -1)
    for i in order:
        row = inter.getrow(i)
        best, best_j = -1, 0.0
        for j, n in zip(row.indices, row.data, strict=True):
            if j == i or leader_of[j] != j:
                continue
            union = sizes[i] + sizes[j] - n
            jac = n / union if union else 0.0
            if jac >= cfg.DRIVER_GROUP_JACCARD and jac > best_j:
                best, best_j = j, jac
        leader_of[i] = best if best >= 0 else i
    return {known[i]: known[leader_of[i]] for i in range(len(known))}


def score(
    index: Index,
    installed: Iterable[int],
    include_adult: bool = config.INCLUDE_ADULT,
    cfg=config,
) -> Scores:
    installed = {int(m) for m in installed}
    known = sorted(m for m in installed if m in index.mod_pos)
    unknown = sorted(installed - set(known))
    M = len(index.mod_ids)
    p = index.wdf / index.W
    X = index.X
    Xw = X.multiply(index.weights[:, None]).tocsc()

    idf = {m: idf_for(index, m, cfg) for m in installed}
    group_of = driver_groups(index, known, idf, cfg)
    # Process mods group by group so each group can contribute max(member).
    known_grouped = sorted(known, key=lambda m: (group_of[m], m))
    assoc_sum = np.zeros(M)
    n_drivers = np.zeros(M)
    carry_group, carry = None, None

    def flush() -> None:
        if carry is not None:
            assoc_sum[:] += carry
            n_drivers[:] += carry > 0

    for start in range(0, len(known_grouped), BLOCK):
        block = known_grouped[start : start + BLOCK]
        pos = [index.mod_pos[m] for m in block]
        co_w = (Xw[:, pos].T @ X).toarray() / index.W  # p(A, C)
        co_n = (X[:, pos].T @ X).toarray()  # distinct collections with both
        with np.errstate(divide="ignore", invalid="ignore"):
            lift = co_w / (p[pos][:, None] * p[None, :] ** cfg.CANDIDATE_FREQ_EXP)
        a = association(lift, cfg)
        a[co_n < cfg.MIN_COOCCURRENCE] = 0.0
        conf = co_n / (co_n + cfg.SHRINK_K)
        idf_col = np.array([idf[m] for m in block])[:, None]
        contrib = a * conf * idf_col
        groups = [group_of[m] for m in block]
        starts = [0] + [i for i in range(1, len(block)) if groups[i] != groups[i - 1]]
        seg_max = np.maximum.reduceat(contrib, starts, axis=0)
        for seg, st in enumerate(starts):
            if groups[st] == carry_group:
                carry = np.maximum(carry, seg_max[seg])
            else:
                flush()
                carry_group, carry = groups[st], seg_max[seg]
    flush()

    quality = quality_scores(index, cfg)
    recency = recency_scores(index, cfg)

    requiring = _mod_array(index, "requiring_count", 0.0)
    library = np.where(requiring >= cfg.LIBRARY_REQUIRING_THRESHOLD, cfg.LIBRARY_PENALTY, 1.0)

    missing_req = np.ones(M)
    for j, m in enumerate(index.mod_ids):
        reqs = index.requires.get(int(m))
        if reqs:
            k = len(reqs - installed)
            if k:
                missing_req[j] = max(
                    cfg.MISSING_REQUIREMENT_MIN_FACTOR, cfg.MISSING_REQUIREMENT_PENALTY**k
                )

    required_by_installed: dict[int, list[int]] = {}
    for m in installed:
        for r in index.requires.get(m, ()):
            if r not in installed:
                required_by_installed.setdefault(r, []).append(m)

    eligible = index.df >= cfg.MIN_CANDIDATE_COLLECTIONS
    for j, m in enumerate(index.mod_ids):
        m = int(m)
        info = index.mods.get(m) or {}
        if (
            m in installed
            or info.get("status") not in (None, "published")
            or (not include_adult and info.get("adult"))
            or (cfg.FILTER_REQUIRED_BY_INSTALLED and m in required_by_installed)
        ):
            eligible[j] = False

    final = assoc_sum * quality * recency * library * missing_req
    final = np.where(eligible, final, 0.0)
    return Scores(
        association=assoc_sum,
        n_drivers=n_drivers,
        quality=quality,
        recency=recency,
        library=library,
        missing_req=missing_req,
        final=final,
        eligible=eligible,
        required_by_installed=required_by_installed,
        idf=idf,
        known_installed=known,
        unknown_installed=unknown,
        group_of=group_of,
    )


def select(
    index: Index,
    scores: Scores,
    top: int,
    category: str | None = None,
    diversity: bool = True,
    cfg=config,
) -> list[int]:
    """Pick `top` mod positions: relevance order, a per-category cap, and MMR."""
    final = scores.final.copy()
    if category:
        needle = category.lower()
        for j, m in enumerate(index.mod_ids):
            cat = ((index.mods.get(int(m)) or {}).get("category") or "").lower()
            if needle not in cat:
                final[j] = 0.0
    order = [int(j) for j in np.argsort(-final, kind="stable") if final[j] > 0]
    if not diversity:
        return order[:top]

    pool = order[: max(cfg.MMR_POOL, top)]
    if not pool:
        return []
    rel = final[pool] / final[pool[0]]
    V = index.X[:, pool].multiply(index.weights[:, None]).tocsc()
    norms = np.sqrt(np.asarray(V.multiply(V).sum(axis=0)).ravel())
    norms[norms == 0] = 1.0
    sim = (V.T @ V).toarray() / np.outer(norms, norms)

    lam = cfg.MMR_LAMBDA
    chosen: list[int] = []
    per_cat: dict[str, int] = {}
    max_sim = np.zeros(len(pool))
    available = np.ones(len(pool), dtype=bool)
    cap = cfg.MAX_PER_CATEGORY if not category else top  # user asked for one category
    while len(chosen) < top and available.any():
        mmr = np.where(available, lam * rel - (1 - lam) * max_sim, -np.inf)
        i = int(np.argmax(mmr))
        available[i] = False
        cat = (index.mods.get(int(index.mod_ids[pool[i]])) or {}).get("category") or "?"
        if per_cat.get(cat, 0) >= cap:
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        chosen.append(i)
        max_sim = np.maximum(max_sim, sim[i])
    return [pool[i] for i in chosen]


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------


def drivers_for(
    index: Index,
    scores: Scores,
    mod_id: int,
    top_drivers: int | None = config.EXPLAIN_TOP_DRIVERS,
    per_driver: int | None = config.EXPLAIN_COLLECTIONS_PER_DRIVER,
    cfg=config,
) -> list[Driver]:
    """Which of the user's mods pushed `mod_id` up, and where they co-occur."""
    j = index.mod_pos.get(mod_id)
    if j is None or not scores.known_installed:
        return []
    cand_rows = set(index.collections_of(mod_id).tolist())
    p = index.wdf / index.W
    out = []
    for a in scores.known_installed:
        if a == mod_id:
            continue
        rows = [r for r in index.collections_of(a) if r in cand_rows]
        n = len(rows)
        if n < cfg.MIN_COOCCURRENCE:
            continue
        pac = index.weights[rows].sum() / index.W
        lift = pac / (p[index.mod_pos[a]] * p[j] ** cfg.CANDIDATE_FREQ_EXP)
        contrib = float(association(np.array([lift]), cfg)[0]) * n / (n + cfg.SHRINK_K)
        contrib *= scores.idf[a]
        if contrib <= 0:
            continue
        rows.sort(key=lambda r: -index.weights[r])
        shown = rows if per_driver is None else rows[:per_driver]
        cols = []
        for r in shown:
            c = index.collections[int(index.col_ids[r])]
            cols.append(SharedCollection(c["id"], c["name"], c["slug"], round(c["weight"], 2)))
        out.append(
            Driver(
                mod_id=a,
                name=(index.mods.get(a) or {}).get("name"),
                contribution=contrib,
                lift=float(lift),
                co_collections=n,
                idf=scores.idf[a],
                collections=cols,
            )
        )
    out.sort(key=lambda d: (-d.contribution, d.mod_id))
    # One driver per group (as in score()); list the rest as "similar".
    best: dict[int, Driver] = {}
    for d in out:
        g = scores.group_of.get(d.mod_id, d.mod_id)
        if g not in best:
            best[g] = d
        else:
            best[g].similar.append({"mod_id": d.mod_id, "name": d.name})
    out = list(best.values())
    return out if top_drivers is None else out[:top_drivers]


def missing_requirements(index: Index, mod_id: int, installed: set[int]) -> list[dict]:
    out = []
    for r in sorted(index.requires.get(mod_id, set()) - installed):
        info = index.mods.get(r) or {}
        out.append({"mod_id": r, "name": info.get("name")})
    return out


def build_recommendation(
    index: Index, scores: Scores, domain: str, j: int, installed: set[int], explain_all=False
) -> Recommendation:
    m = int(index.mod_ids[j])
    info = index.mods.get(m) or {}
    return Recommendation(
        mod_id=m,
        name=info.get("name"),
        author=info.get("author"),
        category=info.get("category"),
        url=nexus_url(domain, m),
        score=float(scores.final[j]),
        components={
            "association": float(scores.association[j]),
            "drivers": int(scores.n_drivers[j]),
            "quality": float(scores.quality[j]),
            "recency": float(scores.recency[j]),
            "library_penalty": float(scores.library[j]),
            "missing_req_penalty": float(scores.missing_req[j]),
        },
        collections=int(index.df[j]),
        drivers=drivers_for(
            index,
            scores,
            m,
            top_drivers=None if explain_all else config.EXPLAIN_TOP_DRIVERS,
            per_driver=None if explain_all else config.EXPLAIN_COLLECTIONS_PER_DRIVER,
        ),
        missing_requirements=missing_requirements(index, m, installed),
    )


def companions(index: Index, scores: Scores, domain: str) -> list[Companion]:
    """ "You have X but not its requirement Y." """
    out = []
    for r, by in scores.required_by_installed.items():
        info = index.mods.get(r) or {}
        out.append(
            Companion(
                mod_id=r,
                name=info.get("name"),
                url=nexus_url(domain, r),
                required_by=[
                    {"mod_id": b, "name": (index.mods.get(b) or {}).get("name")} for b in sorted(by)
                ],
            )
        )
    out.sort(key=lambda c: (-len(c.required_by), c.name or ""))
    return out


def recommend(
    index: Index,
    installed: Iterable[int],
    domain: str,
    top: int = 25,
    category: str | None = None,
    diversity: bool = True,
    include_adult: bool = config.INCLUDE_ADULT,
) -> tuple[list[Recommendation], list[Companion], Scores]:
    installed = set(installed)
    scores = score(index, installed, include_adult=include_adult)
    picks = select(index, scores, top, category=category, diversity=diversity)
    recs = [build_recommendation(index, scores, domain, j, installed) for j in picks]
    return recs, companions(index, scores, domain), scores
