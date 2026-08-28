"""
update_server.py - Automated server update from a Prism instance.
Uses Prism .index directly; no manifest or mod_puller needed.
"""

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def rsync(src, dst, delete=False, exclude_patterns=None, identity=None):
    cmd = ["rsync", "-avz", "--progress"]
    if identity:
        cmd.extend(["-e", f"ssh -i {identity}"])
    if delete:
        cmd.append("--delete")
    if exclude_patterns:
        for pat in exclude_patterns:
            cmd.extend(["--exclude", pat])
    cmd.extend([str(src), dst])
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ssh_run(host, command, identity=None):
    cmd = ["ssh"]
    if identity:
        cmd.extend(["-i", identity])
    cmd.extend([host, command])
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="Update server from Prism instance")
    parser.add_argument(
        "prism_instance", type=Path, help="Path to Prism instance (local)"
    )
    parser.add_argument(
        "--remote",
        type=str,
        help="Remote server destination (e.g., user@host:/remote/base). If not set, assumes local paths.",
    )
    parser.add_argument(
        "--extract-remote",
        action="store_true",
        help="If set, extract the ZIP on the remote server (requires --remote).",
    )
    parser.add_argument(
        "--identity",
        type=str,
        help="Path to SSH private key for remote connections.",
    )
    args = parser.parse_args()

    prism_instance = args.prism_instance.expanduser().resolve()
    minecraft = prism_instance / ".minecraft"
    index_dir = prism_instance / ".index"

    if not minecraft.is_dir():
        print(f"Error: {prism_instance} does not appear to be a Prism instance.")
        sys.exit(1)

    if not index_dir.is_dir():
        print(f"Error: .index folder not found at {index_dir}.")
        sys.exit(1)

    # Load config
    config_dir = Path("config.d")
    deploy_toml = config_dir / "deploy_pack.toml"
    if not deploy_toml.exists():
        print("Error: config.d/deploy_pack.toml not found.")
        sys.exit(1)

    with open(deploy_toml, "rb") as f:
        cfg = tomllib.load(f)

    sync_root = Path(cfg.get("sync_root", "/home/minecraft/minecraft/sync"))
    live_server = Path(cfg.get("live_server", "/home/minecraft/minecraft/server"))
    www_dir = Path(cfg.get("www_dir", "/home/minecraft/minecraft/www"))
    modpack_dir = Path(cfg.get("modpack_dir", "./sync/downloads"))
    exclude_file = Path(
        cfg.get("exclude_file", "/home/minecraft/nfs/sync/.rsync_exclude")
    )

    # ------------------------------------------------------------------
    # Step 1: Copy mods from Prism to sync/downloads
    # ------------------------------------------------------------------
    print("=== Step 1: Copy mods from Prism to sync/downloads ===")
    modpack_dir.mkdir(parents=True, exist_ok=True)
    if (minecraft / "mods").exists():
        shutil.copytree(minecraft / "mods", modpack_dir, dirs_exist_ok=True)
        print(f"Mods copied to {modpack_dir}")
    else:
        print("Warning: No mods directory found in Prism instance.")

    # ------------------------------------------------------------------
    # Step 2: Copy configs, KubeJS, FTBQuests from Prism to sync/live
    # ------------------------------------------------------------------
    print("=== Step 2: Copy configs, KubeJS, FTBQuests from Prism ===")
    if (minecraft / "config").exists():
        shutil.copytree(minecraft / "config", sync_root / "config", dirs_exist_ok=True)
    if (minecraft / "scripts").exists():
        shutil.copytree(
            minecraft / "scripts", sync_root / "scripts", dirs_exist_ok=True
        )
    if (minecraft / "kubejs").exists():
        shutil.copytree(
            minecraft / "kubejs", live_server / "kubejs", dirs_exist_ok=True
        )
    if (minecraft / "config" / "ftbquests").exists():
        shutil.copytree(
            minecraft / "config" / "ftbquests",
            live_server / "config" / "ftbquests",
            dirs_exist_ok=True,
        )

    # ------------------------------------------------------------------
    # Step 3: Build server ZIP using Prism .index
    # ------------------------------------------------------------------
    print("=== Step 3: Build server ZIP from Prism index ===")
    subprocess.run(
        [
            "python",
            "-m",
            "minecraft.deploy_pack",
            "--server",
            "--prism-index",
            str(index_dir),
        ],
        check=True,
    )

    # Find the latest ZIP
    zip_files = list(www_dir.glob("minecraft_client_*.zip"))
    if not zip_files:
        print("Error: No ZIP found in www_dir.")
        sys.exit(1)
    local_zip = max(zip_files, key=lambda p: p.stat().st_mtime)
    print(f"Generated ZIP: {local_zip}")

    # ------------------------------------------------------------------
    # Step 4: Deploy to remote or local server
    # ------------------------------------------------------------------
    if args.remote:
        print(f"=== Step 4: Deploy to remote server {args.remote} ===")

        remote_base = args.remote.rstrip("/")
        remote_sync = f"{remote_base}/{sync_root.relative_to('/') if sync_root.is_absolute() else sync_root}"
        remote_live = f"{remote_base}/{live_server.relative_to('/') if live_server.is_absolute() else live_server}"
        remote_modpack = f"{remote_base}/{modpack_dir.relative_to('/') if modpack_dir.is_absolute() else modpack_dir}"
        remote_www = f"{remote_base}/{www_dir.relative_to('/') if www_dir.is_absolute() else www_dir}"

        excludes = []
        if exclude_file.exists():
            with open(exclude_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        excludes.append(line)

        identity = args.identity

        rsync(
            sync_root,
            remote_sync,
            delete=True,
            exclude_patterns=excludes,
            identity=identity,
        )
        rsync(
            live_server,
            remote_live,
            delete=True,
            exclude_patterns=excludes,
            identity=identity,
        )
        rsync(modpack_dir, remote_modpack, delete=True, identity=identity)
        rsync(local_zip, remote_www / local_zip.name, identity=identity)

        if args.extract_remote:
            host = args.remote.split(":")[0]
            remote_zip_path = f"{remote_www}/{local_zip.name}"
            print(f"Extracting {remote_zip_path} to {remote_live} on {host}...")
            ssh_run(
                host, f"unzip -o {remote_zip_path} -d {remote_live}", identity=identity
            )
            ssh_run(host, f"rm {remote_zip_path}", identity=identity)

        print(
            "=== Remote deployment complete. Restart your Docker container manually. ==="
        )
        print(
            f"  ssh {'-i ' + args.identity if args.identity else ''} {host} 'docker restart your-container-name'"
        )
    else:
        print("=== Step 4: Deploy to local server ===")
        print(f"Extracting {local_zip} to {live_server}...")
        subprocess.run(
            ["unzip", "-o", str(local_zip), "-d", str(live_server)], check=True
        )
        print("=== Server update complete! Restart your Docker container manually.")
        print("  docker restart your-container-name")


if __name__ == "__main__":
    main()
