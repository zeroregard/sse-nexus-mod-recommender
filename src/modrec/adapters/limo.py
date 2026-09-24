"""Limo (https://github.com/limo-app/limo).

Where the data lives:
- Limo's Qt settings (`QSettings("Limo")`) hold the list of staging dirs, as an
  INI array `[staging_directories]` with keys `1\\0=/path`, `2\\1=/path`, ...
    native:  $XDG_CONFIG_HOME/Limo.conf  (~/.config/Limo.conf)
    flatpak: ~/.var/app/io.github.limo_app.limo/config/Limo.conf
- Each staging dir (one per managed game) has `lmm_mods.json`:
    { "name": "...", "steam_app_id": 489830,
      "installed_mods": [ { "id": <limo id>, "name": ..., "remote_source": <url>,
                            "remote_type": 1 (nexus), "remote_mod_id": <nexus id>,
                            "remote_file_id": ... }, ... ],
      "deployers": [ { "profiles": [ { "loadorder": [ {"id", "enabled"} ] } ] } ] }
  `remote_mod_id` is the Nexus mod id — no filename guessing needed. Older
  files lack it; we fall back to parsing `remote_source`.
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path

from ..config import Game
from .base import AdapterError, InstalledMod, dedupe, parse_nexus_url

CONFIG_FILE = "lmm_mods.json"
REMOTE_NEXUS = 1


def settings_files() -> list[Path]:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [
        xdg / "Limo.conf",
        Path.home() / ".var/app/io.github.limo_app.limo/config/Limo.conf",
    ]


def staging_dirs_from_settings(path: Path) -> list[Path]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return []
    if not parser.has_section("staging_directories"):
        return []
    dirs = []
    for key, value in parser.items("staging_directories"):
        if key == "size":
            continue
        value = value.strip().strip('"')
        if value:
            dirs.append(Path(value))
    return dirs


def discover_staging_dirs() -> list[Path]:
    dirs: list[Path] = []
    for f in settings_files():
        if f.is_file():
            dirs.extend(staging_dirs_from_settings(f))
    return [d for d in dict.fromkeys(dirs) if (d / CONFIG_FILE).is_file()]


def _load(staging: Path) -> dict:
    try:
        return json.loads((staging / CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"Could not read {staging / CONFIG_FILE}: {exc}") from exc


def _manages(settings: dict, game: Game) -> bool:
    if game.steam_app_id is not None and settings.get("steam_app_id") == game.steam_app_id:
        return True
    name = str(settings.get("name", "")).lower()
    if any(n.lower() == name for n in game.names):
        return True
    for mod in settings.get("installed_mods") or []:
        parsed = parse_nexus_url(mod.get("remote_source", ""))
        if parsed and parsed[0] == game.domain:
            return True
    return False


def _enabled_ids(settings: dict) -> set[int]:
    enabled: set[int] = set()
    for depl in settings.get("deployers") or []:
        for prof in depl.get("profiles") or []:
            for entry in prof.get("loadorder") or []:
                if entry.get("enabled"):
                    enabled.add(entry.get("id"))
    return enabled


def mods_from_settings(settings: dict, game: Game) -> list[InstalledMod]:
    enabled = _enabled_ids(settings)
    out = []
    for mod in settings.get("installed_mods") or []:
        nexus_id = int(mod.get("remote_mod_id") or 0)
        parsed = parse_nexus_url(mod.get("remote_source", ""))
        if parsed and parsed[0] != game.domain:
            continue  # a mod from another game's page (rare, but possible)
        if nexus_id <= 0 and parsed:
            nexus_id = parsed[1]
        if nexus_id <= 0:
            continue  # local-only mod
        out.append(
            InstalledMod(
                mod_id=nexus_id,
                name=mod.get("name"),
                file_id=int(mod.get("remote_file_id") or 0) or None,
                enabled=mod.get("id") in enabled,
            )
        )
    return dedupe(out)


class LimoAdapter:
    name = "limo"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def _staging_dirs(self) -> list[Path]:
        if self.path is not None:
            p = Path(self.path).expanduser()
            if p.is_file():
                p = p.parent
            return [p]
        return discover_staging_dirs()

    def detect(self, game: Game) -> bool:
        for d in self._staging_dirs():
            try:
                if _manages(_load(d), game):
                    return True
            except AdapterError:
                continue
        return False

    def installed_mods(self, game: Game) -> list[InstalledMod]:
        dirs = self._staging_dirs()
        if not dirs:
            raise AdapterError(
                "No Limo staging directory found. Pass --path <staging dir> "
                f"(the folder containing {CONFIG_FILE})."
            )
        mods: list[InstalledMod] = []
        matched = False
        for d in dirs:
            settings = _load(d)
            if self.path is None and not _manages(settings, game):
                continue
            matched = True
            mods.extend(mods_from_settings(settings, game))
        if not matched:
            raise AdapterError(f"None of Limo's staging dirs manage {game.domain}: {dirs}")
        return dedupe(mods)
