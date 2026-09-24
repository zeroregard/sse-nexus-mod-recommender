from __future__ import annotations

import json

import pytest

from modrec.adapters import (
    AdapterError,
    LimoAdapter,
    ManualAdapter,
    MO2Adapter,
    get_adapter,
    parse_nexus_url,
)
from modrec.adapters.limo import staging_dirs_from_settings
from modrec.config import get_game
from modrec.nexus import mod_uid

SSE = get_game("skyrimse")


def write_limo(tmp_path, installed, name="Skyrim Special Edition", steam=489830):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "lmm_mods.json").write_text(
        json.dumps(
            {
                "name": name,
                "steam_app_id": steam,
                "installed_mods": installed,
                "deployers": [{"profiles": [{"loadorder": [{"id": 0, "enabled": True}]}]}],
            }
        )
    )
    return staging


def test_limo_reads_remote_mod_id(tmp_path):
    staging = write_limo(
        tmp_path,
        [
            {"id": 0, "name": "SkyUI", "remote_mod_id": 12604, "remote_type": 1},
            {
                "id": 1,
                "name": "Old entry",
                "remote_source": "https://www.nexusmods.com/skyrimspecialedition/mods/266?tab=files",
            },
            {"id": 2, "name": "My local tweak", "remote_mod_id": -1, "remote_source": ""},
            {"id": 3, "name": "SkyUI dup file", "remote_mod_id": 12604},
        ],
    )
    mods = LimoAdapter(staging).installed_mods(SSE)
    assert [m.mod_id for m in mods] == [266, 12604]
    assert next(m for m in mods if m.mod_id == 12604).enabled is True


def test_limo_discovers_staging_dirs_from_qsettings(tmp_path, monkeypatch):
    staging = write_limo(tmp_path, [{"id": 0, "name": "X", "remote_mod_id": 5}])
    other = tmp_path / "other"
    other.mkdir()
    (other / "lmm_mods.json").write_text(
        json.dumps({"name": "Cyberpunk 2077", "installed_mods": []})
    )
    conf = tmp_path / "config"
    conf.mkdir()
    (conf / "Limo.conf").write_text(
        f"[General]\nfoo=1\n\n[staging_directories]\n1\\0={other}\n2\\1={staging}\nsize=2\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert staging_dirs_from_settings(conf / "Limo.conf") == [other, staging]
    adapter = LimoAdapter()
    assert adapter.detect(SSE)
    assert [m.mod_id for m in adapter.installed_mods(SSE)] == [5]
    assert isinstance(get_adapter(None, SSE), LimoAdapter)


def test_manual_list(tmp_path):
    f = tmp_path / "mods.txt"
    f.write_text(
        "# my mods\n12604\nhttps://www.nexusmods.com/skyrimspecialedition/mods/266\n"
        "https://www.nexusmods.com/skyrim/mods/1  # other game, ignored\n\n266\n"
    )
    assert [m.mod_id for m in ManualAdapter(f).installed_mods(SSE)] == [266, 12604]


def test_manual_rejects_garbage(tmp_path):
    f = tmp_path / "mods.txt"
    f.write_text("SkyUI\n")
    with pytest.raises(AdapterError):
        ManualAdapter(f).installed_mods(SSE)


def test_stubs_raise():
    with pytest.raises(NotImplementedError):
        MO2Adapter().installed_mods(SSE)


def test_parse_url_and_uid():
    assert parse_nexus_url("https://www.nexusmods.com/skyrimspecialedition/mods/12604") == (
        "skyrimspecialedition",
        12604,
    )
    assert parse_nexus_url("nope") is None
    assert mod_uid(1704, 266) == "7318624272650"  # observed from the live API
