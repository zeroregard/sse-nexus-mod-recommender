"""Mod Organizer 2 — stub.

Where the data lives (MO2 under Wine/Proton on Linux):
- Portable instance: `<MO2 dir>/ModOrganizer.ini`, mods in `<MO2 dir>/mods/`.
- Global instances: `<prefix>/drive_c/users/<user>/AppData/Local/ModOrganizer/<instance>/`.
- Each mod folder has `mods/<Mod Name>/meta.ini`:
      [General]
      gameName=SkyrimSE
      modid=12604
      ...
  `modid` is the Nexus mod id (0 / -1 when unknown). The active profile's
  `profiles/<profile>/modlist.txt` lists enabled mods as `+Mod Name`.

To implement: read every meta.ini with configparser, keep entries whose
gameName matches game.names (SkyrimSE) and whose modid > 0.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Game
from .base import InstalledMod


class MO2Adapter:
    name = "mo2"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def detect(self, game: Game) -> bool:
        return False

    def installed_mods(self, game: Game) -> list[InstalledMod]:
        raise NotImplementedError(
            "MO2 support isn't implemented yet (see modrec/adapters/mo2.py for where the "
            "data lives). Use --adapter manual with a list of mod ids for now."
        )
