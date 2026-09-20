# tests/unit/deploy_pack/test_deploy_safety.py

"""End-to-end safety tests for deploy_pack.

These tests build a real (temporary) repository layout, real sync and
instance directories, and run deploy_pack.main() with the real
non-mocked deploy pipeline. They exist to catch regressions where a
clean deploy would destroy files it should not, or where a dry-run
option would accidentally write to a live server.

The two failure modes these tests are designed to catch:

  1. Clean deploy deletes a protected file (tokens, world, sessions).
  2. Dry-run or --no-deploy touches a live instance directory.

Every test in this file would have caught the token-deletion incident
that motivated the protect-file mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.minecraft import deploy_pack


@pytest.fixture
def fake_repo(tmp_path):
    """Build a minimal but real repository layout under tmp_path.

    Returns a dict of the important paths so tests can assert on them.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    # Logs directory must exist for the default logging config.
    (repo / "logs").mkdir()

    # --- sync source ------------------------------------------------
    sync = repo / "sync"
    sync.mkdir()
    (sync / "kubejs").mkdir()
    (sync / "kubejs" / "test.js").write_text("// test kubejs file\n")
    (sync / "config").mkdir()
    (sync / "config" / "common.toml").write_text("managed = true\n")

    downloads = sync / "downloads"
    downloads.mkdir()
    index = downloads / ".index"
    index.mkdir()
    (index / "testmod.pw.toml").write_text('name = "Test Mod"\nfilename = "testmod.jar"\nside = "both"\n')

    # --- mods directory ---------------------------------------------
    mods = repo / "mods"
    mods.mkdir()

    # --- www --------------------------------------------------------
    www = repo / "www"
    www.mkdir()

    # --- survival instance ------------------------------------------
    survival = repo / "survival"
    survival.mkdir()
    (survival / "server.properties").write_text("motd=Custom MOTD\n")
    (survival / "config").mkdir()
    (survival / "config" / "common.toml").write_text("managed = OLD\n")
    (survival / "config" / "tokens.json").write_text('{"secret": "please-keep"}\n')
    (survival / "config" / "stale.txt").write_text("should be deleted\n")
    (survival / "kubejs").mkdir()
    (survival / "kubejs" / "old.js").write_text("// old, not in source\n")

    # --- protect file -----------------------------------------------
    protect = repo / ".deploy_protect"
    protect.write_text("# Safe-keep list\ntokens.json\nserver.properties\n")

    # --- exclude file -----------------------------------------------
    exclude = repo / ".rsync_exclude"
    exclude.write_text("# nothing excluded\n")

    # --- config.d ---------------------------------------------------
    config_d = repo / "config.d"
    config_d.mkdir()
    (config_d / "deploy_pack.toml").write_text(
        f'sync_root = "{sync}"\n'
        f'mods_dir = "{mods}"\n'
        f'www_dir = "{www}"\n'
        f'exclude_file = "{exclude}"\n'
        f'protect_file = "{protect}"\n'
        f'modpack_dir = "{downloads}"\n'
        f'output_filename = "minecraft_client_{{date}}.zip"\n'
        f"\n"
        f"[sync_mapping]\n"
        f'kubejs = "kubejs"\n'
        f'config = "config"\n'
        f"\n"
        f"[instances.survival]\n"
        f'path = "{survival}"\n'
    )

    return {
        "repo": repo,
        "sync": sync,
        "mods": mods,
        "www": www,
        "survival": survival,
        "protect": protect,
        "config_d": config_d,
    }


def _run_main(monkeypatch, fake_repo, *argv):
    """Run deploy_pack.main() with cwd set to the fake repo and given argv."""
    monkeypatch.chdir(fake_repo["repo"])
    monkeypatch.setattr(sys, "argv", ["deploy_pack.py", *argv])
    deploy_pack.main()


def _snapshot(root: Path) -> dict[str, bytes]:
    """Return {relative path: content bytes} for every file under root."""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestCleanDeployPreservesProtectedFiles:
    """A real clean deploy must never delete files listed in .deploy_protect."""

    def test_tokens_survive(self, fake_repo, monkeypatch):
        """The tokens file listed in .deploy_protect survives a clean deploy."""
        survival = fake_repo["survival"]
        tokens = survival / "config" / "tokens.json"
        original = tokens.read_text()

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert tokens.is_file(), "tokens.json was deleted by clean deploy"
        assert tokens.read_text() == original, "tokens.json content was modified"

    def test_server_properties_survive(self, fake_repo, monkeypatch):
        """server.properties listed in .deploy_protect survives a clean deploy."""
        survival = fake_repo["survival"]
        props = survival / "server.properties"
        original = props.read_text()

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert props.is_file()
        assert props.read_text() == original

    def test_stale_files_are_cleaned(self, fake_repo, monkeypatch):
        """Files not in source and not protected ARE deleted."""
        survival = fake_repo["survival"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert not (survival / "config" / "stale.txt").exists()
        assert not (survival / "kubejs" / "old.js").exists()

    def test_managed_files_are_overwritten(self, fake_repo, monkeypatch):
        """Files in both source and target take the source content."""
        survival = fake_repo["survival"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        common = survival / "config" / "common.toml"
        assert common.read_text() == "managed = true\n"

    def test_source_files_are_deployed(self, fake_repo, monkeypatch):
        """New files from source land in the target."""
        survival = fake_repo["survival"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert (survival / "kubejs" / "test.js").is_file()


class TestDryRunDoesNotTouchLiveServers:
    """--dry-run must never write to a live instance or the shared mods dir."""

    def test_instance_dir_byte_identical(self, fake_repo, monkeypatch):
        """After --dry-run, the survival instance is byte-identical."""
        survival = fake_repo["survival"]
        before = _snapshot(survival)

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        after = _snapshot(survival)
        assert before == after, "survival/ contents changed during --dry-run"

    def test_stale_files_survive_dry_run(self, fake_repo, monkeypatch):
        """The stale file that a real deploy would delete survives --dry-run."""
        survival = fake_repo["survival"]

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        assert (survival / "config" / "stale.txt").is_file()
        assert (survival / "kubejs" / "old.js").is_file()

    def test_protected_files_untouched(self, fake_repo, monkeypatch):
        """--dry-run doesn't even read-protect; it just doesn't touch anything."""
        survival = fake_repo["survival"]
        tokens = survival / "config" / "tokens.json"
        original = tokens.read_text()

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        assert tokens.read_text() == original

    def test_mods_dir_untouched(self, fake_repo, monkeypatch):
        """--dry-run does not deploy mods to the shared mods directory."""
        mods = fake_repo["mods"]
        assert list(mods.iterdir()) == []

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        assert list(mods.iterdir()) == []

    def test_output_goes_to_dry_run_subdir(self, fake_repo, monkeypatch):
        """--dry-run writes ZIP and changelog into www/dry_run/, not www/."""
        www = fake_repo["www"]

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        dry_run = www / "dry_run"
        assert dry_run.is_dir()
        assert any(dry_run.glob("minecraft_client_*.zip"))
        assert any(dry_run.glob("changelog_*.html"))

        # Nothing else landed in www/.
        siblings = sorted(p.name for p in www.iterdir())
        assert siblings == ["dry_run"], f"unexpected entries in www/: {siblings}"

    def test_dry_run_implies_no_deploy(self, fake_repo, monkeypatch):
        """--dry-run alone must not write to the shared mods directory."""
        mods = fake_repo["mods"]

        _run_main(monkeypatch, fake_repo, "--server", "--dry-run")

        assert list(mods.iterdir()) == []


class TestNoDeployDoesNotTouchLiveServers:
    """--no-deploy skips instance and mod writes but still builds the ZIP."""

    def test_instance_dir_byte_identical(self, fake_repo, monkeypatch):
        """--no-deploy leaves the instance directory untouched."""
        survival = fake_repo["survival"]
        before = _snapshot(survival)

        _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")

        assert _snapshot(survival) == before

    def test_mods_dir_untouched(self, fake_repo, monkeypatch):
        """--no-deploy leaves the shared mods directory untouched."""
        mods = fake_repo["mods"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")

        assert list(mods.iterdir()) == []

    def test_zip_is_built(self, fake_repo, monkeypatch):
        """--no-deploy still produces the client ZIP in www/."""
        www = fake_repo["www"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")

        assert any(www.glob("minecraft_client_*.zip"))

    def test_changelog_is_written(self, fake_repo, monkeypatch):
        """--no-deploy still writes the changelog HTML into www/."""
        www = fake_repo["www"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")

        assert any(www.glob("changelog_*.html"))


class TestNoZipSkipsZipAndPublish:
    """--no-zip still deploys the server side but produces no ZIP or HTML."""

    def test_server_side_runs(self, fake_repo, monkeypatch):
        """--no-zip still deploys the instance, cleaning and copying."""
        survival = fake_repo["survival"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert (survival / "config" / "common.toml").read_text() == "managed = true\n"
        assert (survival / "kubejs" / "test.js").is_file()

    def test_no_zip_or_html_in_www(self, fake_repo, monkeypatch):
        """--no-zip produces no ZIP and no changelog HTML."""
        www = fake_repo["www"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        assert not any(www.glob("minecraft_client_*.zip"))
        assert not any(www.glob("changelog_*.html"))


class TestNoNotifySkipsWebhook:
    """--no-notify produces the HTML but does not post to Discord."""

    def test_webhook_not_called(self, fake_repo, monkeypatch):
        """The webhook module is never touched when --no-notify is set."""
        with patch("src.minecraft.deploy_pack.notify.post_discord_webhook") as mock_webhook:
            _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")
        mock_webhook.assert_not_called()

    def test_html_still_written(self, fake_repo, monkeypatch):
        """The changelog HTML is produced regardless of --no-notify."""
        www = fake_repo["www"]

        _run_main(monkeypatch, fake_repo, "--server", "--no-deploy", "--no-notify")

        assert any(www.glob("changelog_*.html"))


class TestMissingProtectFileIsHardError:
    """A missing .deploy_protect must abort the run, not silently clean."""

    def test_missing_protect_file_aborts(self, fake_repo, monkeypatch):
        """Deleting .deploy_protect causes the deploy to fail loudly."""
        fake_repo["protect"].unlink()

        with pytest.raises((FileNotFoundError, SystemExit)):
            _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

    def test_instance_not_cleaned_when_protect_missing(self, fake_repo, monkeypatch):
        """Even if the run aborts, no files are deleted from the instance."""
        fake_repo["protect"].unlink()
        survival = fake_repo["survival"]
        before = _snapshot(survival)

        with pytest.raises((FileNotFoundError, SystemExit)):
            _run_main(monkeypatch, fake_repo, "--server", "--no-zip", "--no-notify")

        # Nothing was cleaned.
        assert _snapshot(survival) == before
