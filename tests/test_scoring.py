from __future__ import annotations

import numpy as np

from modrec import db
from modrec.evaluate import evaluate
from modrec.index import load_index
from modrec.scoring import recommend, score

from .conftest import add_collection, add_mod

DOMAIN = "skyrimspecialedition"


def test_niche_beats_popular(world):
    index = load_index(world)
    recs, _, scores = recommend(index, {1, 10, 11}, DOMAIN, top=10)
    ids = [r.mod_id for r in recs]
    assert ids[0] == 12
    pos2 = index.mod_pos[2]
    assert scores.final[index.mod_pos[12]] > scores.final[pos2]


def test_explanation_names_drivers_and_collections(world):
    index = load_index(world)
    recs, _, _ = recommend(index, {1, 10, 11}, DOMAIN, top=1)
    top = recs[0]
    driver_ids = {d.mod_id for d in top.drivers}
    # 10 and 11 share most of their collections: one driver group, listed once.
    covered = driver_ids | {s["mod_id"] for d in top.drivers for s in d.similar}
    assert {10, 11} <= covered
    assert len(driver_ids & {10, 11}) == 1
    assert 1 not in driver_ids  # in every collection: no lift, no idf
    assert all(d.collections for d in top.drivers)
    assert top.url.endswith("/mods/12")


def test_installed_never_recommended(world):
    index = load_index(world)
    recs, _, _ = recommend(index, {1, 10, 11}, DOMAIN, top=50, diversity=False)
    assert not {1, 10, 11} & {r.mod_id for r in recs}


def test_dedupe_collapses_forks(conn):
    base = list(range(1, 21))
    add_collection(conn, 1, base, endorsements=5000)
    add_collection(conn, 2, base[:-1] + [99], endorsements=10)  # fork: Jaccard 19/21
    add_collection(conn, 3, list(range(50, 70)))
    for m in [*base, 99, *range(50, 70)]:
        add_mod(conn, m)
    conn.commit()
    index = load_index(conn)
    assert index.K == 2
    assert index.duplicates == {1: [2]}


def test_required_by_installed_is_filtered_to_companions(world):
    world.execute("INSERT INTO mod_requirements VALUES (10, 12, 'Mod 12', NULL)")
    world.commit()
    index = load_index(world)
    recs, comps, _ = recommend(index, {1, 10, 11}, DOMAIN, top=10)
    assert 12 not in {r.mod_id for r in recs}
    assert comps[0].mod_id == 12
    assert comps[0].required_by[0]["mod_id"] == 10


def test_quality_uses_rate_not_count(conn):
    for cid in range(1, 6):
        add_collection(conn, cid, [1, 2, 3])
    add_mod(conn, 1)
    add_mod(conn, 2, endorsements=300, downloads=3_000)  # 10% rate, tiny
    add_mod(conn, 3, endorsements=3_000, downloads=300_000)  # 1% rate, huge
    conn.commit()
    index = load_index(conn)
    s = score(index, {1})
    assert s.quality[index.mod_pos[2]] > s.quality[index.mod_pos[3]]


def test_category_cap(conn):
    for cid in range(1, 11):
        add_collection(conn, cid, [1, *range(10, 20)] if cid <= 5 else list(range(30, 40)))
    add_mod(conn, 1)
    for m in range(10, 20):
        add_mod(conn, m, category="Textures")
    for m in range(30, 40):
        add_mod(conn, m)
    conn.commit()
    index = load_index(conn)
    recs, _, _ = recommend(index, {1}, DOMAIN, top=10)
    cats = [r.category for r in recs]
    assert cats.count("Textures") <= 4


def test_global_idf_preferred(world):
    world.execute("UPDATE mods SET global_collection_count=4000 WHERE mod_id=10")
    db.set_meta(world, "game_collection_total", 4758)
    world.commit()
    index = load_index(world)
    s = score(index, {1, 10, 11})
    assert s.idf[10] < 0.2  # globally common, even though rare locally
    assert s.idf[11] > 1.0


def test_evaluate_runs_and_beats_nothing(world):
    # give the user a longer list so there's something to hold out
    installed = {1, 10, 11, 12, 2}
    index = load_index(world)
    report = evaluate(index, installed, holdout=1, k=5, trials=4, seed=1)
    assert set(report.methods) == {"modrec", "popularity", "raw_cooccurrence"}
    assert 0.0 <= report.methods["modrec"]["recall"] <= 1.0
    assert report.held_out_total == 4


def test_score_arrays_align(world):
    index = load_index(world)
    s = score(index, {1, 10})
    assert s.final.shape == index.mod_ids.shape
    assert np.all(s.final >= 0)


def test_mod_series_counts_once(conn):
    """40 mods that only ever appear together are one piece of evidence."""
    series = list(range(500, 540))
    for cid in (1, 2):
        add_collection(conn, cid, [*series, 90, *range(700 + cid * 20, 720 + cid * 20)])
    for cid in range(3, 9):
        add_collection(conn, cid, [60, 61, *range(700 + cid * 20, 720 + cid * 20)])
    add_collection(conn, 9, [90, 3000, 3001])
    add_collection(conn, 10, [61, 3002, 3003])
    for m in [*series, 60, 61, 90, 3000, 3001, 3002, 3003, *range(740, 900)]:
        add_mod(conn, m)
    conn.commit()
    index = load_index(conn)
    s = score(index, {*series, 60})
    assert len({s.group_of[m] for m in series}) == 1
    one = score(index, {series[0], 60})
    # The whole series contributes exactly what one of its mods would.
    j = index.mod_pos[90]
    assert np.isclose(s.association[j], one.association[j])
