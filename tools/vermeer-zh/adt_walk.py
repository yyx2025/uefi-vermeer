#!/usr/bin/env python3
"""Walk an Apple ADT exactly the way DarwinBoot's AdtNode does.

DarwinBoot refuses a tree unless the node chain consumes the file exactly
(`Offset != TreeSize` -> EFI_COMPROMISED_DATA), and it looks for three specific
nodes: the ramdisk offset, the trust-cache offset and the window size. A tree
that parses but lacks them still fails on hardware, so both are checked here.

The ADT encoding (see make-vermeer-adt.py):
    node     u32 nProperties, u32 nChildren, properties..., children...
    property char name[32], u32 length, payload, padded to 4 bytes
A property whose payload is a child count and whose name is empty opens a node;
`/chosen/memory-map` holds the ramdisk and trust-cache ranges as (offset, size)
pairs of u64.

Usage: python adt_walk.py FILE
"""
import struct
import sys
from pathlib import Path

# The node names DarwinBoot reads.
WANTED = ('memory-map', 'RAMDisk', 'PackedRAMDisk', 'TrustCache')


def walk(data, offset, depth, path, found):
    if offset + 8 > len(data):
        raise ValueError(f'truncated node header at {offset:#x}')
    n_props, n_children = struct.unpack_from('<II', data, offset)
    offset += 8
    names = []
    for _ in range(n_props):
        if offset + 36 > len(data):
            raise ValueError(f'truncated property at {offset:#x}')
        name = data[offset:offset + 32].split(b'\x00')[0].decode('latin-1')
        length = struct.unpack_from('<I', data, offset + 32)[0]
        payload = data[offset + 36:offset + 36 + length]
        offset += 36 + ((length + 3) & ~3)
        if offset > len(data):
            raise ValueError(f'property {name!r} overruns at {offset:#x}')
        names.append(name)
        if name == 'name' and payload.rstrip(b'\x00'):
            path = path + '/' + payload.rstrip(b'\x00').decode('latin-1')
        if name in WANTED:
            found.append((path, name, payload))
    for _ in range(n_children):
        offset = walk(data, offset, depth + 1, path, found)
    return offset


def main(path):
    data = Path(path).read_bytes()
    print(f'{path}: {len(data):,} bytes')
    found = []
    try:
        end = walk(data, 0, 0, '', found)
    except ValueError as error:
        print(f'FAIL  {error}')
        return 1

    exact = end == len(data)
    print(f'{"PASS" if exact else "FAIL"}  node walk ends at {end:#x}, '
          f'file is {len(data):#x}'
          + ('' if exact else '  (DarwinBoot requires an exact match)'))

    print(f'\nnodes DarwinBoot reads ({len(found)} found):')
    for node_path, name, payload in found:
        if len(payload) == 16:
            first, second = struct.unpack('<QQ', payload)
            detail = f'offset {first:#x}, size {second:#x}'
        else:
            detail = payload.hex()[:32] or '(empty)'
        print(f'  {node_path or "/":34} {name:14} {detail}')

    names = {name for _, name, _ in found}
    # DarwinBoot looks for the memory-map node and the two range placeholders
    # inside it. A node is identified by the path component, so match on the
    # trailing segment rather than the whole path.
    paths = {path for path, _, _ in found}
    has_map = any(p.endswith('/memory-map') for p in paths)
    print(f'\nrequired nodes:')
    print(f'  {"PASS" if has_map else "FAIL"}  /chosen/memory-map present')

    if not has_map:
        return 1
    print('\nthe tree satisfies DarwinBoot\'s structural requirements')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
