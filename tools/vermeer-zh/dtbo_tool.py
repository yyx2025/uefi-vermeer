#!/usr/bin/env python3
"""Read an Android DTBO image and pull out the touchscreen overlay.

Android's dtbo partition is a table of flattened device trees ("DTBO v1"):

  header   magic 0xd7b7ab1e, total_size, header_size, dt_entry_size,
           dt_entry_count, dt_entries_offset, page_size, version
  entry    dt_size, dt_offset, id, revision   (16 bytes each)
  trees    one FDT per entry, each an *overlay*: /fragment@N nodes carrying
           `target` (the node it applies to) and `__overlay__` (what to add).

A DTBO can hold dozens of overlays for different panel/touch configurations;
only one matches a given unit. This tool lists them, finds the ones that carry
touchscreen nodes, and prints what those nodes declare, so the right overlay can
be merged into the platform DTB.

Usage:
  python dtbo_tool.py list    dtbo.img          list every overlay + its id
  python dtbo_tool.py touch   dtbo.img          only overlays with touch nodes
  python dtbo_tool.py show    dtbo.img INDEX    dump one overlay's fragments
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fdt_tool import Fdt, describe  # noqa: E402

DTBO_MAGIC = 0xD7B7AB1E
TOUCH_MARKERS = ('touch', 'goodix', 'novatek', 'synaptics', 'focaltech', 'himax', 'gt9916')


def parse_header(data):
    (magic, total, header_size, entry_size, entry_count, entries_offset,
     page_size, version) = struct.unpack_from('>8I', data, 0)
    if magic != DTBO_MAGIC:
        raise SystemExit(f'not a DTBO image (magic {magic:#x}, expected {DTBO_MAGIC:#x})')
    entries = []
    for index in range(entry_count):
        offset = entries_offset + index * entry_size
        dt_size, dt_offset, ident, revision = struct.unpack_from('>4I', data, offset)
        entries.append({'index': index, 'size': dt_size, 'offset': dt_offset,
                        'id': ident, 'revision': revision})
    return {'total': total, 'header_size': header_size, 'page_size': page_size,
            'version': version, 'entries': entries}


def overlay_nodes(data, entry):
    """Return [(path, props)] for one overlay FDT."""
    blob = data[entry['offset']:entry['offset'] + entry['size']]
    fdt = Fdt(blob)
    return list(fdt.nodes())


def touch_nodes(nodes):
    """The nodes in an overlay that look like a touchscreen."""
    found = []
    for path, props in nodes:
        blob = (path + ' ' + ' '.join(props.keys())).lower()
        if any(marker in blob for marker in TOUCH_MARKERS):
            found.append((path, props))
    return found


def cmd_list(path, only_touch=False):
    data = Path(path).read_bytes()
    header = parse_header(data)
    print(f'{path}: DTBO v{header["version"]}, {len(header["entries"])} overlays, '
          f'total {header["total"]:,} bytes')
    for entry in header['entries']:
        if entry['size'] == 0:
            continue
        nodes = overlay_nodes(data, entry)
        hits = touch_nodes(nodes)
        if only_touch and not hits:
            continue
        labels = sorted({p.rstrip('/').split('/')[-1] for p, _ in hits})[:6]
        tag = f'  触摸节点 {len(hits)}: {", ".join(labels)}' if hits else ''
        print(f'  [{entry["index"]:>3}] id {entry["id"]:#010x} rev {entry["revision"]:#010x} '
              f'{entry["size"]:>7} bytes, {len(nodes):>4} nodes{tag}')
    return 0


def cmd_show(path, index):
    data = Path(path).read_bytes()
    header = parse_header(data)
    if index >= len(header['entries']):
        raise SystemExit(f'overlay {index} out of range (0..{len(header["entries"]) - 1})')
    entry = header['entries'][index]
    nodes = overlay_nodes(data, entry)
    print(f'overlay {index}: id {entry["id"]:#010x} rev {entry["revision"]:#010x}, '
          f'{entry["size"]} bytes, {len(nodes)} nodes')
    for node_path, props in nodes:
        print(f'\n{node_path}')
        for key, value in props.items():
            print(f'   {key:34} {describe(value)[:70]}')
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    command, path = argv[0], argv[1]
    if command == 'list':
        return cmd_list(path)
    if command == 'touch':
        return cmd_list(path, only_touch=True)
    if command == 'show':
        return cmd_show(path, int(argv[2]))
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
