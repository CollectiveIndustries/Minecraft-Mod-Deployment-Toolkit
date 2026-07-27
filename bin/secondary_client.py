#!/usr/bin/env python3
"""
secondary_client.py - Verify a secondary file system against a master server.

Usage:
    python secondary_client.py <secondary_root> <server_host> [--port PORT]

The client walks <secondary_root>, computes SHA‑256 hashes for every regular file,
and sends the mapping (relative path -> hash) to the master server.
It then receives a JSON report detailing missing, extra, and modified files,
and prints a human-readable summary.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path


def compute_sha256(file_path):
    """Compute SHA-256 hash of a file in chunks."""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
    except OSError as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return None
    return sha256.hexdigest()


def build_inventory(root_dir):
    """Walk root_dir and return dict of relative path -> sha256 for files."""
    inventory = {}
    root_path = Path(root_dir).resolve()
    for entry in root_path.rglob('*'):
        if entry.is_file():
            rel_path = str(entry.relative_to(root_path))
            print(f"Hashing: {rel_path}", file=sys.stderr)
            h = compute_sha256(entry)
            if h is not None:
                inventory[rel_path] = h
    return inventory


def send_inventory(host, port, inventory):
    """Connect to server, send inventory, and receive report."""
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect((host, port))

        # Serialize inventory to JSON
        data = json.dumps(inventory).encode('utf-8')
        # Send length (4 bytes) followed by data
        client_socket.sendall(len(data).to_bytes(4, 'big') + data)

        # Receive length of response
        raw_len = client_socket.recv(4)
        if not raw_len:
            raise ValueError("No response length received")
        length = int.from_bytes(raw_len, 'big')
        # Receive the response
        response_data = b''
        while len(response_data) < length:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            response_data += chunk
        if len(response_data) != length:
            raise ValueError("Incomplete response received")

        report = json.loads(response_data.decode('utf-8'))
        return report

    except Exception as e:
        print(f"Communication error: {e}", file=sys.stderr)
        return None
    finally:
        client_socket.close()


def print_report(report):
    """Print a human-readable summary of the verification report."""
    if report is None:
        print("No report received.", file=sys.stderr)
        return

    missing = report.get('missing', [])
    extra = report.get('extra', [])
    modified = report.get('modified', [])
    ok = report.get('ok', [])

    total = len(missing) + len(extra) + len(modified) + len(ok)
    print(f"\nVerification complete. {len(ok)} files ok, {len(missing)} missing, "
          f"{len(extra)} extra, {len(modified)} modified (out of {total} total files).")

    if missing:
        print("\n--- Missing files (in master, not in secondary) ---")
        for f in missing:
            print(f"  {f}")

    if extra:
        print("\n--- Extra files (in secondary, not in master) ---")
        for f in extra:
            print(f"  {f}")

    if modified:
        print("\n--- Modified files (hash mismatch) ---")
        for f in modified:
            print(f"  {f}")


def main():
    parser = argparse.ArgumentParser(
        description="Client for verifying a secondary file system against a master server"
    )
    parser.add_argument('secondary_root', help="Root directory of the secondary file system")
    parser.add_argument('server_host', help="Hostname or IP of the master server")
    parser.add_argument('--port', type=int, default=9999, help="Port of the master server (default: 9999)")
    args = parser.parse_args()

    if not os.path.isdir(args.secondary_root):
        print(f"Error: {args.secondary_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    print("Building secondary inventory...", file=sys.stderr)
    inventory = build_inventory(args.secondary_root)
    print(f"Built inventory: {len(inventory)} files", file=sys.stderr)

    print(f"Connecting to {args.server_host}:{args.port}...", file=sys.stderr)
    report = send_inventory(args.server_host, args.port, inventory)

    print_report(report)


if __name__ == '__main__':
    main()