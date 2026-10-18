# tests/modpack_upgrade/test_side_determination.py

"""Side determination chain tests (Side_Determination Section 1-Section 4)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from minecraft.modpack_upgrade.side_determination import (
    SideResolution,
    resolve_final_side,
)


def _fabric_jar(tmp_path: Path, environment: object | None = None) -> Path:
    """Create a jar with a fabric.mod.json (environment may be absent)."""
    jar = tmp_path / "mod.jar"
    body: dict = {"id": "test", "version": "1.0"}
    if environment is not None:
        body["environment"] = environment
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps(body))
    return jar


def _forge_jar(tmp_path: Path, client_side_only: bool | None = None) -> Path:
    """Create a jar with a mods.toml (clientSideOnly may be absent)."""
    jar = tmp_path / "mod.jar"
    lines = ["[[mods]]", 'modId = "test"']
    if client_side_only is not None:
        lines.append(f"clientSideOnly = {str(client_side_only).lower()}")
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml", "\n".join(lines))
    return jar


def _empty_jar(tmp_path: Path) -> Path:
    """Create a jar with no mod manifests."""
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("readme.txt", "nothing")
    return jar


def _resolve(
    *,
    jar_path: Path | None = None,
    loader: str = "forge",
    side_override_result: str | None = None,
    provider_side: str | None = None,
    side_declared: str = "unknown",
) -> SideResolution:
    """Convenience wrapper."""
    return resolve_final_side(
        filename="mod.jar",
        project_id="mod",
        jar_path=jar_path,
        loader=loader,
        side_override_result=side_override_result,
        provider_side=provider_side,
        side_declared=side_declared,
    )


# ---------------------------------------------------------------------------
# Override wins
# ---------------------------------------------------------------------------


def test_override_wins_over_jar(tmp_path: Path) -> None:
    """An override beats jar metadata."""
    jar = _fabric_jar(tmp_path, "client")
    result = _resolve(jar_path=jar, loader="fabric", side_override_result="server")
    assert result.side == "server"
    assert result.source == "override"


def test_override_skipped_returns_skipped(tmp_path: Path) -> None:
    """An override of 'skipped' is honoured."""
    jar = _fabric_jar(tmp_path, "client")
    result = _resolve(jar_path=jar, loader="fabric", side_override_result="skipped")
    assert result.side == "skipped"
    assert result.source == "override"


# ---------------------------------------------------------------------------
# Fabric / Quilt jar metadata (Section 3.1)
# ---------------------------------------------------------------------------


def test_fabric_environment_absent_returns_both(tmp_path: Path) -> None:
    """Absent environment -> both (format default)."""
    jar = _fabric_jar(tmp_path, None)
    result = _resolve(jar_path=jar, loader="fabric")
    assert result.side == "both"
    assert result.source == "jar"


def test_fabric_environment_star_returns_both(tmp_path: Path) -> None:
    """environment='*' -> both."""
    jar = _fabric_jar(tmp_path, "*")
    result = _resolve(jar_path=jar, loader="fabric")
    assert result.side == "both"
    assert result.source == "jar"


def test_fabric_environment_client_returns_client(tmp_path: Path) -> None:
    """environment='client' -> client."""
    jar = _fabric_jar(tmp_path, "client")
    result = _resolve(jar_path=jar, loader="fabric")
    assert result.side == "client"
    assert result.source == "jar"


def test_fabric_environment_server_returns_server(tmp_path: Path) -> None:
    """environment='server' -> server."""
    jar = _fabric_jar(tmp_path, "server")
    result = _resolve(jar_path=jar, loader="fabric")
    assert result.side == "server"
    assert result.source == "jar"


def test_fabric_environment_array_falls_through(tmp_path: Path) -> None:
    """Array form -> falls through to provider / pw_toml."""
    jar = _fabric_jar(tmp_path, ["client"])
    result = _resolve(jar_path=jar, loader="fabric", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"


def test_quilt_loader_reads_fabric_mod_json(tmp_path: Path) -> None:
    """Quilt uses fabric.mod.json identically."""
    jar = _fabric_jar(tmp_path, "client")
    result = _resolve(jar_path=jar, loader="quilt")
    assert result.side == "client"


# ---------------------------------------------------------------------------
# Forge / NeoForge jar metadata (Section 3.2)
# ---------------------------------------------------------------------------


def test_forge_client_side_only_true_returns_client(tmp_path: Path) -> None:
    """clientSideOnly=true -> client."""
    jar = _forge_jar(tmp_path, True)
    result = _resolve(jar_path=jar, loader="forge")
    assert result.side == "client"
    assert result.source == "jar"


def test_forge_client_side_only_false_falls_through(tmp_path: Path) -> None:
    """clientSideOnly=false -> falls through (ambiguous)."""
    jar = _forge_jar(tmp_path, False)
    result = _resolve(jar_path=jar, loader="forge", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"


def test_forge_client_side_only_absent_falls_through(tmp_path: Path) -> None:
    """Absent clientSideOnly -> falls through."""
    jar = _forge_jar(tmp_path, None)
    result = _resolve(jar_path=jar, loader="forge", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"


def test_neoforge_reads_mods_toml(tmp_path: Path) -> None:
    """NeoForge reads mods.toml identically."""
    jar = _forge_jar(tmp_path, True)
    result = _resolve(jar_path=jar, loader="neoforge")
    assert result.side == "client"


# ---------------------------------------------------------------------------
# Jar absent / unreadable
# ---------------------------------------------------------------------------


def test_jar_missing_falls_through(tmp_path: Path) -> None:
    """A missing jar falls through to provider."""
    result = _resolve(jar_path=tmp_path / "none.jar", loader="forge", provider_side="client")
    assert result.side == "client"
    assert result.source == "provider"


def test_jar_without_manifest_falls_through(tmp_path: Path) -> None:
    """A jar with no mod manifests falls through."""
    jar = _empty_jar(tmp_path)
    result = _resolve(jar_path=jar, loader="forge", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"


def test_jar_bad_zip_falls_through(tmp_path: Path) -> None:
    """A malformed zip falls through."""
    jar = tmp_path / "bad.jar"
    jar.write_bytes(b"not a zip")
    result = _resolve(jar_path=jar, loader="forge", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"


# ---------------------------------------------------------------------------
# Provider / .pw.toml / unknown
# ---------------------------------------------------------------------------


def test_provider_wins_when_no_jar_signal(tmp_path: Path) -> None:
    """No jar signal -> provider used."""
    jar = _empty_jar(tmp_path)
    result = _resolve(jar_path=jar, loader="forge", provider_side="server")
    assert result.side == "server"
    assert result.source == "provider"


def test_provider_unknown_falls_through_to_pw_toml(tmp_path: Path) -> None:
    """Provider returning 'unknown' -> .pw.toml."""
    jar = _empty_jar(tmp_path)
    result = _resolve(jar_path=jar, loader="forge", provider_side="unknown", side_declared="both")
    assert result.side == "both"
    assert result.source == "pw_toml"


def test_pw_toml_used_when_nothing_else(tmp_path: Path) -> None:
    """No jar signal, no provider -> .pw.toml."""
    result = _resolve(jar_path=None, loader="forge", provider_side=None, side_declared="client")
    assert result.side == "client"
    assert result.source == "pw_toml"


def test_all_unknown_returns_unknown() -> None:
    """Nothing resolves -> unknown (caller exits 3)."""
    result = _resolve(jar_path=None, loader="forge", provider_side=None, side_declared="unknown")
    assert result.side == "unknown"
    assert result.source == "unknown"


def test_pw_toml_malformed_falls_to_unknown() -> None:
    """A malformed side_declared is not usable -> unknown."""
    result = _resolve(jar_path=None, loader="forge", provider_side=None, side_declared="bogus")
    assert result.side == "unknown"
    assert result.source == "unknown"


# ---------------------------------------------------------------------------
# Unsupported loader
# ---------------------------------------------------------------------------


def test_unsupported_loader_falls_through(tmp_path: Path) -> None:
    """An unrecognised loader skips jar metadata."""
    jar = _fabric_jar(tmp_path, "client")
    result = _resolve(jar_path=jar, loader="exotic", provider_side="both")
    assert result.side == "both"
    assert result.source == "provider"
