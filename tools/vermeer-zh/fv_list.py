#!/usr/bin/env python3
"""List the FFS files inside a UEFI firmware volume (or a whole FD image).

Verifies that a build actually contains what was asked for: the vermeer
Chinese UI application and its registrar, by FILE_GUID.

FFS layout (PI spec):
  file header  EFI_FFS_FILE_HEADER (24 bytes, or 32 with the large-file
               extension): name GUID, integrity checksum, type, attributes,
               size[3], state. Size = 0xFFFFFF means "look at the big header".
  sections     EFI_COMMON_SECTION_HEADER (4 bytes, or 8 when size is 0xFFFFFF):
               size[3], type.

The volume is walked FFS by FFS; padding bytes (0xFF) are skipped. This is a
reader only: it reports what is there, it does not modify anything.
"""
import struct
import sys
from pathlib import Path

FFS_HEADER = 24
FFS_HEADER2 = 32
SECTION_HEADER = 4

FILE_TYPES = {
    0x01: 'RAW', 0x02: 'FREEFORM', 0x03: 'SECURITY_CORE', 0x04: 'PEI_CORE',
    0x05: 'DXE_CORE', 0x06: 'PEIM', 0x07: 'DRIVER', 0x08: 'COMBINED_PEIM_DRIVER',
    0x09: 'APPLICATION', 0x0A: 'SMM', 0x0B: 'FIRMWARE_VOLUME_IMAGE',
    0x0C: 'COMBINED_SMM_DXE', 0x0D: 'SMM_CORE', 0x0F: 'MM_STANDALONE',
    0x10: 'MM_CORE_STANDALONE', 0xF0: 'PAD',
}

SECTION_TYPES = {
    0x01: 'COMPRESSION', 0x02: 'GUID_DEFINED', 0x10: 'PE32', 0x11: 'PIC',
    0x12: 'TE', 0x13: 'DXE_DEPEX', 0x14: 'VERSION', 0x15: 'USER_INTERFACE',
    0x16: 'COMPATIBILITY16', 0x17: 'FV_IMAGE', 0x18: 'FREEFORM_SUBTYPE_GUID',
    0x19: 'RAW', 0x1B: 'PEI_DEPEX', 0x1C: 'SMM_DEPEX',
}

# The two modules added for the Chinese UI.
CHINESE_UI_APP = '7e2b4c61-9a35-4f8d-b1e7-3c5a90d24f18'
CHINESE_UI_DXE = '3a9c51d7-6e24-4b08-9f31-58d7a2e6c4b9'


def guid_str(raw):
    d1, d2, d3 = struct.unpack_from('<IHH', raw, 0)
    return f'{d1:08x}-{d2:04x}-{d3:04x}-' + raw[8:10].hex() + '-' + raw[10:16].hex()


def sections(data, offset, end):
    """Yield (type, offset, size) for the sections of one FFS file."""
    cursor = offset
    while cursor + SECTION_HEADER <= end:
        size = data[cursor] | (data[cursor + 1] << 8) | (data[cursor + 2] << 16)
        kind = data[cursor + 3]
        header = SECTION_HEADER
        if size == 0xFFFFFF:
            if cursor + 8 > end:
                return
            size = struct.unpack_from('<I', data, cursor + 4)[0]
            header = 8
        if size < header or cursor + size > end:
            return
        yield kind, cursor + header, size - header
        cursor += (size + 3) & ~3


def walk_volume(data, start, length, label):
    """Walk one FV's files and return a list of dicts."""
    if data[start:start + 4] != b'_FVH':
        # FV headers put the signature 40 bytes into the header.
        signature_at = data.find(b'_FVH', start, start + 64)
        if signature_at < 0:
            return []
        start = signature_at - 40
    if data[start:start + 4] == b'_FVH':
        start -= 0  # already a bare volume header
    files = []
    cursor = start + 72  # EFI_FIRMWARE_VOLUME_HEADER is 72 bytes with one block map
    end = start + length
    while cursor + FFS_HEADER <= end:
        if data[cursor] == 0xFF and data[cursor:cursor + 16] == b'\xff' * 16:
            # Padding: skip to the next 8-byte boundary and look for a header.
            cursor += 8
            continue
        name = guid_str(data[cursor:cursor + 16])
        kind = data[cursor + 18]
        size = data[cursor + 20] | (data[cursor + 21] << 8) | (data[cursor + 22] << 16)
        header = FFS_HEADER
        if size == 0xFFFFFF:
            if cursor + FFS_HEADER2 > end:
                break
            size = struct.unpack_from('<Q', data, cursor + 24)[0]
            header = FFS_HEADER2
        if kind == 0xF0 or size < header or cursor + size > end:
            cursor += 8
            continue
        body = cursor + header
        inner = [(SECTION_TYPES.get(k, f'{k:#04x}'), off, sz) for k, off, sz in
                 sections(data, body, cursor + size)]
        files.append({'guid': name, 'type': kind, 'size': size,
                      'offset': cursor, 'sections': inner})
        cursor += (size + 7) & ~7
    return files


def main(path):
    data = Path(path).read_bytes()
    print(f'{path}: {len(data):,} bytes')

    found_app = found_dxe = False
    total = 0
    for start in range(0, len(data) - 72, 8):
        if data[start + 40:start + 44] != b'_FVH':
            continue
        length = struct.unpack_from('<Q', data, start + 32)[0]
        volume_id = guid_str(data[start + 16:start + 32])
        files = walk_volume(data, start, length, volume_id)
        if not files:
            continue
        print(f'\nFV at {start:#x} size {length:#x} ({volume_id}) : {len(files)} files')
        for entry in files:
            total += 1
            kind = FILE_TYPES.get(entry['type'], f'{entry["type"]:#04x}')
            mark = ''
            if entry['guid'] == CHINESE_UI_APP:
                mark = '  <== 中文界面应用'
                found_app = True
            elif entry['guid'] == CHINESE_UI_DXE:
                mark = '  <== 中文界面注册器'
                found_dxe = True
            print(f'  {entry["guid"]}  {kind:12} {entry["size"]:>8} bytes{mark}')

    print(f'\ntotal files: {total}')
    print(f'Chinese UI application present: {found_app}')
    print(f'Chinese UI registrar present  : {found_dxe}')
    return 0 if (found_app and found_dxe) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
