from __future__ import annotations

from pathlib import Path

from ..config import Game
from .base import AdapterError, InstalledMod, ModManagerAdapter, parse_nexus_url
from .limo import LimoAdapter
from .manual import ManualAdapter
from .mo2 import MO2Adapter
from .vortex import VortexAdapter

# Detection order: first match wins.
ADAPTERS: dict[str, type] = {
    "limo": LimoAdapter,
    "mo2": MO2Adapter,
    "vortex": VortexAdapter,
    "manual": ManualAdapter,
}


def get_adapter(name: str | None, game: Game, path: Path | None = None) -> ModManagerAdapter:
    if name:
        try:
            return ADAPTERS[name.lower()](path)
        except KeyError:
            raise AdapterError(f"Unknown adapter {name!r}; choose from {', '.join(ADAPTERS)}")
    for cls in ADAPTERS.values():
        adapter = cls(path)
        if adapter.detect(game):
            return adapter
    raise AdapterError(
        "Couldn't detect a mod manager for this game. Pass --adapter (and --path), "
        "or put mod ids/URLs in ~/.config/modrec/mods.txt."
    )


__all__ = [
    "ADAPTERS",
    "AdapterError",
    "InstalledMod",
    "LimoAdapter",
    "MO2Adapter",
    "ManualAdapter",
    "ModManagerAdapter",
    "VortexAdapter",
    "get_adapter",
    "parse_nexus_url",
]
