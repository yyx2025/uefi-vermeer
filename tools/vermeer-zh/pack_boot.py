#!/usr/bin/env python3
"""Pack the vermeer boot image exactly as the firmware expects it.

Reproduces UniXNU's bootimg step without needing the whole build system:

    kernel payload = gzip(BootShim.bin + SILICIUM_UEFI.fd) + patched_dtb
    boot.img       = mkbootimg(kernel, empty ramdisk, header v0, base 0)

Everything is verified by unpacking the result again:
  * ARM64 header of the shim describes (FD_BASE, shim + FD size)
  * gzip round-trips to shim+FD, and the appended DTB is byte identical
  * the boot image header carries the requested addresses
  * the appended DTB still has /reserved-memory/splash_region

Usage:
  python pack_boot.py --fd FD --uefi UEFI-DIR --dtb DTB [--out boot.img]
"""
import argparse
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

FD_BASE = 0xA7100000
FD_SIZE = 0x300000
PAGESIZE = 4096
HEADER_VERSION = 0
BASE = 0
KERNEL_OFFSET = 0x8000
TAGS_OFFSET = 0x1E00000


def compile_shim(uefi, scratch, clang, objcopy, nm):
    """Build BootShim.bin and return its bytes after checking the ARM64 header."""
    source = uefi / 'BootShim/BootShim.S'
    if not source.is_file():
        raise SystemExit(f'{source} missing: --uefi must be a Mu-Silicium checkout')
    obj = scratch / 'shim.o'
    binary = scratch / 'shim.bin'
    subprocess.run(
        [clang, '--target=aarch64-none-elf', '-c', f'-DFD_BASE={FD_BASE}',
         f'-DFD_SIZE={FD_SIZE}', '-DREQUIRES_KERNEL_HEADER=1', str(source), '-o', str(obj)],
        check=True, capture_output=True)
    subprocess.run([objcopy, '-O', 'binary', str(obj), str(binary)], check=True, capture_output=True)
    shim = binary.read_bytes()

    symbols = subprocess.run([nm, '-n', str(obj)], check=True, capture_output=True, text=True).stdout
    offset = next((int(line.split()[0], 16) for line in symbols.splitlines()
                   if line.endswith(' _Payload')), None)
    if offset != len(shim):
        # A literal pool between the code and _Payload would break the copy loop.
        raise SystemExit(f'_Payload at {offset} but shim is {len(shim)} bytes')
    if shim[56:60] != b'ARM\x64':
        raise SystemExit('the ARM64 image header magic is missing from the shim')
    text_offset, image_size = struct.unpack_from('<QQ', shim, 8)
    if (text_offset, image_size) != (FD_BASE, len(shim) + FD_SIZE):
        raise SystemExit(f'header says ({text_offset:#x}, {image_size:#x}), '
                         f'expected ({FD_BASE:#x}, {len(shim) + FD_SIZE:#x})')
    return shim


def verify_dtb(path):
    """Check the splash node the stock ABL needs before it keeps the panel on."""
    sys.path.insert(0, str(Path(__file__).parent))
    from fdt_tool import Fdt, describe
    fdt = Fdt(Path(path).read_bytes())
    props = fdt.find('/reserved-memory/splash_region')
    if props is None:
        raise SystemExit(f'{path}: /reserved-memory/splash_region is missing; '
                         'the stock bootloader powers the panel down without it')
    reg = props.get('reg')
    if reg is None or len(reg) != 16:
        raise SystemExit(f'{path}: splash_region reg is not 4 cells')
    words = struct.unpack('>4I', reg)
    if (words[1], words[3]) != (0xB8000000, 0x02B00000):
        raise SystemExit(f'{path}: splash_region reg is {[hex(w) for w in words]}, '
                         'expected 0 0xb8000000 0 0x2b00000')
    return fdt


def pack(fd_path, uefi_dir, dtb_path, out_path, mkbootimg):
    fd = Path(fd_path).read_bytes()
    if len(fd) != FD_SIZE:
        raise SystemExit(f'{fd_path} is {len(fd):#x} bytes, the platform config says {FD_SIZE:#x}')
    verify_dtb(dtb_path)

    clang = 'clang'
    objcopy = 'llvm-objcopy'
    nm = 'llvm-nm'
    with tempfile.TemporaryDirectory(prefix='vermeer-pack-') as scratch_name:
        scratch = Path(scratch_name)
        shim = compile_shim(Path(uefi_dir), scratch, clang, objcopy, nm)
        combined = shim + fd
        tree = Path(dtb_path).read_bytes()
        kernel = gzip.compress(combined, mtime=0) + tree

        kernel_file = scratch / 'Image.gz-dtb'
        kernel_file.write_bytes(kernel)
        ramdisk = scratch / 'ramdisk'
        ramdisk.write_bytes(b'')

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            sys.executable, str(mkbootimg),
            '--kernel', str(kernel_file),
            '--ramdisk', str(ramdisk),
            '--base', hex(BASE),
            '--kernel_offset', hex(KERNEL_OFFSET),
            '--tags_offset', hex(TAGS_OFFSET),
            '--pagesize', str(PAGESIZE),
            '--header_version', str(HEADER_VERSION),
            '-o', str(out_path)], check=True)

        report = verify(out_path, combined, tree)
    report.update({
        'fd_sha256': hashlib.sha256(fd).hexdigest(),
        'dtb_sha256': hashlib.sha256(tree).hexdigest(),
        'shim_bytes': len(shim),
        'image_bytes': out_path.stat().st_size,
        'image_sha256': hashlib.sha256(out_path.read_bytes()).hexdigest(),
        'fd_base': hex(FD_BASE),
        'fd_size': hex(FD_SIZE),
        'hardware_tested': False,
    })
    return report


def verify(path, combined, tree):
    """Unpack the boot image and compare every part against its input."""
    data = Path(path).read_bytes()
    if data[:8] != b'ANDROID!':
        raise SystemExit('not an Android boot image')
    (kernel_size, kernel_address, ramdisk_size, _, second_size, _, tags, page,
     version, _) = struct.unpack_from('<10I', data, 8)
    if second_size or version != HEADER_VERSION or page != PAGESIZE:
        raise SystemExit('boot image header does not match the requested parameters')
    if kernel_address != BASE + KERNEL_OFFSET or tags != BASE + TAGS_OFFSET:
        raise SystemExit('boot image load addresses do not match the requested offsets')

    import zlib
    kernel = data[page:page + kernel_size]
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    payload = unpacker.decompress(kernel)
    if payload != combined:
        raise SystemExit('the gzip stream does not hold shim + firmware')
    if not unpacker.eof:
        raise SystemExit('the gzip stream is truncated')
    if unpacker.unused_data != tree:
        raise SystemExit('the appended device tree differs from the input')

    start = page * (1 + -(-kernel_size // page))
    if data[start:start + ramdisk_size] != b'':
        raise SystemExit('unexpected ramdisk bytes')
    return {'kernel_sha256': hashlib.sha256(kernel).hexdigest(),
            'kernel_bytes': kernel_size, 'ramdisk_bytes': ramdisk_size,
            'header_version': version, 'page_size': page,
            'kernel_address': hex(kernel_address), 'tags_address': hex(tags)}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fd', required=True, help='SILICIUM_UEFI.fd')
    parser.add_argument('--uefi', required=True, help='Mu-Silicium checkout (BootShim, mkbootimg)')
    parser.add_argument('--dtb', required=True, help='device tree with splash_region')
    parser.add_argument('--out', default='boot.img')
    parser.add_argument('--report', default=None, help='JSON report path (default: alongside)')
    args = parser.parse_args()

    uefi = Path(args.uefi)
    report = pack(args.fd, uefi, args.dtb, args.out, uefi / 'Resources/Scripts/mkbootimg.py')
    out = Path(args.out)
    report_path = Path(args.report) if args.report else out.with_suffix('.json')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'wrote {out} ({report["image_bytes"]} bytes)')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
