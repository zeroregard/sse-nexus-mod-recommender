"""Hold-out validation: hide some of the user's mods, recommend from the rest,
count how many hidden mods come back in the top K. Compared against two
baselines so a number means something:

- popularity: rank candidates by (weighted) collection frequency
- raw co-occurrence: Σ over the user's mods of plain co-occurrence counts
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from . import config
from .index import Index
from .scoring import score, select


@dataclass
class MethodResult:
    hits: int = 0
    niche_hits: int = 0
    reciprocal_rank: float = 0.0

    def summary(self, total: int, niche_total: int, trials: int) -> dict:
        return {
            "recall": self.hits / total if total else 0.0,
            "niche_recall": self.niche_hits / niche_total if niche_total else 0.0,
            "mrr": self.reciprocal_rank / total if total else 0.0,
            "hits": self.hits,
        }


@dataclass
class EvalReport:
    k: int
    holdout: int
    trials: int
    eligible_pool: int
    held_out_total: int
    niche_total: int
    niche_threshold_df: float
    methods: dict[str, dict] = field(default_factory=dict)
    trial_hits: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def eligible_holdouts(index: Index, installed: set[int], cfg=config) -> list[int]:
    """Held-out mods must be recoverable in principle: indexed with enough
    support, published, and not a requirement of another installed mod (those
    land in the 'missing requirements' section, not the ranking)."""
    required = {r for m in installed for r in index.requires.get(m, ())}
    out = []
    for m in sorted(installed):
        j = index.mod_pos.get(m)
        if j is None or index.df[j] < cfg.MIN_CANDIDATE_COLLECTIONS:
            continue
        if (index.mods.get(m) or {}).get("status") not in (None, "published"):
            continue
        if m in required:
            continue
        out.append(m)
    return out


def _rank_hits(ranked: list[int], held: set[int], niche: set[int], res: MethodResult) -> list[int]:
    hits = []
    for rank, m in enumerate(ranked, 1):
        if m in held:
            hits.append(m)
            res.hits += 1
            res.niche_hits += m in niche
            res.reciprocal_rank += 1.0 / rank
    return hits


def _trial(
    index: Index,
    installed: set[int],
    held: set[int],
    niche: set[int],
    k: int,
    methods: dict[str, MethodResult],
    diversity: bool,
    include_adult: bool,
    cfg=config,
) -> list[int]:
    rest = installed - held
    s = score(index, rest, include_adult=include_adult, cfg=cfg)
    ranked = [int(index.mod_ids[j]) for j in select(index, s, k, diversity=diversity, cfg=cfg)]
    hits = _rank_hits(ranked, held, niche, methods["modrec"])

    elig = s.eligible
    pop = np.where(elig, index.wdf, -1.0)
    ranked_pop = [int(index.mod_ids[j]) for j in np.argsort(-pop)[:k] if pop[j] > 0]
    _rank_hits(ranked_pop, held, niche, methods["popularity"])

    rest_pos = [index.mod_pos[m] for m in rest if m in index.mod_pos]
    per_col = np.asarray(index.X[:, rest_pos].sum(axis=1)).ravel()
    raw = np.where(elig, index.X.T @ per_col, -1.0)
    ranked_raw = [int(index.mod_ids[j]) for j in np.argsort(-raw)[:k] if raw[j] > 0]
    _rank_hits(ranked_raw, held, niche, methods["raw_cooccurrence"])
    return hits


def _new_methods() -> dict[str, MethodResult]:
    return {
        "modrec": MethodResult(),
        "popularity": MethodResult(),
        "raw_cooccurrence": MethodResult(),
    }


def _niche(index: Index, pool: list[int]) -> tuple[set[int], float]:
    """'Niche' = at or below the median collection frequency of the pool."""
    df_pool = np.array([index.df[index.mod_pos[m]] for m in pool])
    threshold = float(np.median(df_pool))
    return {m for m, d in zip(pool, df_pool, strict=True) if d <= threshold}, threshold


def evaluate(
    index: Index,
    installed: set[int],
    holdout: int = 10,
    k: int = 25,
    trials: int = 5,
    seed: int = 0,
    diversity: bool = True,
    include_adult: bool = config.INCLUDE_ADULT,
) -> EvalReport:
    """Hold out mods from *your* list."""
    pool = eligible_holdouts(index, installed)
    if len(pool) < holdout + 1:
        raise ValueError(
            f"Only {len(pool)} of your mods are recoverable from the index; "
            f"need more than --holdout {holdout}. Crawl more collections."
        )
    niche, threshold = _niche(index, pool)
    rng = np.random.default_rng(seed)
    methods = _new_methods()
    report = EvalReport(k, holdout, trials, len(pool), 0, 0, threshold)
    for _ in range(trials):
        held = {int(m) for m in rng.choice(pool, size=holdout, replace=False)}
        report.held_out_total += len(held)
        report.niche_total += len(held & niche)
        report.trial_hits.append(
            _trial(index, installed, held, niche, k, methods, diversity, include_adult)
        )
    report.methods = {
        name: r.summary(report.held_out_total, report.niche_total, trials)
        for name, r in methods.items()
    }
    return report


def evaluate_collections(
    index: Index,
    probes: int = 30,
    holdout: int = 10,
    k: int = 25,
    seed: int = 0,
    min_size: int = 40,
    max_size: int = 400,
    diversity: bool = True,
    include_adult: bool = config.INCLUDE_ADULT,
) -> EvalReport:
    """Leave-one-collection-out: treat a real collection as a user's mod list,
    remove it (and anything near-identical to it) from the index, hide some of
    its mods, and see whether they come back. Needs no personal data, so it's
    the benchmark to tune weights against. Uses local idf only, since global
    counts are fetched just for the scanned user's mods."""
    cfg = config.overridden(IDF_USE_GLOBAL=False)
    sizes = np.asarray(index.X.sum(axis=1)).ravel()
    candidates = [r for r in range(index.K) if min_size <= sizes[r] <= max_size]
    rng = np.random.default_rng(seed)
    chosen = rng.choice(candidates, size=min(probes, len(candidates)), replace=False)
    Xb = index.X.tocsr()
    methods = _new_methods()
    report = EvalReport(k, holdout, 0, 0, 0, 0, 0.0)
    thresholds = []
    for r in chosen:
        row = set(Xb.getrow(int(r)).indices.tolist())
        # Drop the probe and any collection sharing >50% of it (forks under the
        # dedupe threshold would otherwise leak the answer).
        overlap = np.asarray((Xb @ Xb.getrow(int(r)).T).todense()).ravel()
        leak = [i for i in range(index.K) if overlap[i] / max(len(row), 1) > 0.5]
        sub = index.without_rows(leak)
        installed = {int(index.mod_ids[j]) for j in row}
        pool = eligible_holdouts(sub, installed, cfg)
        if len(pool) < holdout + 5:
            continue
        niche, threshold = _niche(sub, pool)
        thresholds.append(threshold)
        held = {int(m) for m in rng.choice(pool, size=holdout, replace=False)}
        report.trials += 1
        report.eligible_pool += len(pool)
        report.held_out_total += len(held)
        report.niche_total += len(held & niche)
        report.trial_hits.append(
            _trial(sub, installed, held, niche, k, methods, diversity, include_adult, cfg)
        )
    report.niche_threshold_df = float(np.median(thresholds)) if thresholds else 0.0
    report.methods = {
        name: r.summary(report.held_out_total, report.niche_total, report.trials)
        for name, r in methods.items()
    }
    return report
