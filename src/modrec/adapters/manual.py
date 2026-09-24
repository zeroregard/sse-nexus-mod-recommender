"""Plain text list: one Nexus mod id or mod URL per line. `#` starts a comment.
The escape hatch for unsupported managers, and handy for testing."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import Game
from .base import AdapterError, InstalledMod, dedupe, parse_nexus_url

_ID_RE = re.compile(r"^\d+$")


def default_path() -> Path:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return xdg / "modrec" / "mods.txt"


def parse_lines(lines: list[str], game: Game) -> list[InstalledMod]:
    out = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if _ID_RE.match(line):
            out.append(InstalledMod(mod_id=int(line)))
            continue
        parsed = parse_nexus_url(line)
        if parsed is None:
            raise AdapterError(f"Can't parse mod list line: {raw.strip()!r}")
        domain, mod_id = parsed
        if domain == game.domain:
            out.append(InstalledMod(mod_id=mod_id))
    return dedupe(out)


class ManualAdapter:
    name = "manual"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_path()

    def detect(self, game: Game) -> bool:
        return self.path.is_file()

    def installed_mods(self, game: Game) -> list[InstalledMod]:
        if not self.path.is_file():
            raise AdapterError(f"Mod list not found: {self.path} (pass --path)")
        return parse_lines(self.path.read_text(encoding="utf-8").splitlines(), game)
