#!/usr/bin/env python3

import re
import sys
from pathlib import Path

LOG = Path("server/logs/kubejs/server.log")


def usage():
    print("""Usage:
  lookup item <search>
  lookup block <search>
  lookup machine <search>
  lookup entity <search>
  lookup tags <item-id>
  lookup mod <mod-id>

Examples:
  lookup item paper
  lookup item sawdust
  lookup item steel
  lookup machine press
  lookup block copper
  lookup tags immersiveengineering:dust_wood
  lookup mod create
""")


def get_latest_dump(text):
    """Return only the newest complete registry dump."""

    starts = list(re.finditer(r"\[REGDUMP\]\s+REGISTRY DUMP START", text))

    if not starts:
        return text

    start = starts[-1].start()

    end = text.find("[REGDUMP] REGISTRY DUMP END", start)

    if end == -1:
        return text[start:]

    return text[start:end]


def parse_log():
    if not LOG.exists():
        print(f"ERROR: Cannot find {LOG}", file=sys.stderr)
        sys.exit(1)

    text = LOG.read_text(encoding="utf-8", errors="replace")

    text = get_latest_dump(text)

    registry = {
        "item": [],
        "block": [],
        "machine": [],
        "entity": [],
    }

    pattern = re.compile(
        r"\[REGDUMP\]\s+"
        r"(ITEM|BLOCK|MACHINE|ENTITY)"
        r"\|(.*)"
    )

    for line in text.splitlines():
        match = pattern.search(line)

        if not match:
            continue

        kind = match.group(1).lower()
        fields = match.group(2).split("|")

        if len(fields) < 3:
            continue

        identifier = fields[0].strip()
        name = fields[1].strip()
        mod = fields[2].strip()

        # Ignore the ResourceKey format from the earlier
        # broken registry dump implementation.
        if identifier.startswith("ResourceKey["):
            continue

        if kind in ("item", "block"):
            stack = fields[3].strip() if len(fields) >= 4 else ""

            tags = [x.strip() for x in fields[4:] if x.strip()]

        else:
            stack = ""

            tags = [x.strip() for x in fields[3:] if x.strip()]

        registry[kind].append(
            {
                "id": identifier,
                "name": name,
                "mod": mod,
                "stack": stack,
                "tags": tags,
            }
        )

    return registry


def print_entry(entry):

    print(entry["id"])
    print(f"  Name:  {entry['name']}")
    print(f"  Mod:   {entry['mod']}")

    if entry["stack"]:
        print(f"  Stack: {entry['stack']}")

    if entry["tags"]:
        print("  Tags:")

        for tag in entry["tags"]:
            print(f"    {tag}")

    print()


def search(registry, kind, query):

    query = query.lower()

    results = []

    for entry in registry[kind]:
        searchable = " ".join(
            [
                entry["id"],
                entry["name"],
                entry["mod"],
                *entry["tags"],
            ]
        ).lower()

        if query in searchable:
            results.append(entry)

    print(f"{kind.upper()} SEARCH: {query}")

    print("─" * 70)

    print(f"{len(results)} match{'es' if len(results) != 1 else ''}")

    print()

    for entry in results:
        print_entry(entry)


def show_tags(registry, identifier):

    identifier = identifier.lower()

    for entry in registry["item"]:
        if entry["id"].lower() == identifier:
            print(f"{entry['id']} — {entry['name']}")

            print()

            if not entry["tags"]:
                print("No tags.")
                return

            print("Tags:")

            for tag in entry["tags"]:
                print(f"  {tag}")

            return

    print(f"Item not found: {identifier}", file=sys.stderr)

    sys.exit(1)


def show_mod(registry, mod):

    mod = mod.lower()

    results = []

    for kind in registry:
        for entry in registry[kind]:
            if entry["mod"].lower() == mod:
                results.append((kind, entry))

    print(f"MOD: {mod}")
    print("─" * 70)
    print(f"{len(results)} entries")
    print()

    for kind, entry in results:
        print(f"{kind.upper():8} {entry['id']} — {entry['name']}")


def main():

    args = sys.argv[1:]

    if not args:
        usage()
        sys.exit(2)

    command = args[0].lower()

    if command in (
        "help",
        "-h",
        "--help",
    ):
        usage()
        return

    registry = parse_log()

    if command in (
        "item",
        "block",
        "machine",
        "entity",
    ):
        if len(args) < 2:
            print(f"Usage: lookup {command} <search>", file=sys.stderr)

            sys.exit(2)

        query = " ".join(args[1:])

        search(registry, command, query)

        return

    if command == "tags":
        if len(args) != 2:
            print("Usage: lookup tags <item-id>", file=sys.stderr)

            sys.exit(2)

        show_tags(registry, args[1])

        return

    if command == "mod":
        if len(args) != 2:
            print("Usage: lookup mod <mod-id>", file=sys.stderr)

            sys.exit(2)

        show_mod(registry, args[1])

        return

    print(f"Unknown command: {command}", file=sys.stderr)

    usage()

    sys.exit(2)


if __name__ == "__main__":
    main()
