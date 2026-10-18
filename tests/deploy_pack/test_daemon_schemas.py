# tests/deploy_pack/test_daemon_schemas.py

"""Daemon request schema tests (spec v1.3 Section 12.3).

Exercises every pydantic field validator in ``daemon/schemas.py``. The
plugin tests in ``test_daemon_plugin_http.py`` exercise these only
indirectly via happy-path HTTP requests; this file pins the rejection
branches so a validator regression fails loudly here rather than
producing a 500 instead of a 422 in production.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minecraft.deploy_pack.daemon.schemas import (
    DeployRequest,
    PublishRequest,
    VerifyRequest,
)

# ---------------------------------------------------------------------------
# _validate_release_id
# ---------------------------------------------------------------------------


def test_release_id_valid_basic():
    """A well-formed release ID is accepted."""
    req = VerifyRequest(release_id="v1.0.2")
    assert req.release_id == "v1.0.2"


def test_release_id_valid_with_suffix():
    """A release ID with an alphanumeric suffix is accepted."""
    req = VerifyRequest(release_id="v1.0.2-rc1")
    assert req.release_id == "v1.0.2-rc1"


def test_release_id_explicit_none_accepted():
    """Explicit ``None`` is accepted when ``release_pointer`` is set."""
    req = VerifyRequest(release_id=None, release_pointer="stable")
    assert req.release_id is None


def test_release_id_invalid_pattern_rejected():
    """A non-version string is rejected."""
    with pytest.raises(ValidationError, match="invalid release_id"):
        VerifyRequest(release_id="not-a-version")


def test_release_id_path_traversal_rejected():
    """Path traversal characters are rejected."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="../etc/passwd")


def test_release_id_with_slash_rejected():
    """A slash in ``release_id`` is rejected."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="v1.0.2/bogus")


# ---------------------------------------------------------------------------
# _validate_pointer
# ---------------------------------------------------------------------------


def test_pointer_explicit_none_accepted():
    """Explicit ``None`` is accepted when ``release_id`` is set."""
    req = VerifyRequest(release_id="v1.0.2", release_pointer=None)
    assert req.release_pointer is None


def test_pointer_stable_accepted():
    """The ``stable`` pointer is accepted."""
    req = VerifyRequest(release_pointer="stable")
    assert req.release_pointer == "stable"


def test_pointer_unknown_rejected():
    """An unknown pointer name is rejected."""
    with pytest.raises(ValidationError, match="invalid pointer"):
        VerifyRequest(release_pointer="bogus")


def test_pointer_latest_slip_rejected():
    """``latest-slip`` is DEV-local and not accepted by the prod daemon."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_pointer="latest-slip")


# ---------------------------------------------------------------------------
# _validate_commit
# ---------------------------------------------------------------------------


def test_commit_explicit_none_accepted():
    """Explicit ``None`` is accepted."""
    req = VerifyRequest(release_id="v1.0.2", expected_commit_sha=None)
    assert req.expected_commit_sha is None


def test_commit_sha1_accepted():
    """A 40-char SHA-1 hex string is accepted."""
    sha = "a" * 40
    req = VerifyRequest(release_id="v1.0.2", expected_commit_sha=sha)
    assert req.expected_commit_sha == sha


def test_commit_sha256_accepted():
    """A 64-char SHA-256 hex string is accepted."""
    sha = "b" * 64
    req = VerifyRequest(release_id="v1.0.2", expected_commit_sha=sha)
    assert req.expected_commit_sha == sha


def test_commit_invalid_format_rejected():
    """A malformed string is rejected with a descriptive message."""
    with pytest.raises(ValidationError, match="invalid expected_commit_sha"):
        VerifyRequest(release_id="v1.0.2", expected_commit_sha="not-a-hex-sha")


def test_commit_wrong_length_rejected():
    """A 39-char string is rejected (neither 40 nor 64)."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="v1.0.2", expected_commit_sha="a" * 39)


def test_commit_non_hex_rejected():
    """A 40-char string containing non-hex characters is rejected."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="v1.0.2", expected_commit_sha="g" * 40)


def test_commit_sha256_wrong_length_rejected():
    """A 63-char string is rejected (neither 40 nor 64)."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="v1.0.2", expected_commit_sha="a" * 63)


# ---------------------------------------------------------------------------
# _validate_slip_sha
# ---------------------------------------------------------------------------


def test_slip_sha_explicit_none_accepted():
    """Explicit ``None`` is accepted."""
    req = VerifyRequest(release_id="v1.0.2", expected_slip_sha256=None)
    assert req.expected_slip_sha256 is None


def test_slip_sha_valid_accepted():
    """A 64-char SHA-256 hex string is accepted."""
    sha = "c" * 64
    req = VerifyRequest(release_id="v1.0.2", expected_slip_sha256=sha)
    assert req.expected_slip_sha256 == sha


def test_slip_sha_invalid_rejected():
    """A malformed slip SHA is rejected with a descriptive message."""
    with pytest.raises(ValidationError, match="invalid expected_slip_sha256"):
        VerifyRequest(release_id="v1.0.2", expected_slip_sha256="bad")


def test_slip_sha_non_hex_rejected():
    """A 64-char string containing non-hex characters is rejected."""
    with pytest.raises(ValidationError):
        VerifyRequest(release_id="v1.0.2", expected_slip_sha256="z" * 64)


# ---------------------------------------------------------------------------
# _check_combination
# ---------------------------------------------------------------------------


def test_combination_neither_set_rejected():
    """A request with neither ``release_id`` nor ``release_pointer`` is rejected."""
    with pytest.raises(ValidationError, match="exactly one of"):
        VerifyRequest()


def test_combination_both_set_rejected():
    """A request with both fields set is rejected."""
    with pytest.raises(ValidationError, match="only one of"):
        VerifyRequest(release_id="v1.0.2", release_pointer="stable")


# ---------------------------------------------------------------------------
# Subclass inheritance
# ---------------------------------------------------------------------------


def test_publish_request_inherits_combination_check():
    """PublishRequest rejects a body with neither field."""
    with pytest.raises(ValidationError, match="exactly one of"):
        PublishRequest()


def test_publish_request_inherits_release_id_validation():
    """PublishRequest rejects a malformed release_id."""
    with pytest.raises(ValidationError, match="invalid release_id"):
        PublishRequest(release_id="bad")


def test_publish_request_happy_path():
    """A valid PublishRequest body is accepted."""
    req = PublishRequest(release_id="v1.0.2")
    assert req.release_id == "v1.0.2"


def test_deploy_request_inherits_combination_check():
    """DeployRequest rejects a body with neither field."""
    with pytest.raises(ValidationError, match="exactly one of"):
        DeployRequest()


def test_deploy_request_happy_path():
    """A valid DeployRequest body is accepted."""
    req = DeployRequest(release_id="v1.0.2")
    assert req.release_id == "v1.0.2"
