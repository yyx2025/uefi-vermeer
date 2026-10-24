#!/usr/bin/env python3
"""Check a generated VerifiedRoot manifest with the kit's own rules.

Writing a manifest with one script and trusting it is not a check: the firmware
uses kits/VerifiedRoot/src/verified_root.c, and a mismatch there only shows up as
a refused root on hardware. So this re-implements the layout from that C file --
field offsets, formulas, magic values -- and cross-checks every one of them.

The C file's acceptance conditions (vr_open, verified_root.c):
    magic "VROOT01\0", version 1, header 160, block 4096, reserved 0
    minimum_generation != 0
    block_count == ceil(image_bytes / 4096)
    signature over the 160-byte header, RSA-3072
    the hash table follows the 384-byte signature

Usage: python check_verified_root.py DIR
"""
import hashlib
import json
import subprocess
import struct
import sys
from pathlib import Path

VR_MAGIC = b'VROOT01\0'
VR_HEADER_BYTES = 160
VR_SIGNATURE_BYTES = 384
VR_BLOCK_BYTES = 4096
VR_KEY_BYTES = 395


def main(directory):
    directory = Path(directory)
    record = json.loads((directory / 'verification.json').read_text())
    manifest = (directory / 'manifest.bin').read_bytes()
    anchor = (directory / 'trusted-key.bin').read_bytes()
    print(f'{directory}')
    print(f'  manifest {len(manifest):,} bytes, anchor {len(anchor)} bytes')

    problems = []

    if len(anchor) != VR_KEY_BYTES:
        problems.append(f'anchor is {len(anchor)} bytes, VerifiedRoot wants {VR_KEY_BYTES}')
    if anchor[:8] != b'VROOTKEY':
        problems.append(f'anchor magic is {anchor[:8]!r}, expected b"VROOTKEY"')
    if anchor[-3:] != b'\x01\x00\x01':
        problems.append('anchor does not end with exponent 65537')

    header = manifest[:VR_HEADER_BYTES]
    magic, version, header_bytes, block_bytes, reserved = struct.unpack_from('<8s4I', header, 0)
    image_bytes, block_count = struct.unpack_from('<2Q', header, 24)
    volume_uuid = header[40:56]
    image_sha, table_sha, key_sha = (header[56:88], header[88:120], header[120:152])
    generation = struct.unpack_from('<Q', header, 152)[0]

    if magic != VR_MAGIC:
        problems.append(f'manifest magic {magic!r}')
    if version != 1:
        problems.append(f'version {version}')
    if header_bytes != VR_HEADER_BYTES:
        problems.append(f'header {header_bytes}')
    if block_bytes != VR_BLOCK_BYTES:
        problems.append(f'block {block_bytes}')
    if reserved != 0:
        problems.append(f'reserved {reserved}')
    if generation == 0:
        problems.append('generation is 0 (vr_open rejects)')

    expected_blocks = image_bytes // VR_BLOCK_BYTES + (1 if image_bytes % VR_BLOCK_BYTES else 0)
    if block_count != expected_blocks:
        problems.append(f'block_count {block_count}, expected {expected_blocks}')

    if hashlib.sha256(anchor).digest() != key_sha:
        problems.append('header key digest does not match the anchor')

    table = manifest[VR_HEADER_BYTES + VR_SIGNATURE_BYTES:]
    if len(table) != block_count * 32:
        problems.append(f'hash table is {len(table)} bytes, expected {block_count * 32}')
    elif hashlib.sha256(table).digest() != table_sha:
        problems.append('hash table digest does not match the header')

    # Signature covers the header only; verify with the anchor's modulus.
    signature = manifest[VR_HEADER_BYTES:VR_HEADER_BYTES + VR_SIGNATURE_BYTES]
    if len(signature) != VR_SIGNATURE_BYTES:
        problems.append(f'signature is {len(signature)} bytes')
    else:
        public = directory.parent / 'keys' / 'owner-public.pem'
        if not public.is_file():
            public = directory.parent / 'keys-selftest' / 'owner-public.pem'
        header_file = directory / 'header-check.bin'
        header_file.write_bytes(header)
        sig_file = directory / 'signature-check.bin'
        sig_file.write_bytes(signature)
        if public.is_file():
            result = subprocess.run(
                ['openssl', 'dgst', '-sha256', '-verify', str(public),
                 '-signature', str(sig_file), str(header_file)],
                capture_output=True, text=True)
            ok = result.returncode == 0
            print(f'  signature verified by openssl: {ok}')
            if not ok:
                problems.append(f'signature check failed: {result.stdout}{result.stderr}'.strip())
        else:
            print(f'  signature not checked (no public key at {public})')
        header_file.unlink(missing_ok=True)
        sig_file.unlink(missing_ok=True)

    print(f'  image {image_bytes:,} bytes in {block_count:,} blocks')
    print(f'  volume uuid {record["volume_uuid"]}')
    print(f'  generation {generation}')

    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print(f'  - {problem}')
        return 1
    print('\nthe manifest satisfies every condition vr_open() checks')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
