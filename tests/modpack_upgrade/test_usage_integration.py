# tests/modpack_upgrade/test_usage_integration.py

"""Integration tests for usage cap enforcement through the HTTP transport
and the update / materialize commands.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from minecraft.modpack_upgrade.cli.commands import materialize as materialize_mod
from minecraft.modpack_upgrade.cli.commands import update as update_mod
from minecraft.modpack_upgrade.cli.errors import UpgradeCliError
from minecraft.modpack_upgrade.providers.http import RequestsTransport
from minecraft.modpack_upgrade.report import (
    Diagnostics,
    ReportInputs,
    ReportPolicy,
    ResolutionReport,
    ResolvedEntry,
    SeedEntry,
    TargetRuntime,
    write_report,
)
from minecraft.modpack_upgrade.state import (
    STATUS_RESOLVED,
    RunState,
    save_state,
)
from minecraft.modpack_upgrade.usage import (
    UsageCapExceeded,
    UsageConfig,
    UsageTracker,
)

# ---------------------------------------------------------------------------
# Fake responses and sessions
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal ``requests`` response stand-in."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"ok",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store the response fields."""
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}

    def iter_content(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """Yield the content as one chunk."""
        del chunk_size
        if self.content:
            yield self.content

    def close(self) -> None:
        """No-op close."""


class _FakeSession:
    """``requests.Session`` stand-in that pops one response per call."""

    def __init__(self, scripted: list[_FakeResponse]) -> None:
        """Store the scripted responses."""
        self._scripted = list(scripted)
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        """Pop and return the next response."""
        del kwargs
        self.calls.append(url)
        if not self._scripted:
            raise AssertionError(f"no scripted response for {url}")
        return self._scripted.pop(0)


def _tracker(tmp_path: Path, **overrides: object) -> UsageTracker:
    """Return a tracker for integration tests."""
    base: dict[str, object] = {
        "log_path": tmp_path / "usage.jsonl",
        "window_hours": 24,
        "max_api_requests": None,
        "max_downloaded_bytes": None,
    }
    base.update(overrides)
    return UsageTracker(UsageConfig(**base))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RequestsTransport + tracker
# ---------------------------------------------------------------------------


def test_transport_without_tracker_no_checks(tmp_path: Path) -> None:
    """No tracker means no checks and no recording."""
    session = _FakeSession([_FakeResponse(content=b"ok")])
    transport = RequestsTransport(session=session)
    result = transport.get("https://api.modrinth.com/v2/project/create")
    assert result.status_code == 200


def test_transport_with_tracker_records_api_call(tmp_path: Path) -> None:
    """An API URL is recorded as a single api call."""
    session = _FakeSession([_FakeResponse(content=b"ok")])
    tracker = _tracker(tmp_path, max_api_requests=10)
    transport = RequestsTransport(session=session, usage_tracker=tracker)
    transport.get("https://api.modrinth.com/v2/project/create")
    assert tracker.api_used() == 1
    assert tracker.bytes_used() == 0


def test_transport_with_tracker_records_download(tmp_path: Path) -> None:
    """A CDN URL is recorded as a download with its byte count."""
    payload = b"x" * 1024
    session = _FakeSession([_FakeResponse(content=payload)])
    tracker = _tracker(tmp_path, max_downloaded_bytes=100_000)
    transport = RequestsTransport(session=session, usage_tracker=tracker)
    transport.get("https://cdn.modrinth.com/data/abc/versions/1/mod.jar")
    assert tracker.bytes_used() == 1024
    assert tracker.api_used() == 0


def test_transport_raises_when_api_cap_reached(tmp_path: Path) -> None:
    """A cap-reached tracker raises UsageCapExceeded before the request."""
    session = _FakeSession([])
    tracker = _tracker(tmp_path, max_api_requests=0)
    transport = RequestsTransport(session=session, usage_tracker=tracker)
    with pytest.raises(UsageCapExceeded):
        transport.get("https://api.modrinth.com/v2/project/create")


def test_transport_retries_count_as_multiple_calls(tmp_path: Path) -> None:
    """A retried API call records every attempt against the cap."""
    session = _FakeSession([_FakeResponse(status_code=500), _FakeResponse(content=b"ok")])
    tracker = _tracker(tmp_path, max_api_requests=10)
    transport = RequestsTransport(session=session, usage_tracker=tracker)
    transport.get("https://api.modrinth.com/v2/project/create")
    assert tracker.api_used() == 2


def test_transport_cap_hit_on_retry_aborts(tmp_path: Path) -> None:
    """If the cap is hit between attempts, the retry aborts."""
    session = _FakeSession([_FakeResponse(status_code=500), _FakeResponse(content=b"ok")])
    tracker = _tracker(tmp_path, max_api_requests=1)
    transport = RequestsTransport(session=session, usage_tracker=tracker)
    with pytest.raises(UsageCapExceeded):
        transport.get("https://api.modrinth.com/v2/project/create")
    assert tracker.api_used() == 1


# ---------------------------------------------------------------------------
# update.run and materialize.run cap propagation
# ---------------------------------------------------------------------------


class _CapRaisingTransport:
    """A transport whose every call raises UsageCapExceeded."""

    def __init__(self, kind: str = "api_request", cap: int = 5000) -> None:
        """Store the cap identity for the exception message."""
        self._kind = kind
        self._cap = cap

    def get(self, url: str, **kwargs: object) -> object:
        """Raise on any call."""
        del kwargs
        raise UsageCapExceeded(self._kind, self._cap, self._cap)


def test_update_cap_exceeded_returns_exit_6(tmp_path: Path) -> None:
    """A cap-exceeded transport inside update.run produces exit 6."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_text("[budget]\nmax_api_requests = 1\n")
    (config_dir / "side_overrides.toml").write_text("")
    manifest = tmp_path / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "52.0.1"\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n'
    )
    downloads = tmp_path / "sync" / "downloads"
    index = downloads / ".index"
    index.mkdir(parents=True)
    (index / "mod.pw.toml").write_text('filename = "mod.jar"\nside = "both"\n[update.modrinth]\nmod-id = "mod"\nversion = "v1"\n')
    (downloads / "mod.jar").write_bytes(b"x")

    with pytest.raises(UpgradeCliError) as ei:
        update_mod.run(
            config_dir=config_dir,
            project_root=tmp_path,
            release_mode="upgrade",
            transport=_CapRaisingTransport(),
            run_id="run-1",
            state_dir=tmp_path / "state",
        )
    assert ei.value.exit_code == 6
    assert "budget exhausted" in str(ei.value)


def test_materialize_cap_exceeded_returns_exit_6(tmp_path: Path) -> None:
    """A cap-exceeded transport inside materialize.run produces exit 6."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / "upgrade.toml").write_text("[budget]\nmax_api_requests = 1\n")
    (config_dir / "deploy_pack.toml").write_text('[instances.survival]\ncontainer = "mc-survival"\n')
    (config_dir / "side_overrides.toml").write_text("")

    manifest = tmp_path / "sync" / "modpack.manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '[minecraft]\nversion = "1.21.1"\nloader = "forge"\nloader_version = "52.0.1"\n[policy]\nselection = "release_then_beta"\nno_version = "ask"\n'
    )

    seed = SeedEntry(
        filename="mod-v2.jar",
        provider="modrinth",
        jar_sha256="a" * 64,
        side_declared="both",
        side_effective="both",
        project_id="mod",
        current_version_id="v1",
    )
    entry = ResolvedEntry(
        filename="mod-v2.jar",
        provider="modrinth",
        is_in_house=False,
        is_dependency_only=False,
        side_provisional="both",
        side_source_provisional="override",
        project_id="mod",
        version_id="v2",
    )
    report = ResolutionReport(
        run_id="run-1",
        generated_at_utc="2026-01-01T00:00:00Z",
        status="complete",
        inputs=ReportInputs(
            manifest_sha256="1" * 64,
            resolution_inputs_sha256="2" * 64,
            side_overrides_sha256="3" * 64,
            release_mode="upgrade",
        ),
        target_runtime=TargetRuntime(
            minecraft_version="1.21.1",
            loader="forge",
            loader_version="52.0.1",
        ),
        policy=ReportPolicy(selection="release_then_beta", no_version="ask"),
        seed=(seed,),
        resolved=(entry,),
        lost=(),
        cascade=(),
        conflict=(),
        diagnostics=Diagnostics(),
    )
    state_dir = tmp_path / "state"
    state_run_dir = state_dir / "runs" / "run-1"
    state_run_dir.mkdir(parents=True)
    report_path = tmp_path / "www" / "runs" / "run-1" / "resolution-report.toml"
    report_path.parent.mkdir(parents=True)
    report_sha = write_report(report_path, report)

    state = RunState(
        run_id="run-1",
        status=STATUS_RESOLVED,
        release_mode="upgrade",
        paused_at_utc="2026-01-01T00:00:00Z",
        report_sha256=report_sha,
        resolution_inputs_sha256="2" * 64,
        manifest_path=str(manifest),
    )
    save_state(state_run_dir / "state.toml", state)

    # Short-circuit the integrity check so we get to the download phase.
    orig_check = materialize_mod.check_integrity
    materialize_mod.check_integrity = lambda state, ctx: []  # type: ignore[assignment]
    try:
        with pytest.raises(UpgradeCliError) as ei:
            materialize_mod.run(
                config_dir=config_dir,
                project_root=tmp_path,
                run_id="run-1",
                transport=_CapRaisingTransport(),
                state_dir=state_dir,
            )
        assert ei.value.exit_code == 6
        assert "budget exhausted" in str(ei.value)
    finally:
        materialize_mod.check_integrity = orig_check  # type: ignore[assignment]
