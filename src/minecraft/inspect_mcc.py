#!/usr/bin/env python3

from __future__ import annotations

import io
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NBTReader:
    def __init__(self, data: bytes):
        self.data = data
        self.fp = io.BytesIO(data)

    def tell(self) -> int:
        return self.fp.tell()

    def read(self, n: int) -> bytes:
        b = self.fp.read(n)
        if len(b) != n:
            raise EOFError(f"Unexpected EOF at offset {self.tell()}")
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self.read(8))[0]

    def string(self) -> str:
        n = struct.unpack(">H", self.read(2))[0]
        return self.read(n).decode("utf-8", errors="replace")

    def skip_payload(self, tag: int) -> object:
        if tag == TAG_END:
            return None

        if tag == TAG_BYTE:
            return self.i8()

        if tag == TAG_SHORT:
            return self.i16()

        if tag == TAG_INT:
            return self.i32()

        if tag == TAG_LONG:
            return self.i64()

        if tag == TAG_FLOAT:
            return self.f32()

        if tag == TAG_DOUBLE:
            return self.f64()

        if tag == TAG_BYTE_ARRAY:
            n = self.i32()
            self.read(n)
            return f"<byte_array {n}>"

        if tag == TAG_STRING:
            return self.string()

        if tag == TAG_LIST:
            child_type = self.u8()
            n = self.i32()

            values = []
            for _ in range(n):
                values.append(self.skip_payload(child_type))

            return values

        if tag == TAG_COMPOUND:
            result = {}
            while True:
                child_type = self.u8()

                if child_type == TAG_END:
                    break

                name = self.string()
                result[name] = self.skip_payload(child_type)

            return result

        if tag == TAG_INT_ARRAY:
            n = self.i32()
            self.read(n * 4)
            return f"<int_array {n}>"

        if tag == TAG_LONG_ARRAY:
            n = self.i32()
            self.read(n * 8)
            return f"<long_array {n}>"

        raise ValueError(f"Unknown NBT tag type {tag} at offset {self.tell()}")


def find_entity_lists(obj, path="root"):
    """
    Recursively find lists whose elements look like entity compounds.
    """
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"

            if key.lower() == "entities" and isinstance(value, list):
                found.append((child_path, value))

            found.extend(find_entity_lists(value, child_path))

    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(find_entity_lists(value, f"{path}[{i}]"))

    return found


def entity_type(entity):
    if not isinstance(entity, dict):
        return "<non-compound>"

    # Standard entity identifier.
    ident = entity.get("id")
    if isinstance(ident, str):
        return ident

    # Fallbacks for unexpected structures.
    for key in ("type", "Type", "EntityType"):
        value = entity.get(key)
        if isinstance(value, str):
            return value

    return "<unknown>"


def item_stack_identity(entity):
    if not isinstance(entity, dict):
        return None

    item = entity.get("Item")

    if not isinstance(item, dict):
        return None

    item_id = item.get("id")
    count = item.get("Count")

    if not isinstance(item_id, str):
        return None

    return item_id, count


def estimate_size(obj) -> int:
    """
    Rough Python-object-independent serialized-content estimate.
    This is deliberately approximate; the useful number is the zlib
    decompressed payload size and the entity count/type distribution.
    """
    if obj is None:
        return 1

    if isinstance(obj, (bool, int, float)):
        return 8

    if isinstance(obj, str):
        return len(obj.encode("utf-8"))

    if isinstance(obj, list):
        return sum(estimate_size(x) for x in obj)

    if isinstance(obj, dict):
        total = 0
        for k, v in obj.items():
            total += len(str(k).encode("utf-8"))
            total += estimate_size(v)
        return total

    return 0


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} FILE.mcc")
        raise SystemExit(2)

    path = Path(sys.argv[1])

    compressed = path.read_bytes()
    decompressed = zlib.decompress(compressed)

    print("=" * 72)
    print("MCC INSPECTION")
    print("=" * 72)
    print(f"File:                 {path}")
    print(f"Compressed size:      {len(compressed):,} bytes")
    print(f"Decompressed size:    {len(decompressed):,} bytes")
    print()

    reader = NBTReader(decompressed)

    root_type = reader.u8()
    if root_type != TAG_COMPOUND:
        raise ValueError(f"Expected root TAG_COMPOUND, got {root_type}")

    root_name = reader.string()
    root = reader.skip_payload(TAG_COMPOUND)

    print(f"Root name:            {root_name!r}")
    print(f"Root keys:            {', '.join(sorted(root.keys()))}")
    print()

    entities_lists = find_entity_lists(root)

    if not entities_lists:
        print("No 'Entities' list found.")
        print()
        print("Top-level structure:")
        for k, v in root.items():
            if isinstance(v, list):
                print(f"  {k}: LIST[{len(v)}]")
            elif isinstance(v, dict):
                print(f"  {k}: COMPOUND[{len(v)} keys]")
            else:
                print(f"  {k}: {type(v).__name__}")
        return

    for list_path, entities in entities_lists:
        print("-" * 72)
        print(f"ENTITY LIST: {list_path}")
        print(f"Entity count:         {len(entities):,}")
        print()

        counts = Counter(entity_type(e) for e in entities)

        # ----- NEW: ItemStack analysis -----
        item_counts = Counter()
        item_total = 0

        for entity in entities:
            result = item_stack_identity(entity)
            if result is None:
                continue
            item_id, count = result
            item_counts[item_id] += 1
            if isinstance(count, int):
                item_total += count

        print("ItemStack IDs:")
        for item_id, count in item_counts.most_common():
            print(f"  {count:>10,}  {item_id}")

        print()
        print(f"Total item entities: {len(entities):,}")
        print(f"Entities with Item data: {sum(item_counts.values()):,}")
        print(f"Total item count:       {item_total:,}")
        print()
        # ----- END new code -----

        print("Entity types:")
        for ident, count in counts.most_common():
            print(f"  {count:>10,}  {ident}")

        print()

        # Show the largest entity compounds by rough estimate.
        sized = []
        for idx, entity in enumerate(entities):
            sized.append((estimate_size(entity), idx, entity_type(entity)))

        sized.sort(reverse=True)

        print("Largest entities (rough content estimate):")
        for size, idx, ident in sized[:20]:
            entity = entities[idx]
            pos = entity.get("Pos")
            uuid = entity.get("UUID")

            print(f"  {size:>12,} bytes  index={idx:<8} type={ident}")

            if pos is not None:
                print(f"      Pos:  {pos}")

            if uuid is not None:
                print(f"      UUID: {uuid}")

        print()
        print("Create package count:")
        package_count = sum(
            count
            for ident, count in counts.items()
            if ident
            in {
                "create:package",
                "create:package_entity",
                "create:package_entity_item",
            }
        )
        print(f"  {package_count:,}")

    print()
    print("=" * 72)
    print("Inspection complete; source file was not modified.")
    print("=" * 72)


if __name__ == "__main__":
    main()
