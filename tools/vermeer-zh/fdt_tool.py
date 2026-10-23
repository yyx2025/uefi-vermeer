#!/usr/bin/env python3
"""Pure-Python FDT (flattened device tree) reader/patch tool.

Everything here is standard library only: the shellout-free path matters on
Windows hosts that have no dtc/fdtput, and it lets the build verify its own
output byte for byte.

FDT v17 layout:
  header   magic 0xd00dfeed, totalsize, off_dt_struct, off_dt_strings,
           off_mem_rsvmap, version, last_comp_version, boot_cpuid_phys,
           size_dt_strings, size_dt_struct
  memrsv   (u64 addr, u64 size) ... terminated by 0,0
  struct   FDT_BEGIN_NODE(1) name, props..., FDT_END_NODE(2);
           FDT_PROP(3) len, nameoff, value; FDT_NOP(4); FDT_END(9)
  strings  property name pool

Usage:
  python fdt_tool.py dump  FILE [--path /reserved-memory]
  python fdt_tool.py add-node FILE --path /reserved-memory/splash_region \
      --prop str:label=cont_splash_region --prop x:reg=0,0xb8000000,0,0x2b00000 \
      --out OUT
  python fdt_tool.py verify FILE --path /reserved-memory/splash_region
"""
import struct
import sys
from pathlib import Path

MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


class Fdt:
    def __init__(self, blob):
        (magic, self.total, self.off_struct, self.off_strings, self.off_memrsv,
         self.version, self.last_comp, self.boot_cpuid, self.size_strings,
         self.size_struct) = struct.unpack_from('>10I', blob, 0)
        if magic != MAGIC:
            raise ValueError(f'not an FDT: magic {magic:#x}')
        self.blob = bytearray(blob)
        self.strings = bytes(self.blob[self.off_strings:self.off_strings + self.size_strings])
        self.struct = bytes(self.blob[self.off_struct:self.off_struct + self.size_struct])

    # -- reading ------------------------------------------------------------

    def string_at(self, offset):
        end = self.strings.find(b'\x00', offset)
        return self.strings[offset:end].decode('utf-8', 'replace')

    def nodes(self):
        """Yield (path, properties) for every node, depth first."""
        struct_data = self.struct
        index = 0
        path = []

        while index + 4 <= len(struct_data):
            tag = struct.unpack_from('>I', struct_data, index)[0]
            index += 4
            if tag == FDT_NOP:
                continue
            if tag == FDT_END:
                break
            if tag == FDT_BEGIN_NODE:
                end = struct_data.find(b'\x00', index)
                name = struct_data[index:end].decode('utf-8', 'replace')
                index = (end + 4) & ~3
                path.append(name)
                current = '/' + '/'.join(p for p in path if p)
                props = {}
                # Properties follow until the first child or the end node.
                while True:
                    peek = struct.unpack_from('>I', struct_data, index)[0]
                    if peek == FDT_PROP:
                        length, nameoff = struct.unpack_from('>II', struct_data, index + 4)
                        value = struct_data[index + 12:index + 12 + length]
                        props[self.string_at(nameoff)] = value
                        index = (index + 12 + length + 3) & ~3
                    elif peek in (FDT_NOP, FDT_BEGIN_NODE, FDT_END_NODE, FDT_END):
                        break
                    else:
                        raise ValueError(f'unknown tag {peek:#x} in node {current}')
                yield current, props
            elif tag == FDT_END_NODE:
                if path:
                    path.pop()
            else:
                raise ValueError(f'unexpected tag {tag:#x} at {index:#x}')

    def find(self, path):
        for node_path, props in self.nodes():
            if node_path == path:
                return props
        return None

    # -- writing ------------------------------------------------------------

    def add_node(self, path, properties):
        """Add a node with properties under path's parent; returns a new Fdt.

        A node that already exists is left alone (the caller verifies).
        """
        if self.find(path) is not None:
            return self
        parent_path, _, node_name = path.rpartition('/')
        parent_path = parent_path or '/'

        # Serialise the new node.
        new_struct = bytearray()
        new_struct += struct.pack('>I', FDT_BEGIN_NODE)
        encoded = node_name.encode() + b'\x00'
        new_struct += encoded + b'\x00' * ((-len(encoded)) % 4)
        strings = bytearray(self.strings)
        for key, value in properties.items():
            # Accept both raw bytes and the (kind, bytes) tuples that
            # parse_prop() produces; only the bytes matter here.
            raw = value[1] if isinstance(value, tuple) else value
            offset = self._intern(strings, key)
            new_struct += struct.pack('>III', FDT_PROP, len(raw), offset)
            new_struct += raw + b'\x00' * ((-len(raw)) % 4)
        new_struct += struct.pack('>I', FDT_END_NODE)

        # Splice it just before the parent's FDT_END_NODE.
        parent_close = self._find_node_end(parent_path)
        if parent_close is None:
            raise ValueError(f'parent node not found: {parent_path}')

        struct_out = bytearray(self.struct)
        struct_out[parent_close:parent_close] = new_struct
        return self._rebuild(struct_out, strings)

    def _intern(self, strings, key):
        needle = key.encode() + b'\x00'
        found = strings.find(needle)
        if found >= 0 and (found == 0 or strings[start_boundary(strings, found)]):
            return found
        offset = len(strings)
        strings += needle
        return offset

    def _find_node_end(self, path):
        """Byte offset in struct of the path node's own FDT_END_NODE."""
        parts = [p for p in path.split('/') if p]
        index = 0
        stack = []
        while index + 4 <= len(self.struct):
            tag = struct.unpack_from('>I', self.struct, index)[0]
            if tag == FDT_BEGIN_NODE:
                end = self.struct.find(b'\x00', index + 4)
                name = self.struct[index + 4:end].decode('utf-8', 'replace')
                # The root node has an empty name and does not consume a path part.
                if name:
                    stack.append(name)
                index = (end + 4) & ~3
            elif tag == FDT_PROP:
                length = struct.unpack_from('>I', self.struct, index + 4)[0]
                index = (index + 12 + length + 3) & ~3
            elif tag == FDT_NOP:
                index += 4
            elif tag == FDT_END_NODE:
                if stack == parts:
                    return index
                if stack:
                    stack.pop()
                index += 4
            else:
                return None
        return None

    def _rebuild(self, struct_out, strings):
        memrsv = bytes(self.blob[self.off_memrsv:self.off_struct])
        out = bytearray()
        header_size = 40
        off_memrsv = header_size
        # Keep the memory reservation block where it is; recompute the rest.
        out += b'\x00' * header_size
        out += memrsv
        while len(out) % 8:
            out += b'\x00'
        off_struct = len(out)
        out += struct_out
        out += b'\x00' * ((-len(out)) % 4)
        off_strings = len(out)
        out += strings
        total = len(out)
        struct.pack_into('>10I', out, 0, MAGIC, total, off_struct, off_strings,
                         off_memrsv, self.version, self.last_comp, self.boot_cpuid,
                         len(strings), len(struct_out))
        return Fdt(bytes(out))


def start_boundary(strings, found):
    """True when offset `found` begins a full name (not a tail of a longer one)."""
    return found > 0 and strings[found - 1:found] == b'\x00'


def parse_prop(spec):
    """str:name=value | x:name=a,b,... | i:name=n | b:name=hexbytes"""
    kind, _, rest = spec.partition(':')
    key, _, value = rest.partition('=')
    if kind == 'str':
        return key, ('str', value.encode() + b'\x00')
    if kind == 'x':
        words = [int(v, 0) for v in value.split(',') if v.strip()]
        return key, ('x', b''.join(struct.pack('>I', w & 0xFFFFFFFF) for w in words))
    if kind == 'i':
        return key, ('i', struct.pack('>I', int(value, 0) & 0xFFFFFFFF))
    if kind == 'b':
        return key, ('b', bytes.fromhex(value))
    raise SystemExit(f'bad property spec: {spec} (use str:/x:/i:/b:)')


def describe(value):
    if not value:
        return '(empty)'
    # A NUL-terminated all-printable payload is a string, whatever its length.
    stripped = value.rstrip(b'\x00')
    if value.endswith(b'\x00') and stripped and all(32 <= b < 127 for b in stripped):
        return repr(stripped.decode('ascii'))
    if len(value) % 4 == 0:
        words = struct.unpack('>%dI' % (len(value) // 4), value)
        return ' '.join(f'{w:#x}' for w in words[:8])
    return value.hex()


def cmd_dump(args):
    fdt = Fdt(Path(args[0]).read_bytes())
    target = args[args.index('--path') + 1] if '--path' in args else None
    for path, props in fdt.nodes():
        if target and path != target:
            continue
        print(path)
        for key, value in props.items():
            print(f'   {key:24} {describe(value)}')
    return 0


def cmd_add_node(args):
    source = Path(args[0])
    path = args[args.index('--path') + 1]
    specs = [args[i + 1] for i, a in enumerate(args) if a == '--prop']
    out = Path(args[args.index('--out') + 1])
    fdt = Fdt(source.read_bytes())
    before = fdt.find(path)
    if before is not None:
        print(f'{path} already exists; nothing to do')
        out.write_bytes(source.read_bytes())
        return 0
    properties = dict(parse_prop(s) for s in specs)
    result = fdt.add_node(path, properties)
    blob = result.blob
    # Round-trip through a fresh parse before writing.
    check = Fdt(bytes(blob))
    got = check.find(path)
    if got is None:
        raise SystemExit('round-trip failed: node missing after patch')
    if check.total != len(blob):
        raise SystemExit(f'header totalsize {check.total:#x} != file size {len(blob):#x}')
    out.write_bytes(bytes(blob))
    print(f'wrote {out} ({len(blob)} bytes, was {source.stat().st_size})')
    for key, value in got.items():
        print(f'   {key:24} {describe(value)}')
    return 0


def cmd_verify(args):
    fdt = Fdt(Path(args[0]).read_bytes())
    path = args[args.index('--path') + 1]
    props = fdt.find(path)
    if props is None:
        print(f'MISSING {path}')
        return 1
    print(f'{path}: {len(props)} properties')
    for key, value in props.items():
        print(f'   {key:24} {describe(value)}')
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    command = argv[0]
    rest = argv[1:]
    if command == 'dump':
        return cmd_dump(rest)
    if command == 'add-node':
        return cmd_add_node(rest)
    if command == 'verify':
        return cmd_verify(rest)
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
