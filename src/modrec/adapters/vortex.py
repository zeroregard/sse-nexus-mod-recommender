"""Vortex — stub.

Where the data lives (Vortex under Wine on Linux):
- `<prefix>/drive_c/users/<user>/AppData/Roaming/Vortex/state.v2/` is a
  LevelDB database. Keys are `###`-joined state paths with JSON values, e.g.
      persistent###mods###skyrimse###<vortex mod id>
  whose value has `attributes.modId` (Nexus mod id), `attributes.fileId`,
  `attributes.downloadGame`, and `state` ("installed").
- Enabled state per profile: `persistent###profiles###<profile>###modState`.

To implement: open with `plyvel` (or dump via Vortex's own `--get` CLI),
iterate the `persistent###mods###skyrimse###` prefix, collect attributes.modId.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Game
from .base import InstalledMod


class VortexAdapter:
    name = "vortex"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def detect(self, game: Game) -> bool:
        return False

    def installed_mods(self, game: Game) -> list[InstalledMod]:
        raise NotImplementedError(
            "Vortex support isn't implemented yet (see modrec/adapters/vortex.py for where "
            "the data lives). Use --adapter manual with a list of mod ids for now."
        )
