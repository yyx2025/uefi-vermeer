#!/usr/bin/env python3
"""End-to-end check of a vermeer boot image: boot.img -> FD -> Chinese UI file.

Reads the Android boot image, inflates the kernel, splits shim/firmware/DTB,
decompresses the inner FVMAIN, and asserts the Chinese UI application and its
registrar are present. Proves the shipped image really carries the UI, rather
than trusting the build.

Usage: python verify_boot_image.py boot.img [--expect-sha256 HASH]
"""
import hashlib
import lzma
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fv_list  # noqa: E402

FD_SIZE = 0x300000
CHINESE_UI_APP = '7e2b4c61-9a35-4f8d-b1e7-3c5a90d24f18'
CHINESE_UI_DXE = '3a9c51d7-6e24-4b08-9f31-58d7a2e6c4b9'
SPLASH_NODE = '/reserved-memory/splash_region'


def split_kernel(data, page, kernel_size):
    """Return (kernel bytes, appended DTB or None)."""
    kernel = data[page:page + kernel_size]
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    payload = unpacker.decompress(kernel)
    if not unpacker.eof:
        raise SystemExit('gzip stream is truncated')
    return payload, (unpacker.unused_data or None)


def extract_fd(payload):
    """BootShim.bin is 192 bytes; the firmware volume follows it."""
    fd = payload[192:]
    if len(fd) != FD_SIZE:
        raise SystemExit(f'firmware is {len(fd):#x} bytes, expected {FD_SIZE:#x}')
    return fd


def inner_volume(fd):
    """Find the GUIDed LZMA section that wraps FVMAIN and decompress it."""
    marker = bytes.fromhex('93fd219e729c154c8c4be77f1db2d792')  # FVMAIN_COMPACT file
    offset = fd.find(marker)
    if offset < 0:
        raise SystemExit('FVMAIN_COMPACT file not found in the firmware volume')
    section = offset + 24
    size = fd[section] | (fd[section + 1] << 8) | (fd[section + 2] << 16)
    if fd[section + 3] != 0x02:
        raise SystemExit('FVMAIN_COMPACT is not a GUIDed section')
    data_offset = struct.unpack_from('<H', fd, section + 20)[0]
    start = section + data_offset
    return lzma.decompress(fd[start:section + size], format=lzma.FORMAT_ALONE)


def main(path, expected_sha=None):
    data = Path(path).read_bytes()
    if data[:8] != b'ANDROID!':
        raise SystemExit('not an Android boot image')
    (kernel_size, kernel_addr, ramdisk_size, _, second_size, _, tags, page,
     version, _) = struct.unpack_from('<10I', data, 8)
    print(f'{path}')
    print(f'  image      {len(data):,} bytes, header v{version}, page {page}')
    print(f'  kernel     {kernel_size:,} bytes at {kernel_addr:#x}, tags {tags:#x}')
    print(f'  ramdisk    {ramdisk_size} bytes')

    digest = hashlib.sha256(data).hexdigest()
    print(f'  sha256     {digest}')
    if expected_sha and digest != expected_sha:
        raise SystemExit(f'sha256 mismatch: expected {expected_sha}')

    payload, tree = split_kernel(data, page, kernel_size)
    fd = extract_fd(payload)
    print(f'  firmware   {len(fd):,} bytes (fd sha256 {hashlib.sha256(fd).hexdigest()[:16]}...)')

    if tree:
        if tree[:4] != b'\xd0\x0d\xfe\xed':
            raise SystemExit('appended blob is not an FDT')
        from fdt_tool import Fdt
        fdt = Fdt(tree)
        if fdt.find(SPLASH_NODE) is None:
            raise SystemExit(f'{SPLASH_NODE} missing from the appended DTB')
        print(f'  dtb        {len(tree):,} bytes, splash_region present')

    volume = inner_volume(fd)
    print(f'  inner FV   {len(volume):,} bytes')

    guids = set()
    for start in range(0, len(volume) - 72, 8):
        if volume[start + 40:start + 44] != b'_FVH':
            continue
        length = struct.unpack_from('<Q', volume, start + 32)[0]
        for entry in fv_list.walk_volume(volume, start, length, ''):
            guids.add(entry['guid'])

    app = CHINESE_UI_APP in guids
    dxe = CHINESE_UI_DXE in guids
    print(f'  中文界面应用   : {"present" if app else "MISSING"}')
    print(f'  中文界面注册器 : {"present" if dxe else "MISSING"}')
    return 0 if (app and dxe) else 1


if __name__ == '__main__':
    expected = None
    if '--expect-sha256' in sys.argv:
        expected = sys.argv[sys.argv.index('--expect-sha256') + 1]
    sys.exit(main(sys.argv[1], expected))
