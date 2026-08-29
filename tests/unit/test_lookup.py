# tests/unit/test_lookup.py
"""Unit tests for lookup.py - registry dump parser."""

from pathlib import Path
from unittest.mock import patch

import pytest

from minecraft import lookup


@pytest.fixture
def sample_log(tmp_path):
    """Create a temporary log file with a sample registry dump."""
    log_content = """
# Older dump (ignored)
[REGDUMP] REGISTRY DUMP START
[REGDUMP] ITEM|old:item|Old Item|oldmod|1|
[REGDUMP] REGISTRY DUMP END

# Newer dump (should be used)
[REGDUMP] REGISTRY DUMP START
[REGDUMP] ITEM|minecraft:apple|Apple|minecraft|64|minecraft:food|minecraft:berries
[REGDUMP] ITEM|minecraft:stick|Stick|minecraft|16|
[REGDUMP] BLOCK|minecraft:stone|Stone|minecraft||minecraft:stone|forge:stone
[REGDUMP] MACHINE|create:mechanical_press|Mechanical Press|create||
[REGDUMP] ENTITY|minecraft:zombie|Zombie|minecraft||
[REGDUMP] REGISTRY DUMP END
"""
    log_file = tmp_path / "kubejs" / "server.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(log_content)
    return log_file


def test_get_latest_dump():
    """Should extract only the newest dump."""
    text = """
[REGDUMP] REGISTRY DUMP START
old data
[REGDUMP] REGISTRY DUMP END
[REGDUMP] REGISTRY DUMP START
new data
[REGDUMP] REGISTRY DUMP END
"""
    result = lookup.get_latest_dump(text)
    assert "old data" not in result
    assert "new data" in result


def test_get_latest_dump_no_end():
    """Should return from last start to end of text if no end marker."""
    text = "[REGDUMP] REGISTRY DUMP START\nunfinished data"
    result = lookup.get_latest_dump(text)
    assert "unfinished data" in result


def test_get_latest_dump_no_start():
    """Should return entire text if no dump start found."""
    text = "no registry dump here"
    result = lookup.get_latest_dump(text)
    assert result == text


def test_parse_log(sample_log):
    """Should parse registry entries correctly."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
    assert len(registry["item"]) == 2
    assert len(registry["block"]) == 1
    assert len(registry["machine"]) == 1
    assert len(registry["entity"]) == 1

    apple = registry["item"][0]
    assert apple["id"] == "minecraft:apple"
    assert apple["name"] == "Apple"
    assert apple["mod"] == "minecraft"
    assert apple["stack"] == "64"
    assert apple["tags"] == ["minecraft:food", "minecraft:berries"]

    stick = registry["item"][1]
    assert stick["id"] == "minecraft:stick"
    assert stick["tags"] == []

    stone = registry["block"][0]
    assert stone["id"] == "minecraft:stone"
    assert stone["tags"] == ["minecraft:stone", "forge:stone"]

    press = registry["machine"][0]
    assert press["id"] == "create:mechanical_press"
    assert press["stack"] == ""


def test_parse_log_missing_file(tmp_path):
    """Should exit if log file missing."""
    with patch("minecraft.lookup.LOG", tmp_path / "missing.log"):
        with pytest.raises(SystemExit) as exc:
            lookup.parse_log()
        assert exc.value.code == 1


def test_parse_log_ignores_resourcekey():
    """Should skip entries with ResourceKey format."""
    log_file = Path("test.log")
    with patch("minecraft.lookup.LOG", log_file):
        log_file.write_text("""
[REGDUMP] REGISTRY DUMP START
[REGDUMP] ITEM|ResourceKey[minecraft:apple]|Apple|minecraft|
[REGDUMP] REGISTRY DUMP END
""")
        registry = lookup.parse_log()
        assert len(registry["item"]) == 0
        log_file.unlink()


def test_search(capsys, sample_log):
    """Should print matching entries."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        lookup.search(registry, "item", "apple")
    captured = capsys.readouterr()
    assert "ITEM SEARCH: apple" in captured.out
    assert "minecraft:apple" in captured.out
    assert "minecraft:stick" not in captured.out


def test_search_no_results(capsys, sample_log):
    """Should print '0 matches'."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        lookup.search(registry, "item", "unknown")
    captured = capsys.readouterr()
    assert "0 matches" in captured.out
    assert "ITEM SEARCH: unknown" in captured.out


def test_show_tags(capsys, sample_log):
    """Should print tags for an item."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        lookup.show_tags(registry, "minecraft:apple")
    captured = capsys.readouterr()
    assert "minecraft:apple — Apple" in captured.out
    assert "minecraft:food" in captured.out
    assert "minecraft:berries" in captured.out


def test_show_tags_no_tags(capsys, sample_log):
    """Should print 'No tags.' if none."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        lookup.show_tags(registry, "minecraft:stick")
    captured = capsys.readouterr()
    assert "minecraft:stick — Stick" in captured.out
    assert "No tags." in captured.out


def test_show_tags_not_found(capsys, sample_log):
    """Should exit if item not found."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        with pytest.raises(SystemExit) as exc:
            lookup.show_tags(registry, "unknown:item")
        assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Item not found: unknown:item" in captured.err


def test_show_mod(capsys, sample_log):
    """Should list all entries for a mod."""
    with patch("minecraft.lookup.LOG", sample_log):
        registry = lookup.parse_log()
        lookup.show_mod(registry, "minecraft")
    captured = capsys.readouterr()
    assert "MOD: minecraft" in captured.out
    assert "minecraft:apple" in captured.out
    assert "minecraft:stick" in captured.out
    assert "minecraft:stone" in captured.out
    assert "minecraft:zombie" in captured.out
    # Should NOT include create entries
    assert "create:mechanical_press" not in captured.out


def test_main_help(capsys):
    """main() should print usage for help."""
    with patch("sys.argv", ["lookup", "help"]):
        lookup.main()
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_main_search(capsys, sample_log):
    """main() should route search command."""
    with (
        patch("sys.argv", ["lookup", "item", "apple"]),
        patch("minecraft.lookup.LOG", sample_log),
    ):
        lookup.main()
    captured = capsys.readouterr()
    assert "ITEM SEARCH: apple" in captured.out


def test_main_tags(capsys, sample_log):
    """main() should route tags command."""
    with (
        patch("sys.argv", ["lookup", "tags", "minecraft:apple"]),
        patch("minecraft.lookup.LOG", sample_log),
    ):
        lookup.main()
    captured = capsys.readouterr()
    assert "minecraft:apple — Apple" in captured.out


def test_main_mod(capsys, sample_log):
    """main() should route mod command."""
    with (
        patch("sys.argv", ["lookup", "mod", "minecraft"]),
        patch("minecraft.lookup.LOG", sample_log),
    ):
        lookup.main()
    captured = capsys.readouterr()
    assert "MOD: minecraft" in captured.out


def test_main_unknown_command(capsys):
    """main() should print error for unknown command."""
    with patch("sys.argv", ["lookup", "unknown"]):
        with pytest.raises(SystemExit) as exc:
            lookup.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Unknown command: unknown" in captured.err


def test_main_missing_args(capsys):
    """main() should print usage if no args."""
    with patch("sys.argv", ["lookup"]):
        with pytest.raises(SystemExit) as exc:
            lookup.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_main_item_missing_query(capsys):
    """main() should exit if search query missing."""
    with patch("sys.argv", ["lookup", "item"]):
        with pytest.raises(SystemExit) as exc:
            lookup.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Usage: lookup item <search>" in captured.err


def test_main_tags_missing_args(capsys):
    """main() should exit if tags command missing args."""
    with patch("sys.argv", ["lookup", "tags"]):
        with pytest.raises(SystemExit) as exc:
            lookup.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Usage: lookup tags <item-id>" in captured.err


def test_main_mod_missing_args(capsys):
    """main() should exit if mod command missing args."""
    with patch("sys.argv", ["lookup", "mod"]):
        with pytest.raises(SystemExit) as exc:
            lookup.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Usage: lookup mod <mod-id>" in captured.err
