# tests/deploy_pack/test_main_helpers.py

"""Coverage for main.py's CLI helpers and preflight.states' edge cases."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from minecraft.deploy_pack import main as main_mod
from minecraft.deploy_pack.preflight import states as states_mod

# ---------------------------------------------------------------------------
# _parse_overrides
# ---------------------------------------------------------------------------


def test_parse_overrides_empty():
    """An empty leftover list produces an empty override dict."""
    assert main_mod._parse_overrides([]) == {}


def test_parse_overrides_equals_form():
    """--key=value tokens parse into snake_case kwargs."""
    assert main_mod._parse_overrides(["--sync-root=/srv", "--mods-dir=/m"]) == {
        "sync_root": "/srv",
        "mods_dir": "/m",
    }


def test_parse_overrides_space_form():
    """--key value token pairs parse into snake_case kwargs."""
    assert main_mod._parse_overrides(["--sync-root", "/srv", "--mods-dir", "/m"]) == {
        "sync_root": "/srv",
        "mods_dir": "/m",
    }


def test_parse_overrides_bare_flag_skipped():
    """A --flag at end-of-input with no value is ignored."""
    assert main_mod._parse_overrides(["--sync-root"]) == {}


def test_parse_overrides_flag_followed_by_flag():
    """--flag followed by another --flag does not consume the second as a value."""
    assert main_mod._parse_overrides(["--sync-root", "--mods-dir", "/m"]) == {
        "mods_dir": "/m",
    }


def test_parse_overrides_bare_token_skipped():
    """Non-flag tokens in the leftover list are ignored."""
    assert main_mod._parse_overrides(["orphan", "--sync-root", "/srv"]) == {
        "sync_root": "/srv",
    }


def test_parse_overrides_double_dash_skipped():
    """A bare '--' is not a valid override and is skipped."""
    assert main_mod._parse_overrides(["--"]) == {}


def test_parse_overrides_mixed_forms():
    """Both forms can appear in one call."""
    assert main_mod._parse_overrides(["--sync-root=/a", "--mods-dir", "/b"]) == {
        "sync_root": "/a",
        "mods_dir": "/b",
    }


# ---------------------------------------------------------------------------
# _parse_instances
# ---------------------------------------------------------------------------


def test_parse_instances_none():
    """Absent --instance yields None."""
    assert main_mod._parse_instances(None) is None


def test_parse_instances_single():
    """A single name becomes a one-element set."""
    assert main_mod._parse_instances(["mc-a"]) == {"mc-a"}


def test_parse_instances_comma_separated():
    """Comma-separated names split and strip."""
    assert main_mod._parse_instances(["a, b ,c"]) == {"a", "b", "c"}


def test_parse_instances_repeated_flags():
    """Repeated --instance values collapse into one set."""
    assert main_mod._parse_instances(["a", "b", "a"]) == {"a", "b"}


def test_parse_instances_blank_entries_dropped():
    """Empty tokens after splitting are discarded."""
    assert main_mod._parse_instances(["a,,b, "]) == {"a", "b"}


# ---------------------------------------------------------------------------
# _resolve_config_dir
# ---------------------------------------------------------------------------


def test_resolve_config_dir_default(monkeypatch, tmp_path):
    """No --config-dir falls back to ./config.d."""
    monkeypatch.chdir(tmp_path)
    assert main_mod._resolve_config_dir(None) == Path("config.d")


def test_resolve_config_dir_directory(tmp_path):
    """A directory argument is returned as-is."""
    d = tmp_path / "cfg"
    d.mkdir()
    assert main_mod._resolve_config_dir(str(d)) == d


def test_resolve_config_dir_file_becomes_parent(tmp_path):
    """A file argument is treated as pointing at its parent directory."""
    d = tmp_path / "cfg"
    d.mkdir()
    f = d / "deploy_pack.toml"
    f.write_text("")
    assert main_mod._resolve_config_dir(str(f)) == d


# ---------------------------------------------------------------------------
# preflight.states.classify_state edge cases
# ---------------------------------------------------------------------------


def _state(*, exists=True, status="running", health=None, running=True):
    return SimpleNamespace(exists=exists, status=status, health=health, running=running)


def test_classify_state_missing_container():
    """A container that does not exist is a §8.9 failure."""
    msg = states_mod.classify_state(_state(exists=False, running=False), "c")
    assert msg is not None
    assert "missing" in msg


def test_classify_state_running_without_health():
    """A running container without .State.Health is a §3.17 / §4.12 failure."""
    msg = states_mod.classify_state(_state(health=None), "c")
    assert msg is not None
    assert ".State.Health" in msg


def test_classify_state_running_healthy_returns_none():
    """A healthy running container passes."""
    assert states_mod.classify_state(_state(health="healthy"), "c") is None


def test_classify_state_stopped_states_return_none():
    """Exited / created / stopped are all non-fatal."""
    assert states_mod.classify_state(_state(status="exited", running=False), "c") is None
    assert states_mod.classify_state(_state(status="created", running=False), "c") is None
    assert states_mod.classify_state(_state(status="stopped", running=False), "c") is None


def test_classify_state_paused_fails():
    """Paused is fatal."""
    msg = states_mod.classify_state(_state(status="paused"), "c")
    assert msg is not None
    assert "paused" in msg


def test_classify_state_removing_fails():
    """Removing is fatal."""
    msg = states_mod.classify_state(_state(status="removing"), "c")
    assert msg is not None


def test_classify_state_dead_fails():
    """Dead is fatal."""
    msg = states_mod.classify_state(_state(status="dead"), "c")
    assert msg is not None


def test_classify_state_still_restarting_fails():
    """A restarting container after the bounded wait is fatal."""
    msg = states_mod.classify_state(_state(status="restarting"), "c")
    assert msg is not None
    assert "restarting" in msg


def test_classify_state_unknown_status_fails():
    """Any unexpected status is fatal with a descriptive message."""
    msg = states_mod.classify_state(_state(status="frobnicating"), "c")
    assert msg is not None
    assert "frobnicating" in msg
