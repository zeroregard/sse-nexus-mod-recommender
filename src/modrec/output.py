"""Render recommendations as a rich table, JSON, or Markdown."""

from __future__ import annotations

import json
from collections.abc import Iterable

from rich.console import Console
from rich.table import Table

from .scoring import Companion, Driver, Recommendation

console = Console()


def _components(r: Recommendation) -> str:
    c = r.components
    parts = [
        f"assoc {c['association']:.2f} ({c['drivers']} drivers)",
        f"quality ×{c['quality']:.2f}",
        f"recency ×{c['recency']:.2f}",
    ]
    if c["library_penalty"] != 1.0:
        parts.append(f"library ×{c['library_penalty']:.2f}")
    if c["missing_req_penalty"] != 1.0:
        parts.append(f"unmet reqs ×{c['missing_req_penalty']:.2f}")
    return ", ".join(parts)


def _driver_line(d: Driver, with_collections: bool = True) -> str:
    line = f"{d.name or d.mod_id} [{d.mod_id}] +{d.contribution:.2f} (lift {d.lift:.1f}, {d.co_collections} shared)"
    if d.similar:
        line += f" (+{len(d.similar)} similar mods of yours)"
    if with_collections and d.collections:
        line += " — in " + ", ".join(f'"{c.name}"' for c in d.collections)
    return line


# --- table -----------------------------------------------------------------


def table(recs: list[Recommendation], companions: list[Companion], explain: bool) -> None:
    t = Table(title="Recommendations", show_lines=explain, expand=True)
    t.add_column("#", justify="right", no_wrap=True)
    t.add_column("Mod", ratio=3)
    t.add_column("Category", ratio=1)
    t.add_column("Score", justify="right", no_wrap=True)
    t.add_column("Because you have", ratio=3)
    for i, r in enumerate(recs, 1):
        mod = f"[bold]{r.name}[/bold] by {r.author or '?'}\n[dim]{r.url}[/dim]"
        if explain:
            mod += f"\n[dim]{_components(r)}; in {r.collections} collections[/dim]"
            if r.missing_requirements:
                mod += (
                    "\n[yellow]needs: "
                    + ", ".join(f"{m['name'] or m['mod_id']}" for m in r.missing_requirements)
                    + "[/yellow]"
                )
        drivers = r.drivers if explain else r.drivers[:3]
        because = "\n".join(_driver_line(d, with_collections=explain) for d in drivers)
        t.add_row(str(i), mod, r.category or "", f"{r.score:.2f}", because)
    console.print(t)
    if companions:
        c = Table(title="You have X but not its listed requirement Y", expand=True)
        c.add_column("Missing (Y)", ratio=2)
        c.add_column("Required by your (X)", ratio=3)
        for comp in companions[:25]:
            c.add_row(
                f"{comp.name or comp.mod_id}\n[dim]{comp.url}[/dim]",
                ", ".join(f"{b['name'] or b['mod_id']}" for b in comp.required_by[:5])
                + (f" (+{len(comp.required_by) - 5} more)" if len(comp.required_by) > 5 else ""),
            )
        console.print(c)


# --- json ------------------------------------------------------------------


def as_json(
    recs: list[Recommendation], companions: list[Companion], extra: dict | None = None
) -> str:
    return json.dumps(
        {
            **(extra or {}),
            "recommendations": [r.to_dict() for r in recs],
            "missing_requirements": [c.to_dict() for c in companions],
        },
        indent=2,
    )


# --- markdown --------------------------------------------------------------


def _md_escape(s: str | None) -> str:
    return (s or "").replace("|", "\\|")


def as_markdown(recs: list[Recommendation], companions: list[Companion], explain: bool) -> str:
    lines = ["# Mod recommendations", ""]
    for i, r in enumerate(recs, 1):
        lines.append(f"## {i}. [{_md_escape(r.name)}]({r.url})")
        lines.append("")
        lines.append(
            f"*{_md_escape(r.category)}* · by {_md_escape(r.author)} · "
            f"score **{r.score:.2f}** · in {r.collections} collections"
        )
        lines.append("")
        lines.append(f"Components: {_components(r)}")
        lines.append("")
        if r.missing_requirements:
            lines.append(
                "Needs: "
                + ", ".join(
                    _md_escape(m["name"] or str(m["mod_id"])) for m in r.missing_requirements
                )
            )
            lines.append("")
        lines.append("Because you have:")
        lines.append("")
        for d in r.drivers if explain else r.drivers[:3]:
            lines.append(f"- {_md_escape(_driver_line(d))}")
        lines.append("")
    if companions:
        lines += ["# Missing requirements of mods you have", ""]
        lines += ["| Missing | Required by |", "|---|---|"]
        for c in companions:
            by = ", ".join(_md_escape(b["name"] or str(b["mod_id"])) for b in c.required_by)
            lines.append(f"| [{_md_escape(c.name or str(c.mod_id))}]({c.url}) | {by} |")
        lines.append("")
    return "\n".join(lines)


def render(fmt: str, recs, companions, explain: bool, extra: dict | None = None) -> None:
    if fmt == "json":
        print(as_json(recs, companions, extra))
    elif fmt == "markdown":
        print(as_markdown(recs, companions, explain))
    else:
        table(recs, companions, explain)


def print_why(rec: Recommendation, rank: int | None, notes: Iterable[str], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({"rank": rank, "notes": list(notes), **rec.to_dict()}, indent=2))
        return
    if fmt == "markdown":
        print(as_markdown([rec], [], explain=True))
        for n in notes:
            print(f"> {n}")
        return
    console.print(f"[bold]{rec.name}[/bold] by {rec.author or '?'} — {rec.category or '?'}")
    console.print(f"[dim]{rec.url}[/dim]")
    console.print(
        f"Score {rec.score:.3f}"
        + (f" (rank {rank})" if rank else " (not in the top results)")
        + f"; appears in {rec.collections} deduplicated collections"
    )
    console.print(f"Components: {_components(rec)}")
    for n in notes:
        console.print(f"[yellow]{n}[/yellow]")
    if rec.missing_requirements:
        console.print(
            "Needs mods you don't have: "
            + ", ".join(f"{m['name'] or '?'} [{m['mod_id']}]" for m in rec.missing_requirements)
        )
    if not rec.drivers:
        console.print("None of your mods co-occur with it often enough to count.")
        return
    t = Table(title="Driven by your mods", expand=True)
    t.add_column("Your mod", ratio=2)
    t.add_column("Contribution", justify="right")
    t.add_column("Lift", justify="right")
    t.add_column("idf", justify="right")
    t.add_column("Shared collections", ratio=4)
    for d in rec.drivers:
        t.add_row(
            f"{d.name or '?'} [{d.mod_id}]"
            + (f"\n[dim]+{len(d.similar)} similar mods of yours[/dim]" if d.similar else ""),
            f"{d.contribution:.3f}",
            f"{d.lift:.1f}",
            f"{d.idf:.2f}",
            f"{d.co_collections}: " + ", ".join(c.name for c in d.collections),
        )
    console.print(t)
