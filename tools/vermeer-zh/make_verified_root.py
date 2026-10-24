#!/usr/bin/env python3
"""Generate the VerifiedRoot anchor data the kernel link needs.

What is missing from the link, and what it actually is
-----------------------------------------------------
NativeUfs is compiled with -DNATIVE_VERIFIED_BLOCK=1 unconditionally
(scripts/build-arm-driver.py line 79), so it references VerifiedRoot symbols.
Two of them are data, not code:

  _verified_root_key          395 bytes: b'VROOTKEY' + RSA-3072 modulus + 65537
  _verified_root_policies     [manifest pointer, size] pairs
  _verified_root_policy_count how many pairs

kits/VerifiedRoot/tools/manifest.py builds that data from an image: it hashes the
image in 4 KiB blocks, signs the ordered hash table with an RSA-3072 key, and
writes header/signature/hashes into a `manifest.bin` envelope. The key is a
*local owner anchor* (public_anchor() only demands a real 3072-bit key with
e=65537) -- not an Apple key. That is what makes this possible offline.

What image to sign
------------------
The anchor covers whatever bytes the block device serves. For the OS volume that
is 044-76034-038.dmg. This tool signs the DMG *as stored* by default, because
that is the data a block driver reading the partition would see; pass
--raw-volume to sign a decompressed image instead if the driver is expected to
serve plain blocks.

Cost: 3.3 GB at 4 KiB per block is ~806k hashes, a few minutes of SHA-256.

Usage:
  python make_verified_root.py --image FILE --volume-uuid UUID --out DIR
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path

WORK = Path(r'D:\ios hack\work\verified-root')
OPENSSL = shutil.which('openssl')
VR_BLOCK = 4096
VR_KEY_BYTES = 395
VR_SIGNATURE_BYTES = 384


def make_owner_keys(directory):
    """One RSA-3072 pair; public_anchor() requires exactly this shape."""
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / 'owner.pem'
    public = directory / 'owner-public.pem'
    if not private.is_file():
        subprocess.run([OPENSSL, 'genrsa', '-out', str(private), '3072'],
                       check=True, capture_output=True)
    if not public.is_file():
        subprocess.run([OPENSSL, 'rsa', '-in', str(private), '-pubout', '-out', str(public)],
                       check=True, capture_output=True)
    return private, public


def public_anchor(public_key):
    """Exactly the encoding manifest.py's public_anchor() produces."""
    text = subprocess.check_output(
        [OPENSSL, 'rsa', '-pubin', '-in', str(public_key), '-text', '-noout'], text=True)
    if '65537' not in text:
        raise SystemExit('the key does not use exponent 65537')
    line = subprocess.check_output(
        [OPENSSL, 'rsa', '-pubin', '-in', str(public_key), '-modulus', '-noout'], text=True)
    modulus = bytes.fromhex(line.strip().split('=', 1)[1])
    if len(modulus) != 384 or not modulus[0] & 128 or not modulus[-1] & 1:
        raise SystemExit('the key is not a real 3072-bit RSA modulus')
    anchor = b'VROOTKEY' + modulus + b'\x01\x00\x01'
    if len(anchor) != VR_KEY_BYTES:
        raise SystemExit(f'anchor is {len(anchor)} bytes, VerifiedRoot expects {VR_KEY_BYTES}')
    return anchor


def sign_image(image, key, anchor, volume_uuid, generation, out_dir):
    """Mirror kits/VerifiedRoot/tools/manifest.py, streaming so 3.3 GB is fine."""
    out_dir.mkdir(parents=True, exist_ok=True)
    image_bytes = image.stat().st_size
    hashes = out_dir / 'hashes.bin'
    image_hash = hashlib.sha256()
    table_hash = hashlib.sha256()
    count = 0

    with image.open('rb') as stream, hashes.open('wb') as table:
        remaining = image_bytes
        while remaining:
            chunk = stream.read(min(VR_BLOCK, remaining))
            if not chunk:
                raise SystemExit('image ended before its declared size')
            digest = hashlib.sha256(chunk).digest()
            image_hash.update(chunk)
            table_hash.update(digest)
            table.write(digest)
            remaining -= len(chunk)
            count += 1

    header = struct.pack(
        '<8s4I2Q16s32s32s32sQ', b'VROOT01\0', 1, 160, VR_BLOCK, 0,
        image_bytes, count, uuid.UUID(str(volume_uuid)).bytes,
        image_hash.digest(), table_hash.digest(), hashlib.sha256(anchor).digest(), generation)
    (out_dir / 'header.bin').write_bytes(header)
    (out_dir / 'trusted-key.bin').write_bytes(anchor)

    signature = out_dir / 'signature.bin'
    subprocess.run([OPENSSL, 'dgst', '-sha256', '-sign', str(key),
                    '-out', str(signature), str(out_dir / 'header.bin')], check=True)
    if len(signature.read_bytes()) != VR_SIGNATURE_BYTES:
        raise SystemExit('unexpected RSA signature size')

    verified = subprocess.run(
        [OPENSSL, 'dgst', '-sha256', '-verify', str(key.parent / 'owner-public.pem'),
         '-signature', str(signature), str(out_dir / 'header.bin')],
        capture_output=True, text=True)
    if verified.returncode:
        raise SystemExit(f'signature did not verify: {verified.stdout}{verified.stderr}')

    manifest = out_dir / 'manifest.bin'
    with manifest.open('wb') as envelope:
        envelope.write(header)
        envelope.write(signature.read_bytes())
        with hashes.open('rb') as table:
            for chunk in iter(lambda: table.read(1 << 20), b''):
                envelope.write(chunk)

    record = {
        'version': 1, 'block_bytes': VR_BLOCK, 'image_bytes': image_bytes,
        'block_count': count, 'volume_uuid': str(volume_uuid).upper(),
        'generation': generation, 'image_sha256': image_hash.hexdigest(),
        'hash_table_sha256': table_hash.hexdigest(),
        'trusted_key_sha256': hashlib.sha256(anchor).hexdigest(),
        'signature_algorithm': 'RSA3072-PKCS1v1.5-SHA256',
        'signature_verified_by_openssl': True,
        'manifest_bytes': manifest.stat().st_size,
        'image_modified': False, 'hardware_tested': False,
        'boot_root_authenticated': False,
    }
    (out_dir / 'verification.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--image', required=True)
    parser.add_argument('--volume-uuid', default='04769D26-2CA0-4825-B443-E2E5F88300F2')
    parser.add_argument('--generation', type=int, default=1)
    parser.add_argument('--out', default=str(WORK / 'os'))
    parser.add_argument('--keys', default=str(WORK / 'keys'))
    args = parser.parse_args()

    image = Path(args.image)
    if not image.is_file():
        raise SystemExit(f'{image} is not a file')
    print(f'image: {image} ({image.stat().st_size:,} bytes)')

    key, public = make_owner_keys(Path(args.keys))
    print(f'owner key: {key}')
    anchor = public_anchor(public)
    print(f'anchor: {len(anchor)} bytes, sha256 {hashlib.sha256(anchor).hexdigest()[:16]}')

    record = sign_image(image, key, anchor, args.volume_uuid,
                        args.generation, Path(args.out))
    print(json.dumps(record, indent=2))
    print(f'\nwrote {Path(args.out)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
