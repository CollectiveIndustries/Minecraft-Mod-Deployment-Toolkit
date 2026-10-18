# src/minecraft/modpack_upgrade/overrides.py
"""Transitional re-export of side overrides (Section 19.3 targets modpack_upgrade).

Once legacy mode is retired, this becomes the canonical implementation and
`deploy_pack.overrides` is deleted.
"""

from minecraft.deploy_pack.overrides import SideOverrides, apply_side_overrides, load_side_overrides, save_side_overrides

__all__ = ["SideOverrides", "apply_side_overrides", "load_side_overrides", "save_side_overrides"]
