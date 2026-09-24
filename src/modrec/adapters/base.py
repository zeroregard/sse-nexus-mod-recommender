from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Game


@dataclass(frozen=True)
class InstalledMod:
    mod_id: int
    name: str | None = None
    file_id: int | None = None
    enabled: bool | None = None


class AdapterError(RuntimeError):
    pass


@runtime_checkable
class ModManagerAdapter(Protocol):
    """Reads a mod manager's on-disk state. `path` overrides auto-discovery
    (a staging dir, an MO2 instance dir, a text file — whatever is natural)."""

    name: str

    def __init__(self, path: Path | None = None) -> None: ...

    def detect(self, game: Game) -> bool:
        """Cheap check: does this manager appear to manage `game` on this machine?"""
        ...

    def installed_mods(self, game: Game) -> list[InstalledMod]:
        """Installed mods that came from Nexus, identified by Nexus mod id."""
        ...


_URL_RE = re.compile(r"nexusmods\.com/(?P<domain>[\w-]+)/mods/(?P<id>\d+)", re.IGNORECASE)


def parse_nexus_url(url: str) -> tuple[str, int] | None:
    m = _URL_RE.search(url or "")
    return (m["domain"].lower(), int(m["id"])) if m else None


def dedupe(mods: list[InstalledMod]) -> list[InstalledMod]:
    """One entry per Nexus mod (a mod may be installed as several files)."""
    seen: dict[int, InstalledMod] = {}
    for m in mods:
        if m.mod_id > 0 and m.mod_id not in seen:
            seen[m.mod_id] = m
    return sorted(seen.values(), key=lambda m: m.mod_id)
