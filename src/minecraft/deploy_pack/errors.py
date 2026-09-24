# src/minecraft/deploy_pack/errors.py

"""Exception hierarchy mapped to the exit codes in Project_Specs.md §2.4, §11.1.

The entrypoint catches DeployPackError and exits with .exit_code. Anything
that escapes those handlers is a bug and exits 1 via the generic
traceback path.

Daemon-loss classification (§2.4)
---------------------------------

DockerUnavailableError and DockerRuntimeError are the two halves of the
"daemon unreachable" story. The distinction is temporal, not semantic:

  * At preflight time, the daemon is expected to be reachable. If it
    isn't, that is a configuration failure: exit 3. docker_runtime
    raises DockerUnavailableError, which is a ConfigError subclass, and
    preflight lets it propagate.

  * After preflight has passed, the daemon is expected to stay up. If
    it drops mid-deployment, that is a runtime failure: exit 1. The
    hooks module catches DockerUnavailableError at the boundary and
    re-raises it as DockerRuntimeError.

The wrapping is deliberate: raising the same exception class in both
phases would force every caller to know which phase it's in. Two
classes, one per phase, keeps the exit-code mapping mechanical.
"""

from __future__ import annotations


class DeployPackError(Exception):
    """Base class. Subclasses carry the exit code the entrypoint should use."""

    exit_code: int = 1


class UsageError(DeployPackError):
    """CLI usage / argument error. Exit 2 (§2.4)."""

    exit_code = 2


class ConfigError(DeployPackError):
    """Configuration or preflight failure. Exit 3 (§2.4)."""

    exit_code = 3


class RuntimeDeployError(DeployPackError):
    """Runtime deployment failure after preflight. Exit 1 (§2.4)."""

    exit_code = 1


class DockerUnavailableError(ConfigError):
    """The Docker daemon cannot be reached.

    Raised by docker_runtime when the SDK cannot connect or a mid-flight
    API call fails with a connection error. Preflight lets this propagate
    (exit 3). Runtime code (hooks) catches it and re-raises as
    DockerRuntimeError so a daemon that drops mid-deployment becomes
    exit 1 (§2.4).
    """


class DockerRuntimeError(RuntimeDeployError):
    """The Docker daemon dropped mid-deployment. Exit 1 (§2.4).

    Wraps a DockerUnavailableError that occurred outside preflight.
    """
