from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import config, db, output
from . import crawl as crawler
from .adapters import AdapterError, get_adapter
from .evaluate import evaluate as run_evaluate
from .evaluate import evaluate_collections
from .index import load_index
from .nexus import GraphQLClient, NexusError, api_key
from .scoring import build_recommendation, score
from .scoring import recommend as run_recommend

app = typer.Typer(
    help="Recommend Nexus mods you don't have, mined from collection co-occurrence.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
err = Console(stderr=True)


class Fmt(StrEnum):
    table = "table"
    json = "json"
    markdown = "markdown"


class State:
    game: config.Game
    db_path: Path


state = State()

GameOpt = Annotated[str, typer.Option("--game", "-g", help="Game alias or Nexus domain.")]


@app.callback()
def main(
    game: GameOpt = config.DEFAULT_GAME,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite index path (default: XDG data dir).")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set", help="Override a config constant, e.g. --set SHRINK_K=5 (repeatable)."
        ),
    ] = None,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        state.game = config.get_game(game)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    state.db_path = db_path or config.default_db_path(state.game)
    for assignment in overrides or []:
        try:
            name, value = config.apply_override(assignment)
        except (KeyError, ValueError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--set") from exc
        err.print(f"[dim]config: {name} = {value!r}[/dim]")


def _progress(msg: str) -> None:
    err.print(f"[dim]{msg}[/dim]")


def _conn():
    return db.connect(state.db_path)


@app.command()
def crawl(
    game: Annotated[
        str | None, typer.Option("--game", "-g", help="Same as the global --game.")
    ] = None,
    limit: Annotated[
        int, typer.Option(help="How many collections to index.")
    ] = config.DEFAULT_CRAWL_LIMIT,
    sort: Annotated[
        str, typer.Option(help="endorsements | downloads | rating | recent")
    ] = "endorsements",
    refresh: Annotated[bool, typer.Option(help="Re-fetch even if cached.")] = False,
    adult: Annotated[
        bool, typer.Option(help="Include adult-flagged collections.")
    ] = config.INCLUDE_ADULT,
    details: Annotated[bool, typer.Option(help="Fetch requirements for candidate mods.")] = True,
    v1_budget: Annotated[
        int,
        typer.Option(
            help="Spend up to N v1 REST calls (needs NEXUS_API_KEY) on unique-download counts."
        ),
    ] = 0,
) -> None:
    """Populate the local index. Resumable: cached collections at their current
    revision are skipped, and Ctrl-C is safe at any point."""
    if game:
        state.game = config.get_game(game)
        state.db_path = config.default_db_path(state.game)
    g = state.game
    conn = _conn()
    err.print(f"Index: {state.db_path}")
    try:
        with GraphQLClient() as gql:
            ids = crawler.list_collections(conn, gql, g, limit, sort, adult, _progress)
            fetched, failed = crawler.fetch_collections(conn, gql, g, ids, refresh, _progress)
            err.print(
                f"Collections: {fetched} fetched, {len(ids) - fetched - failed} cached/skipped, {failed} failed"
            )
            if details:
                cands = crawler.candidate_mod_ids(conn, config.MIN_CANDIDATE_COLLECTIONS)
                cands += db.installed_ids(conn)
                crawler.fetch_mod_details(conn, gql, g, cands, refresh, _progress)
                crawler.fetch_global_counts(
                    conn, gql, g, db.installed_ids(conn), refresh, _progress
                )
            err.print(f"GraphQL requests this run: {gql.requests}")
        if v1_budget > 0:
            if not api_key():
                err.print(f"[yellow]--v1-budget needs ${config.API_KEY_ENV}; skipping[/yellow]")
            else:
                cands = crawler.candidate_mod_ids(conn, config.MIN_CANDIDATE_COLLECTIONS)
                n = crawler.fetch_unique_downloads(conn, g, cands, v1_budget, _progress)
                err.print(f"v1 unique-download lookups: {n}")
    except KeyboardInterrupt:
        conn.rollback()
        err.print("[yellow]Interrupted; progress so far is saved. Re-run to resume.[/yellow]")
        raise typer.Exit(130)
    except NexusError as exc:
        err.print(f"[red]{exc}[/red] (progress so far is saved)")
        raise typer.Exit(1)
    _print_stats(conn)


@app.command()
def scan(
    adapter: Annotated[
        str | None, typer.Option(help="limo | mo2 | vortex | manual (default: auto-detect)")
    ] = None,
    path: Annotated[
        Path | None, typer.Option(help="Manager data path or manual list file.")
    ] = None,
    offline: Annotated[bool, typer.Option(help="Don't fetch metadata for your mods.")] = False,
    show: Annotated[bool, typer.Option(help="Print the detected mods.")] = False,
) -> None:
    """Detect installed mods and remember them for `recommend`."""
    g = state.game
    try:
        a = get_adapter(adapter, g, path)
        mods = a.installed_mods(g)
    except (AdapterError, NotImplementedError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    conn = _conn()
    db.replace_installed(conn, [(m.mod_id, m.name, a.name) for m in mods])
    conn.commit()
    err.print(f"{a.name}: {len(mods)} Nexus mods detected")
    if show:
        for m in mods:
            print(f"{m.mod_id}\t{m.name or ''}")
    if not offline and mods:
        try:
            with GraphQLClient() as gql:
                ids = [m.mod_id for m in mods]
                crawler.fetch_mod_details(conn, gql, g, ids, progress=_progress)
                crawler.fetch_global_counts(conn, gql, g, ids, progress=_progress)
        except (NexusError, KeyboardInterrupt) as exc:
            err.print(
                f"[yellow]Metadata fetch incomplete ({exc!r}); recommendations still work.[/yellow]"
            )
    known = conn.execute(
        "SELECT COUNT(DISTINCT i.mod_id) FROM installed i JOIN collection_mods c ON c.mod_id=i.mod_id"
    ).fetchone()[0]
    err.print(f"{known}/{len(mods)} of your mods appear in the crawled collections")


def _load(adult: bool):
    conn = _conn()
    installed = set(db.installed_ids(conn))
    if not installed:
        err.print("[red]No installed mods recorded. Run `modrec scan` first.[/red]")
        raise typer.Exit(1)
    try:
        index = load_index(conn, include_adult=adult)
    except RuntimeError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    return conn, index, installed


@app.command()
def recommend(
    top: Annotated[int, typer.Option(help="Number of recommendations.")] = 25,
    category: Annotated[
        str | None, typer.Option(help="Only this category (substring match).")
    ] = None,
    explain: Annotated[
        bool, typer.Option(help="Show component breakdown and collections.")
    ] = False,
    fmt: Annotated[Fmt, typer.Option("--format", "-f")] = Fmt.table,
    diversity: Annotated[bool, typer.Option(help="Per-category cap + MMR.")] = True,
    adult: Annotated[
        bool, typer.Option(help="Allow adult-flagged mods/collections.")
    ] = config.INCLUDE_ADULT,
) -> None:
    """Recommend mods you don't have. Runs entirely offline."""
    _, index, installed = _load(adult)
    recs, comps, scores = run_recommend(
        index, installed, state.game.domain, top, category, diversity, adult
    )
    extra = {
        "installed": len(installed),
        "installed_in_index": len(scores.known_installed),
        "collections": index.K,
        "collections_collapsed": sum(len(v) for v in index.duplicates.values()),
    }
    if fmt == Fmt.table:
        err.print(
            f"{extra['installed_in_index']}/{extra['installed']} of your mods are in the index "
            f"({index.K} collections after collapsing {extra['collections_collapsed']} near-duplicates)"
        )
    output.render(fmt.value, recs, comps, explain, extra)


@app.command()
def why(
    mod_id: Annotated[int, typer.Argument(help="Nexus mod id (or a mod URL's id).")],
    fmt: Annotated[Fmt, typer.Option("--format", "-f")] = Fmt.table,
    adult: Annotated[bool, typer.Option()] = config.INCLUDE_ADULT,
) -> None:
    """Full explanation for one mod: every driver and every shared collection."""
    _, index, installed = _load(adult)
    j = index.mod_pos.get(mod_id)
    if j is None:
        err.print(f"Mod {mod_id} isn't in any crawled collection; nothing to explain.")
        raise typer.Exit(1)
    scores = score(index, installed, include_adult=adult)
    notes = []
    if mod_id in installed:
        notes.append("You already have this mod.")
    if mod_id in scores.required_by_installed:
        names = [
            (index.mods.get(b) or {}).get("name") or str(b)
            for b in scores.required_by_installed[mod_id]
        ]
        notes.append(f"Filtered: it's a requirement of mods you have ({', '.join(names)}).")
    if index.df[j] < config.MIN_CANDIDATE_COLLECTIONS:
        notes.append(
            f"Filtered: in only {int(index.df[j])} collections (MIN_CANDIDATE_COLLECTIONS="
            f"{config.MIN_CANDIDATE_COLLECTIONS})."
        )
    status = (index.mods.get(mod_id) or {}).get("status")
    if status not in (None, "published"):
        notes.append(f"Filtered: mod status is {status!r}.")
    # Recompute the unfiltered score so the breakdown is meaningful either way.
    raw = (
        scores.association[j]
        * scores.quality[j]
        * scores.recency[j]
        * scores.library[j]
        * scores.missing_req[j]
    )
    rec = build_recommendation(index, scores, state.game.domain, j, installed, explain_all=True)
    rec.score = float(raw)
    rank = None
    if scores.final[j] > 0:
        rank = int((scores.final > scores.final[j]).sum()) + 1
    output.print_why(rec, rank, notes, fmt.value)


@app.command()
def evaluate(
    holdout: Annotated[int, typer.Option(help="Mods hidden per trial.")] = 10,
    k: Annotated[
        int, typer.Option("--k", help="Cut-off: a hit is a held-out mod in the top K.")
    ] = 25,
    trials: Annotated[int, typer.Option(help="Trials over your own mod list.")] = 5,
    collections: Annotated[
        int,
        typer.Option(
            help="Instead of your list, use N crawled collections as stand-in users "
            "(leave-one-collection-out). Needs no scan; best for tuning weights."
        ),
    ] = 0,
    seed: Annotated[int, typer.Option()] = 0,
    diversity: Annotated[bool, typer.Option()] = True,
    adult: Annotated[bool, typer.Option()] = config.INCLUDE_ADULT,
    fmt: Annotated[Fmt, typer.Option("--format", "-f")] = Fmt.table,
) -> None:
    """Hold-out validation of the scoring against simple baselines."""
    try:
        if collections:
            index = load_index(_conn(), include_adult=adult)
            report = evaluate_collections(
                index, collections, holdout, k, seed, diversity=diversity, include_adult=adult
            )
            title = f"Leave-one-collection-out: {report.trials} collections × {holdout} held out, top {k}"
        else:
            _, index, installed = _load(adult)
            report = run_evaluate(index, installed, holdout, k, trials, seed, diversity, adult)
            title = (
                f"Hold-out: {trials}×{holdout} of {report.eligible_pool} recoverable mods, top {k}"
            )
    except (ValueError, RuntimeError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if fmt == Fmt.json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    from rich.table import Table

    t = Table(title=title)
    for col in ("method", f"recall@{k}", f"niche recall@{k}", "MRR", "hits"):
        t.add_column(col, justify="right" if col != "method" else "left")
    for name, m in report.methods.items():
        t.add_row(
            name,
            f"{m['recall']:.1%}",
            f"{m['niche_recall']:.1%}",
            f"{m['mrr']:.3f}",
            str(m["hits"]),
        )
    output.console.print(t)
    output.console.print(
        f"[dim]niche = held-out mods in ≤{report.niche_threshold_df:.0f} indexed collections "
        f"({report.niche_total} of {report.held_out_total} held out)[/dim]"
    )


def _print_stats(conn) -> None:
    q = lambda s: conn.execute(s).fetchone()[0]
    err.print(
        f"Index: {q('SELECT COUNT(*) FROM collections WHERE fetched_revision IS NOT NULL')} collections, "
        f"{q('SELECT COUNT(DISTINCT mod_id) FROM collection_mods')} distinct mods, "
        f"{q('SELECT COUNT(*) FROM mods WHERE reqs_fetched_at IS NOT NULL')} with requirements; "
        f"{q('SELECT COUNT(*) FROM installed')} installed mods recorded"
    )


@app.command()
def stats() -> None:
    """Summarise what's in the local index."""
    conn = _conn()
    err.print(f"Index: {state.db_path}")
    _print_stats(conn)
    total = db.get_meta(conn, "game_collection_total")
    if total:
        err.print(f"Nexus lists {total} collections for {state.game.domain}")


if __name__ == "__main__":
    app()
