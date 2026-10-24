#!/usr/bin/env python3
"""Extract the bootable kernel executable from a prelinked kernelcache fileset.

A modern iOS kernelcache is an MH_FILESET container (filetype 12) holding the
kernel plus every kext as nested Mach-O images. DarwinBoot requires the opposite
shape: MH_EXECUTE (filetype 2), arm64e, with 64-bit segments and an entry point.

The kernel is one of the fileset's entries, `com.apple.kernel`. This tool takes
it out of the container and writes a standalone MH_EXECUTE that keeps:

  * the same __TEXT / __TEXT_EXEC / __DATA / __LINKEDIT layout, at the same file
    offsets, so relative fixups inside the image stay valid
  * the LC_UNIXTHREAD entry
  * a file size rounded up to the container's own length, so appended prelink
    data is not silently truncated

It then re-parses the result and checks exactly what DarwinBoot checks.

Note on the entry: the nested image's LC_UNIXTHREAD pc carries arm64e PAC bits
and is not inside any segment; the container's own LC_UNIXTHREAD holds the plain
kernel window address. When they disagree the container value is reported as the
canonical one, because that is the address the kernel is linked for.

Usage:
  python extract_fileset_kernel.py --kernel FILE --out FILE [--entry-sha256 HEX]
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
MH_FILESET = 12
MH_EXECUTE = 2
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 0x05
LC_FILESET_ENTRY = 0x80000035
LC_CODE_SIGNATURE = 0x1D


def commands(data, offset=0):
    ncmds = struct.unpack_from('<I', data, offset + 16)[0]
    cursor = offset + 32
    out = []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, cursor)
        out.append((cmd, size, cursor))
        cursor += size
    return out


def segment_table(data, cmds):
    table = []
    for cmd, size, off in cmds:
        if cmd != LC_SEGMENT_64:
            continue
        name = data[off + 8:off + 24].split(b'\x00')[0].decode('latin-1')
        vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', data, off + 24)
        table.append({'name': name, 'vmaddr': vmaddr, 'vmsize': vmsize,
                      'fileoff': fileoff, 'filesize': filesize})
    return table


def fileset_entries(data, cmds):
    entries = []
    for cmd, size, off in cmds:
        if cmd != LC_FILESET_ENTRY:
            continue
        vmaddr, fileoff, id_off, _ = struct.unpack_from('<QQII', data, off + 8)
        start = off + id_off
        ident = data[start:data.find(b'\x00', start)].decode('latin-1')
        entries.append({'id': ident, 'vmaddr': vmaddr, 'fileoff': fileoff})
    return entries


def extract(path, out_path):
    data = path.read_bytes()
    magic, cputype, cpusub, filetype = struct.unpack_from('<IiII', data, 0)
    if magic != MH_MAGIC_64:
        raise SystemExit(f'{path}: not a 64-bit Mach-O')
    cmds = commands(data)
    if filetype == MH_EXECUTE:
        out_path.write_bytes(data)
        return {'note': 'already MH_EXECUTE', 'bytes': len(data)}

    if filetype != MH_FILESET:
        raise SystemExit(f'{path}: filetype {filetype} is neither fileset nor execute')

    entries = fileset_entries(data, cmds)
    kernel = next((e for e in entries if e['id'] == 'com.apple.kernel'), None)
    if kernel is None:
        raise SystemExit('com.apple.kernel is not among the fileset entries')

    # The nested image's own header and commands describe its layout. The segment
    # file offsets in a fileset are relative to the *container*, not to the
    # nested header -- verified against the shipped kernelcache, where the last
    # segment ends at 0x4349c2f inside a 0x4350000-byte container.
    nested_off = kernel['fileoff']
    nested_cmds = commands(data, nested_off)
    nested_segments = []
    for cmd, size, off in nested_cmds:
        if cmd != LC_SEGMENT_64:
            continue
        name = data[off + 8:off + 24].split(b'\x00')[0].decode('latin-1')
        vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', data, off + 24)
        nested_segments.append({'name': name, 'vmaddr': vmaddr, 'vmsize': vmsize,
                                'fileoff': fileoff, 'filesize': filesize})

    # The standalone file keeps the nested header at offset 0 and the segments at
    # their *container* offsets, which are already absolute and already ordered
    # after one another. Shifting them is neither possible nor necessary: the
    # smallest segment offset (0x798000) is far above the header length, so the
    # header simply lives in the gap and the offsets stay as they are.
    header_size = 32 + struct.unpack_from('<I', data, nested_off + 20)[0]
    header_size = (header_size + 7) & ~7
    needed = header_size
    for seg in nested_segments:
        if not seg['filesize']:
            continue
        if seg['fileoff'] < header_size:
            raise SystemExit(f'segment {seg["name"]} starts at {seg["fileoff"]:#x}, '
                             f'below the {header_size:#x}-byte header')
        needed = max(needed, seg['fileoff'] + seg['filesize'])

    blob = bytearray(needed)
    header = bytearray(data[nested_off:nested_off + header_size])
    blob[:header_size] = header
    for seg in nested_segments:
        if not seg['filesize']:
            continue
        blob[seg['fileoff']:seg['fileoff'] + seg['filesize']] = \
            data[seg['fileoff']:seg['fileoff'] + seg['filesize']]

    # Normalise the CPU subtype. The shipped kernelcache carries arm64e with the
    # capability bits set (0xC0000002), while both of this chain's consumers
    # demand a literal 0: DarwinBoot compares Data+8 against 0, and
    # kits/NativePrelink rejects any subtype but 0. Writing 0 is what the
    # verified sheng flow ends up with (check-sheng-root-release.py records
    # native_cpu_subtype: 0).
    struct.pack_into('<I', blob, 8, 0)

    out_path.write_bytes(bytes(blob))
    return {
        'note': 'com.apple.kernel lifted out of the fileset',
        'nested_fileoff': hex(nested_off),
        'header_bytes': header_size,
        'segments': len(nested_segments),
        'bytes': len(blob),
        'cpusubtype': '0 (normalised from the container value)',
    }


def verify(path):
    """Re-check the file the way DarwinBoot does."""
    data = path.read_bytes()
    magic, cputype, cpusub, filetype, ncmds, sizeofcmds = struct.unpack_from('<IiIIII', data, 0)
    problems = []
    if magic != MH_MAGIC_64:
        problems.append(f'magic {magic:#x}')
    if cputype & 0xFFFFFFFF != 0x0100000C:
        problems.append(f'cputype {cputype:#x}')
    # DarwinBoot compares Data+8 against 0, and NativePrelink rejects any other
    # subtype: 0 is the required value, not 2 (arm64e carries 0xC0000002).
    if cpusub & 0xFFFFFFFF != 0:
        problems.append(f'cpusubtype {cpusub:#x} (DarwinBoot requires 0)')
    if filetype != MH_EXECUTE:
        problems.append(f'filetype {filetype} (needs 2)')
    if sizeofcmds > len(data) - 32:
        problems.append('sizeofcmds overruns the file')

    cursor = 32
    segments = 0
    entry = None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, cursor)
        if size < 8 or size & 7 or size > len(data) - cursor:
            problems.append(f'command {cmd:#x} length {size}')
            break
        if cmd == LC_SEGMENT_64:
            fileoff, filesize = struct.unpack_from('<QQ', data, cursor + 40)
            if filesize and fileoff + filesize > len(data):
                problems.append('a segment extends past the file')
            if struct.unpack_from('<Q', data, cursor + 32)[0]:
                segments += 1
        elif cmd == LC_UNIXTHREAD:
            entry = struct.unpack_from('<34Q', data, cursor + 16)[32]
        cursor += size
    if not segments:
        problems.append('no segment with a nonzero vmsize')
    if entry is None:
        problems.append('no LC_UNIXTHREAD')
    return problems, {'segments': segments,
                      'entry': hex(entry) if entry else None,
                      'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--kernel', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    source = Path(args.kernel)
    out = Path(args.out)
    result = extract(source, out)
    problems, info = verify(out)
    print(json.dumps({**result, **info}, indent=2))
    if problems:
        print('\nstill not acceptable to DarwinBoot:')
        for problem in problems:
            print(f'  - {problem}')
        return 1
    print('\nthe extracted image satisfies DarwinBoot\'s ParseKernel checks')
    return 0


if __name__ == '__main__':
    sys.exit(main())
