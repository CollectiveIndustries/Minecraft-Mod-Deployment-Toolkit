# tests/unit/deploy_pack/test_deploy_closure.py

"""Integration tests for the dependency closure inside the deploy pipeline.

The closure itself is unit-tested in tests/unit/common/test_deps.py.
This file verifies that deploy_pack actually consults the closure, that
the resulting client ZIP and server staging contain what the closure
added, and that the read-only --debug-deps path produces the expected
report without side effects.

The failure mode these tests guard against is the September 2026
crash: Prism index entries tagged ``side='server'`` for libraries that
client-side mods require at load time. The side filter alone produces
a client ZIP missing those libraries, and Forge refuses to start with
"mod X requires Y, Y is not installed". The closure pulls them back
in. Every assertion here corresponds to something a user would see
when the closure is broken:

  - a library missing from the ZIP
  - a library whose presence is not needed
  - a log line that stops reporting the force-include
  - a diagnostic that lies about the seed or closure sizes
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from src.minecraft import deploy_pack

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _write_jar(path: Path, mod_id: str, required_mods: list[str] | None = None) -> Path:
    """Write a minimal Forge jar with a META-INF/mods.toml.

    ``required_mods`` lists modIds this jar declares as mandatory
    BOTH-side dependencies in its manifest. This is the manifest Forge
    reads at load time; a jar that ships without one of these will fail
    the load check exactly as in the crash report.
    """
    lines = [
        'modLoader = "javafml"',
        'loaderVersion = "[47,)"',
        "",
        "[[mods]]",
        f'modId = "{mod_id}"',
    ]
    for dep in required_mods or []:
        lines += [
            "",
            f"[[dependencies.{mod_id}]]",
            f'modId = "{dep}"',
            "mandatory = true",
            'versionRange = "[0,)"',
            'ordering = "NONE"',
            'side = "BOTH"',
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", "\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def closure_repo(tmp_path):
    """Build a repository that exercises both closure edge sources.

    The pack contains eight mods in three categories:

      Libraries tagged ``side='server'`` but required by client mods:
        lib_a.jar       required by user_jar.jar via jar manifest (modId)
        lib_b.jar       required by user_idx.jar via index dep (project-id)

      A library tagged ``side='client'`` but required by a server mod:
        client_lib.jar  required by server_user.jar via jar manifest

      Mods whose metadata matches their actual need:
        user_jar.jar    side='both'   requires lib_a (jar manifest)
        user_idx.jar    side='both'   requires lib_b (index dep, addonId 555)
        common.jar      side='both'   no deps
        server_user.jar side='server' requires client_lib (jar manifest)
        orphan.jar      side='server' required by nothing

    After the side filter alone:

      client seed = {user_jar, user_idx, common, client_lib}                  = 4
      server seed = {lib_a, lib_b, orphan, user_jar, user_idx, common,
                     server_user}                                             = 7

    After the closure runs:

      client = seed + {lib_a, lib_b}                                          = 6
      server = seed + {client_lib}                                            = 8

    Any other count is a closure bug.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    # LoggingCore's default file handler writes into logs/. Pre-create it.
    (repo / "logs").mkdir()

    # --- sync source ------------------------------------------------
    sync = repo / "sync"
    sync.mkdir()
    downloads = sync / "downloads"
    downloads.mkdir()
    index = downloads / ".index"
    index.mkdir()

    # --- jars -------------------------------------------------------
    _write_jar(downloads / "lib_a.jar", "lib_a")
    _write_jar(downloads / "lib_b.jar", "lib_b")
    _write_jar(downloads / "orphan.jar", "orphan")
    _write_jar(downloads / "user_jar.jar", "user_jar", required_mods=["lib_a"])
    _write_jar(downloads / "user_idx.jar", "user_idx")
    _write_jar(downloads / "common.jar", "common")
    _write_jar(downloads / "client_lib.jar", "client_lib")
    _write_jar(
        downloads / "server_user.jar",
        "server_user",
        required_mods=["client_lib"],
    )

    # --- Prism index entries ----------------------------------------
    (index / "lib_a.pw.toml").write_text('filename = "lib_a.jar"\nname = "Lib A"\nside = "server"\n')
    (index / "lib_b.pw.toml").write_text('filename = "lib_b.jar"\nname = "Lib B"\nside = "server"\n\n[update.curseforge]\nproject-id = 555\nfile-id = 1\n')
    (index / "orphan.pw.toml").write_text('filename = "orphan.jar"\nname = "Orphan"\nside = "server"\n')
    (index / "user_jar.pw.toml").write_text('filename = "user_jar.jar"\nname = "User Jar"\nside = "both"\n')
    (index / "user_idx.pw.toml").write_text(
        'filename = "user_idx.jar"\nname = "User Idx"\nside = "both"\n\n[[x-prismlauncher-dependencies]]\naddonId = "555"\ntype = "REQUIRED"\n'
    )
    (index / "common.pw.toml").write_text('filename = "common.jar"\nname = "Common"\nside = "both"\n')
    (index / "client_lib.pw.toml").write_text('filename = "client_lib.jar"\nname = "Client Lib"\nside = "client"\n')
    (index / "server_user.pw.toml").write_text('filename = "server_user.jar"\nname = "Server User"\nside = "server"\n')

    # --- destination dirs ------------------------------------------
    mods = repo / "mods"
    mods.mkdir()
    www = repo / "www"
    www.mkdir()
    survival = repo / "survival"
    survival.mkdir()
    (survival / "server.properties").write_text("motd=test\n")
    multimc = repo / "multimc"
    multimc.mkdir()

    # --- deployment control files -----------------------------------
    exclude = repo / ".rsync_exclude"
    exclude.write_text("# nothing excluded\n")
    protect = repo / ".deploy_protect"
    protect.write_text("server.properties\n")

    # --- config -----------------------------------------------------
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
        f'multimc_base = "{multimc}"\n'
        f'instance_name = "TestInstance"\n'
        f"\n"
        f"[instances.survival]\n"
        f'path = "{survival}"\n'
    )

    return {
        "repo": repo,
        "sync": sync,
        "downloads": downloads,
        "index": index,
        "mods": mods,
        "www": www,
        "survival": survival,
        "multimc": multimc,
        "config_d": config_d,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, fake_repo, *argv):
    """Run deploy_pack.main() with cwd at the repo root and the given argv."""
    monkeypatch.chdir(fake_repo["repo"])
    monkeypatch.setattr(sys, "argv", ["deploy_pack.py", *argv])
    deploy_pack.main()


def _zip_names(zip_path: Path) -> set[str]:
    """Return member names in a ZIP, normalized to forward slashes."""
    with zipfile.ZipFile(zip_path) as zf:
        return {n.replace("\\", "/") for n in zf.namelist()}


def _find_client_zip(root: Path) -> Path:
    """Locate the single client ZIP under root, or fail loudly."""
    matches = list(root.glob("minecraft_client_*.zip"))
    assert len(matches) == 1, f"expected one client ZIP in {root}, found {matches}"
    return matches[0]


def _log_text(fake_repo) -> str:
    """Read the deploy log produced by the run under test."""
    log_path = fake_repo["repo"] / "logs" / "deploy_pack.log"
    assert log_path.is_file(), f"deploy log not written to {log_path}"
    return log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Client-side closure -> client ZIP
# ---------------------------------------------------------------------------


class TestClientClosureInZip:
    """The client ZIP must contain every library the closure pulled in."""

    def test_required_server_libs_are_in_zip(self, closure_repo, monkeypatch):
        """Both lib_a (jar-manifest dep) and lib_b (index-dep) land in the ZIP."""
        _run_main(monkeypatch, closure_repo, "--server", "--dry-run")

        names = _zip_names(_find_client_zip(closure_repo["www"] / "dry_run"))
        assert "mods/lib_a.jar" in names, "lib_a missing: jar-manifest closure failed"
        assert "mods/lib_b.jar" in names, "lib_b missing: index-dep closure failed"

    def test_seed_mods_are_still_in_zip(self, closure_repo, monkeypatch):
        """The side filter's own picks are not lost when the closure runs."""
        _run_main(monkeypatch, closure_repo, "--server", "--dry-run")

        names = _zip_names(_find_client_zip(closure_repo["www"] / "dry_run"))
        for expected in (
            "mods/user_jar.jar",
            "mods/user_idx.jar",
            "mods/common.jar",
            "mods/client_lib.jar",
        ):
            assert expected in names, f"{expected} disappeared from ZIP"

    def test_unneeded_server_mods_stay_out(self, closure_repo, monkeypatch):
        """The closure is additive, not a firehose: orphans stay out."""
        _run_main(monkeypatch, closure_repo, "--server", "--dry-run")

        names = _zip_names(_find_client_zip(closure_repo["www"] / "dry_run"))
        assert "mods/orphan.jar" not in names, "orphan mod was pulled in uninvited"
        assert "mods/server_user.jar" not in names, "server-only mod leaked into client ZIP"

    def test_closure_reported_in_log(self, closure_repo, monkeypatch):
        """The log names both force-included mods and the edge that pulled each in."""
        _run_main(monkeypatch, closure_repo, "--server", "--dry-run")

        log_text = _log_text(closure_repo)
        assert "Dependency closure for side 'client'" in log_text
        assert "force-included 2 mod(s)" in log_text
        assert "user_jar.jar -> lib_a.jar" in log_text
        assert "user_idx.jar -> lib_b.jar" in log_text
        assert "jar modId=lib_a" in log_text
        assert "index addonId=555" in log_text


# ---------------------------------------------------------------------------
# Server-side closure -> shared mods dir
# ---------------------------------------------------------------------------


class TestServerClosureInModsDir:
    """A server mod requiring a client-tagged library must get it deployed."""

    def test_required_client_lib_reaches_mods_dir(self, closure_repo, monkeypatch):
        """client_lib.jar is force-included on the server and deployed to mods/."""
        _run_main(monkeypatch, closure_repo, "--server", "--no-notify")

        deployed = {p.name for p in closure_repo["mods"].iterdir() if p.is_file()}
        assert "client_lib.jar" in deployed, "client_lib was required by server_user but did not reach mods/"

    def test_server_closure_is_logged(self, closure_repo, monkeypatch):
        """The server side reports its own force-include with the right reason.

        Uses --no-zip rather than --dry-run: --dry-run implies --no-deploy,
        which skips the server deploy path entirely and therefore never
        calls load_mod_list(..., 'server', ...). --no-zip runs the server
        path for real while still producing no client artifacts.
        """
        _run_main(monkeypatch, closure_repo, "--server", "--no-zip", "--no-notify")

        log_text = _log_text(closure_repo)
        assert "Dependency closure for side 'server'" in log_text
        assert "force-included 1 mod(s)" in log_text
        assert "server_user.jar -> client_lib.jar" in log_text
        assert "jar modId=client_lib" in log_text


# ---------------------------------------------------------------------------
# Client mode also gets the closure
# ---------------------------------------------------------------------------


class TestClientModeClosure:
    """--client mode deploys the closed set into the MultiMC instance."""

    def test_client_mode_deploys_closed_set(self, closure_repo, monkeypatch):
        """The MultiMC instance receives lib_a and lib_b, not orphan."""
        _run_main(monkeypatch, closure_repo, "--client")

        target = closure_repo["multimc"] / "TestInstance" / ".minecraft" / "mods"
        deployed = {p.name for p in target.iterdir() if p.is_file()}
        assert "lib_a.jar" in deployed
        assert "lib_b.jar" in deployed
        assert "user_jar.jar" in deployed
        assert "orphan.jar" not in deployed


# ---------------------------------------------------------------------------
# --debug-deps
# ---------------------------------------------------------------------------


class TestDebugDeps:
    """The read-only diagnostic prints closure results for both sides."""

    def test_header_and_both_sides(self, closure_repo, monkeypatch, capsys):
        """The output contains the header and both side sections."""
        _run_main(monkeypatch, closure_repo, "--debug-deps")
        out = capsys.readouterr().out
        assert "=== Dependency closure diagnostic ===" in out
        assert "--- Side: client ---" in out
        assert "--- Side: server ---" in out

    def test_client_side_counts(self, closure_repo, monkeypatch, capsys):
        """The client section reports 4 seed mods and 6 in closure, 2 forced."""
        _run_main(monkeypatch, closure_repo, "--debug-deps")
        out = capsys.readouterr().out

        client_block = out.split("--- Side: client ---", 1)[1].split("--- Side: server ---", 1)[0]
        assert "Seed (side filter): 4 mods" in client_block
        assert "Closure:            6 mods" in client_block
        assert "Force-included:     2" in client_block

    def test_server_side_counts(self, closure_repo, monkeypatch, capsys):
        """The server section reports 7 seed mods and 8 in closure, 1 forced."""
        _run_main(monkeypatch, closure_repo, "--debug-deps")
        out = capsys.readouterr().out

        server_block = out.split("--- Side: server ---", 1)[1]
        assert "Seed (side filter): 7 mods" in server_block
        assert "Closure:            8 mods" in server_block
        assert "Force-included:     1" in server_block

    def test_forced_entries_listed_with_reasons(self, closure_repo, monkeypatch, capsys):
        """Every forced entry names the dependent that pulled it in."""
        _run_main(monkeypatch, closure_repo, "--debug-deps")
        out = capsys.readouterr().out

        assert "lib_a.jar" in out
        assert "lib_b.jar" in out
        assert "client_lib.jar" in out
        assert "jar modId=lib_a" in out
        assert "index addonId=555" in out
        assert "jar modId=client_lib" in out

    def test_debug_deps_writes_nothing(self, closure_repo, monkeypatch):
        """--debug-deps must not create a ZIP, changelog, or touch mods/."""
        _run_main(monkeypatch, closure_repo, "--debug-deps")

        assert list(closure_repo["www"].iterdir()) == []
        assert list(closure_repo["mods"].iterdir()) == []
        assert not (closure_repo["survival"] / "config").exists()
