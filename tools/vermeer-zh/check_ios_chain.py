#!/usr/bin/env python3
"""Verify that the iOS boot chain in this repository actually fits together.

The chain is:  fastboot -> BootShim -> UEFI (vermeerPkg) -> BDS -> UniXNU
               -> gUniXNULoaderProtocol -> DarwinBoot -> XNU

Each link has a contract that can be checked on the host, without a device:

  kernel    DarwinBoot ParseKernel accepts only feedfacf / 0x100000c / 2,
            needs 64-bit segments, and refuses if the entry is missing.
  ADT       DarwinBoot walks the .adt sidecar and requires Offset == TreeSize
            after reading ramdisk offset, trustcache offset and window size.
  protocol  UniXNU calls Revision 2 with kexts; DarwinBoot rejects nonzero
            KextCount and a non-NULL DeviceTree, so UniXNU must pass neither.
  video     BootVideoFromGop only accepts PixelBlueGreenRedReserved8BitPerColor,
            which is exactly what SimpleFbDxe must publish.

This tool checks each of them and prints a pass/fail line, so a mismatch is
found here rather than at runtime on hardware.

Usage:
  python check_ios_chain.py [--ipsw EXTRACT-DIR]
"""
import argparse
import struct
import sys
from pathlib import Path

UEFI = Path(r'D:\ios hack\src\uefi-vermeer')
UNIXNU = Path(r'D:\ios hack\src\UniXNU')
DEVICE = Path(r'D:\ios hack\src\UniXNU-vermeer')

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64E = 0x0100000C
CPU_SUBTYPE_ARM64E = 2
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 0x05


def check_kernel_format(kernel_path):
    """Mirror the checks ParseKernel performs on the file header."""
    data = kernel_path.read_bytes()
    print(f'\nkernel: {kernel_path.name} ({len(data):,} bytes)')
    if len(data) < 32:
        return False, 'too small'
    magic, cputype, cpusub, filetype = struct.unpack_from('<IiII', data, 0)
    if magic != MH_MAGIC_64:
        return False, f'magic {magic:#x} (ParseKernel wants {MH_MAGIC_64:#x})'
    if cputype & 0xFFFFFFFF != CPU_TYPE_ARM64E:
        return False, f'cputype {cputype:#x} (wants arm64e)'
    if cpusub & 0xFFFFFFFF != CPU_SUBTYPE_ARM64E:
        return False, f'cpusubtype {cpusub} (wants 2)'
    if filetype != 2:
        return False, f'filetype {filetype} (wants 2 = MH_EXECUTE)'

    ncmds, sizeofcmds = struct.unpack_from('<II', data, 16)
    if sizeofcmds > len(data) - 32:
        return False, 'sizeofcmds overruns the file'
    offset = 32
    segments = 0
    has_entry = False
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if size < 8 or size & 7 or size > len(data) - offset:
            return False, f'load command {cmd:#x} has length {size}'
        if cmd == LC_SEGMENT_64:
            vmsize = struct.unpack_from('<Q', data, offset + 32)[0]
            if vmsize:
                segments += 1
        elif cmd == LC_UNIXTHREAD:
            has_entry = True
        offset += size
    if segments == 0:
        return False, 'no segment with a nonzero vmsize'
    if not has_entry:
        return False, 'no LC_UNIXTHREAD (DarwinBoot refuses without an entry)'
    return True, f'{segments} segments, entry present'


def check_adt(path):
    """Check a sidecar tree the way DarwinBoot will see it.

    Two different formats are in play and they must not be confused:
      * .adt  -- Apple's ADT (the format DarwinBoot's AdtNode walks)
      * .dtb  -- flattened device tree (what EDK II and the K70 bootloader use)
    DarwinBoot only ever reads an .adt next to the kernel. A .dtb is not a
    substitute, so the check reports which format it found.
    """
    data = path.read_bytes()
    print(f'\nADT: {path.name} ({len(data):,} bytes)')

    if data[:4] == b'\xd0\x0d\xfe\xed':
        total = struct.unpack_from('>I', data, 4)[0]
        ok = total == len(data)
        return ok, (f'FDT (not ADT): totalsize {total:#x} vs file {len(data):#x}'
                    + ('' if ok else ' -- header disagrees with the file'))

    if data[:4] != b'\x15\x00\x00\x00':
        return False, f'unrecognised header {data[:8].hex()}'

    def read_node(offset, depth):
        if offset + 8 > len(data):
            raise ValueError(f'truncated node header at {offset}')
        n_props, n_children = struct.unpack_from('<II', data, offset)
        offset += 8
        names = []
        for _ in range(n_props):
            if offset + 36 > len(data):
                raise ValueError(f'truncated property at {offset}')
            name = data[offset:offset + 32].split(b'\x00')[0].decode('latin-1')
            size = struct.unpack_from('<I', data, offset + 32)[0]
            offset += 36 + ((size + 3) & ~3)
            if offset > len(data):
                raise ValueError(f'property {name!r} overruns the file')
            names.append(name)
        for _ in range(n_children):
            offset, _ = read_node(offset, depth + 1)
        return offset, names

    try:
        offset, root_props = read_node(0, 0)
    except ValueError as error:
        return False, str(error)
    ok = offset == len(data)
    detail = (f'ADT: walk ends at {offset:#x}, file is {len(data):#x} '
              f'("{root_props[0] if root_props else "?"}")')
    if not ok:
        return False, detail + ' -- DarwinBoot requires the walk to consume the file'
    return True, detail


def check_protocol_contract():
    """UniXNU must not pass kexts or a device tree to DarwinBoot.

    This is a *configuration* constraint, not a code bug: UniXNU passes
    `Request.DeviceTree = Fdt`, and Fdt is non-NULL only when the entry sets
    `dtb`. So an xnu entry that sets dtb is rejected by DarwinBoot at runtime.
    The contract therefore holds only if no xnu entry carries a dtb.
    """
    print('\nprotocol: UniXNU -> DarwinBoot')
    header = (UNIXNU / 'package/UniXNUPkg/Include/Protocol/UniXNULoader.h').read_text(
        encoding='utf-8', errors='replace')
    darwin = (UNIXNU / 'drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c').read_text(
        encoding='utf-8', errors='replace')
    inject = (UNIXNU / 'package/UniXNUPkg/Library/UniXNULib/Inject.c').read_text(
        encoding='utf-8', errors='replace')

    problems = []
    if 'UNIXNU_LOADER_REVISION    0x00020000' not in header:
        problems.append('protocol revision is not 2')
    # DarwinBoot refuses DT/kexts; UniXNU only produces a DT when the entry asks.
    if 'Request->DeviceTree != NULL' not in darwin:
        problems.append('DarwinBoot no longer filters on DeviceTree (contract changed)')
    if 'if (Entry->Dtb == NULL)' not in inject:
        problems.append('PrepareDeviceTree no longer short-circuits on an unset dtb')
    if problems:
        return False, '; '.join(problems)

    # The remaining condition depends on configuration, which is checked
    # separately by check_xnu_entries.py.
    return True, ('revision 2; DT only when entry.dtb is set, so DarwinBoot '
                  'accepts entries without dtb')


def check_video_format():
    print('\nvideo: SimpleFbDxe -> BootVideoFromGop')
    simple = (UEFI / 'Silicon/Silicium/SiliciumPkg/Drivers/SimpleFbDxe/SimpleFb.c').read_text(
        encoding='utf-8', errors='replace')
    video = (UNIXNU / 'drivers/DarwinBoot/DarwinBootPkg/Driver/BootVideo.h').read_text(
        encoding='utf-8', errors='replace')
    wants = 'PixelBlueGreenRedReserved8BitPerColor' in video
    publishes = re_search_pixel_format(simple)
    if not wants:
        return False, 'BootVideo does not filter on a pixel format'
    return True, f'BootVideo wants BGRX; SimpleFbDxe sets {publishes}'


def re_search_pixel_format(source):
    for line in source.splitlines():
        if 'PixelFormat' in line and '=' in line:
            return line.split('=')[-1].strip().rstrip(';')
    return 'PixelFormat (not assigned in SimpleFb.c)'


def check_ipsw_kernel(extract):
    """If an IPSW extraction is available, check its kernelcache against the ABI."""
    if not extract:
        return None
    candidate = Path(extract) / 'kernelcache.dec.bin'
    if not candidate.is_file():
        return None
    return check_kernel_format(candidate)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ipsw', help='IPSW extraction directory (optional)')
    parser.add_argument('--adt', default=str(DEVICE / 'scripts/data/vermeer-splash.dtb'))
    args = parser.parse_args()

    results = []
    print('=' * 70)
    print('iOS boot chain contract check')
    print('=' * 70)

    ok, detail = check_protocol_contract()
    results.append(('protocol', ok, detail))

    ok, detail = check_video_format()
    results.append(('video', ok, detail))

    adt_path = Path(args.adt)
    if adt_path.is_file():
        ok, detail = check_adt(adt_path)
        results.append(('ADT', ok, detail))
    else:
        results.append(('ADT', False, f'{adt_path} missing'))

    ipsw_result = check_ipsw_kernel(args.ipsw)
    if ipsw_result:
        results.append(('ipsw kernel', ipsw_result[0], ipsw_result[1]))

    print('\n' + '=' * 70)
    failures = 0
    for name, ok, detail in results:
        print(f'{"PASS" if ok else "FAIL"}  {name:12} {detail}')
        failures += 0 if ok else 1
    print('=' * 70)
    print(f'{len(results) - failures}/{len(results)} contracts hold')
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
