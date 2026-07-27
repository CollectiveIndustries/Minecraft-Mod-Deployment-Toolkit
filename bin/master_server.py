#!/usr/bin/env python3
"""
master_server.py - Build an inventory of the master file system and serve it
                   for integrity checks against secondary systems.

Usage:
    python master_server.py <master_root> [--port PORT]

The server walks <master_root>, computes SHA-256 hashes for every regular file,
and keeps the mapping (relative path -> hash) in memory. It then listens on the
specified port (default 9999) for client connections.

For each client, it receives a JSON dictionary of the client's hashes,
compares it with the master inventory, and sends back a JSON report.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
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


def handle_client(conn, addr, inventory):
    """Handle a single client connection."""
    print(f"Connection from {addr}", file=sys.stderr)
    try:
        # Receive length of JSON data (4 bytes, big-endian)
        raw_len = conn.recv(4)
        if not raw_len:
            return
        length = int.from_bytes(raw_len, 'big')
        # Receive the JSON payload
        data = b''
        while len(data) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if len(data) != length:
            raise ValueError("Incomplete data received")

        client_hashes = json.loads(data.decode('utf-8'))

        # Compare client hashes with master inventory
        report = {
            'missing': [],          # files in master but not in client
            'extra': [],            # files in client but not in master
            'modified': [],         # files present in both but hashes differ
            'ok': []                # files identical (optional)
        }

        # Check files that exist in master
        for rel_path, master_hash in inventory.items():
            client_hash = client_hashes.get(rel_path)
            if client_hash is None:
                report['missing'].append(rel_path)
            elif client_hash != master_hash:
                report['modified'].append(rel_path)
            else:
                report['ok'].append(rel_path)

        # Check for extra files in client
        for rel_path in client_hashes:
            if rel_path not in inventory:
                report['extra'].append(rel_path)

        # Send report back
        response = json.dumps(report).encode('utf-8')
        conn.sendall(len(response).to_bytes(4, 'big') + response)

    except Exception as e:
        print(f"Error handling {addr}: {e}", file=sys.stderr)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Master inventory server for file system integrity checks"
    )
    parser.add_argument('master_root', help="Root directory of the master file system")
    parser.add_argument('--port', type=int, default=9999, help="Port to listen on (default: 9999)")
    args = parser.parse_args()

    if not os.path.isdir(args.master_root):
        print(f"Error: {args.master_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    print("Building master inventory...", file=sys.stderr)
    inventory = build_inventory(args.master_root)
    print(f"Inventory built: {len(inventory)} files", file=sys.stderr)

    # Start server
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('0.0.0.0', args.port))
    server_socket.listen(5)
    print(f"Listening on port {args.port}...", file=sys.stderr)

    try:
        while True:
            conn, addr = server_socket.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr, inventory))
            thread.start()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
    finally:
        server_socket.close()


if __name__ == '__main__':
    main()