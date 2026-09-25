# src/minecraft/deploy_pack/preflight/__init__.py

"""Preflight validation and deployment planning (§4.1, §4.3)."""

from .actions import ACTION_ORDER, is_pack_action, resolve_action, resolve_paths_action, sticky_max
from .changes import compute_instance_server_change, compute_mods_change, compute_resource_pack_change
from .runner import run_preflight
from .states import check_rcon_available, classify_state
from .types import (
    InstanceServerChange,
    MemberPlan,
    ModsChange,
    PreflightError,
    PreflightFailure,
    PreflightPlan,
    ReasonEntry,
    ResourcePackChange,
    ScopeSet,
)

__all__ = [
    "ACTION_ORDER",
    "InstanceServerChange",
    "MemberPlan",
    "ModsChange",
    "PreflightError",
    "PreflightFailure",
    "PreflightPlan",
    "ReasonEntry",
    "ResourcePackChange",
    "ScopeSet",
    "check_rcon_available",
    "classify_state",
    "compute_instance_server_change",
    "compute_mods_change",
    "compute_resource_pack_change",
    "is_pack_action",
    "resolve_action",
    "resolve_paths_action",
    "run_preflight",
    "sticky_max",
]
