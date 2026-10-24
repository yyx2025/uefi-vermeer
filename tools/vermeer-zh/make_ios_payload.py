#!/usr/bin/env python3
"""Prepare a bootable iOS payload directory from an IPSW extraction.

What the UEFI side needs, and why each piece exists:

  kernel.macho   XNU as a Mach-O that DarwinBoot accepts: arm64e, MH_EXECUTE,
                 64-bit segments, an LC_UNIXTHREAD entry.
  kernel.adt     the Apple-format device tree DarwinBoot walks; it carries the
                 window size, the ramdisk offset and the trust-cache offset.
  ramdisk        the boot ramdisk, optionally the packed form DarwinBoot knows.
  trustcache     the static trust cache the kernel consults.

This tool takes an extracted IPSW and produces that directory, then reports what
it produced. Where a piece cannot be produced from the extraction it says so
plainly instead of writing a placeholder that would fail later on hardware.

For a kernelcache that is still an IM4P container, the kernel is unwrapped and
decompressed first (LZFSE "bvx2"), because DarwinBoot reads the Mach-O directly.

Usage:
  python make_ios_payload.py --extract DIR --out DIR [--trustcache FILE]
"""
import argparse
import json
import lzma
import struct
import sys
import zlib
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 0x05
LC_FILESET_ENTRY = 0x80000035


def der_element(data, off):
    tag = data[off]
    length = data[off + 1]
    head = 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[off + 2:off + 2 + count], 'big')
        head = 2 + count
    return tag, data[off + head:off + head + length], off + head + length


def unwrap_im4p(blob):
    """Return (type, payload) from an IM4P container, or (None, blob)."""
    if blob[:1] != b'\x30':
        return None, blob
    tag, body, _ = der_element(blob, 0)
    items = []
    off = 0
    while off < len(body):
        t, value, off = der_element(body, off)
        items.append((t, value))
    kind = None
    payload = b''
    for t, value in items:
        if t in (0x16, 0x0C):
            text = value.decode('utf-8', 'replace')
            if text != 'IM4P' and kind is None:
                kind = text
        elif t == 0x04 and not payload:
            payload = value
    return kind, payload


def decompress(payload):
    """Undo the container compression Apple applies to dtre/krnl payloads."""
    if payload[:4] == b'bvx2':
        import lzfse
        return lzfse.decompress(payload), 'lzfse'
    if payload[:4] == b'compl':
        return lzma.decompress(payload[4:]), 'lzss'
    if payload[:2] == b'\x1f\x8b':
        return zlib.decompress(payload, 16 + zlib.MAX_WBITS), 'gzip'
    return payload, 'none'


def describe_macho(data):
    magic, cputype, cpusub, filetype, ncmds, sizeofcmds, flags, _ = \
        struct.unpack_from('<IiIIIIII', data, 0)
    segments = 0
    entry = None
    fileset = 0
    off = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, off)
        if cmd == LC_SEGMENT_64:
            if struct.unpack_from('<Q', data, off + 32)[0]:
                segments += 1
        elif cmd == LC_UNIXTHREAD:
            regs = struct.unpack_from('<34Q', data, off + 16)
            entry = regs[32]
        elif cmd == LC_FILESET_ENTRY:
            fileset += 1
        off += size
    return {'magic': hex(magic), 'cputype': hex(cputype & 0xFFFFFFFF),
            'cpusubtype': cpusub & 0xFFFFFFFF, 'filetype': filetype,
            'segments': segments, 'entry': hex(entry) if entry else None,
            'fileset_entries': fileset}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--extract', required=True, help='IPSW extraction directory')
    parser.add_argument('--out', required=True, help='payload output directory')
    parser.add_argument('--trustcache', help='static trust cache file to include')
    args = parser.parse_args()

    extract = Path(args.extract)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {'source': str(extract), 'pieces': {}, 'missing': []}

    # 1. kernel: prefer an already-decompressed Mach-O, else unwrap the IM4P.
    kernel_source = None
    for name in ('kernelcache.dec.bin', 'kernelcache.kc', 'kernelcache.release.iphone15'):
        candidate = extract / name
        if candidate.is_file():
            kernel_source = candidate
            break

    if kernel_source is None:
        report['missing'].append('kernelcache (no kernelcache.* in the extraction)')
        print('no kernelcache found')
    else:
        blob = kernel_source.read_bytes()
        kind, payload = unwrap_im4p(blob)
        note = None
        if kind:
            payload, compression = decompress(payload)
            note = f'unwrapped IM4P type "{kind}", decompressed with {compression}'
        else:
            note = 'already a Mach-O'
        kernel = out / 'kernel.macho'
        kernel.write_bytes(payload)
        info = describe_macho(payload)
        report['pieces']['kernel'] = {'file': kernel.name, 'bytes': len(payload),
                                      'note': note, **info}
        print(f'kernel: {kernel.name} ({len(payload):,} bytes)')
        print(f'  {info}')
        ok = (info['magic'] == hex(MH_MAGIC_64) and info['cputype'] == hex(0x100000c)
              and info['cpusubtype'] == 2 and info['filetype'] == 2 and info['segments'])
        if not ok:
            report['missing'].append('kernel is not the arm64e MH_EXECUTE DarwinBoot requires')
        if info['fileset_entries']:
            print(f'  note: this is a fileset with {info["fileset_entries"]} entries;')
            print('        DarwinBoot wants the kernel executable, not the container.')

    # 2. trust cache
    tc = Path(args.trustcache) if args.trustcache else None
    if tc is None:
        for name in ('StaticTrustCache', 'Firmware/098-09093-001.dmg.trustcache'):
            candidate = extract / name
            if candidate.is_file():
                tc = candidate
                break
    if tc and tc.is_file():
        kind, payload = unwrap_im4p(tc.read_bytes())
        target = out / 'trustcache'
        target.write_bytes(payload)
        report['pieces']['trustcache'] = {'file': target.name, 'bytes': len(payload),
                                          'type': kind}
        print(f'trustcache: {target.name} ({len(payload):,} bytes, im4p type {kind})')
    else:
        report['missing'].append('trustcache')

    # 3. what still has to come from the build system
    report['missing'].append('kernel.adt (generated by scripts/make-vermeer-adt.py)')
    report['missing'].append('ramdisk (boot ramdisk; DarwinBoot accepts a packed form)')

    (out / 'payload.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'\nwrote {out / "payload.json"}')
    if report['missing']:
        print('still needed from the build system:')
        for item in report['missing']:
            print(f'  - {item}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
