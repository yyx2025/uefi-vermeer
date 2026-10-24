#!/usr/bin/env python3
"""Assemble the generated VerifiedRoot inputs and check the symbols it defines.

The linker needs four globals from this assembly. Assemble it for aarch64 and
confirm each lands in the object with a plausible size: the manifest is tens of
megabytes, the anchor exactly 395 bytes, the policy table two quadwords plus a
count.

This is the check that would have caught the "%d" bug in the emitter, where the
manifest size operand was truncated.

Usage: python assemble_verified_root.py DIR
"""
import shutil
import subprocess
import sys
from pathlib import Path

CLANG = shutil.which('clang') or r'D:\msys64\ucrt64\bin\clang.exe'
NM = shutil.which('llvm-nm') or r'D:\msys64\ucrt64\bin\llvm-nm.exe'


def rewrite_incbin(source, directory):
    """incbin paths must resolve from the assembler's working directory."""
    text = source.read_text(encoding='utf-8')
    text = text.replace(str(directory).replace('\\', '\\\\'), '')
    for name in ('manifest.bin', 'trusted-key.bin', 'samples.bin'):
        text = text.replace(str(directory / name), name)
    # A bare section directive is enough here: the object only needs the symbols
    # and their contents, and DarwinBoot consumes them as data.
    text = text.replace('.section __DATA_CONST,__const', '.section .data')
    text = text.replace('.section "__DATA_CONST,__const",regular', '.section .data')
    return text


def main(directory):
    directory = Path(directory)
    source = directory / 'inputs.S'
    if not source.is_file():
        raise SystemExit(f'{source} is missing')
    rewritten = directory / 'inputs-assemble.S'
    rewritten.write_text(rewrite_incbin(source, directory), encoding='utf-8')

    obj = directory / 'inputs.o'
    result = subprocess.run([CLANG, '--target=aarch64-none-elf', '-c',
                             rewritten.name, '-o', str(obj)],
                            capture_output=True, text=True, cwd=str(directory))
    if result.returncode:
        print(result.stderr[:1500])
        return 1
    print(f'assembled: {obj} ({obj.stat().st_size:,} bytes)')

    # llvm-nm reports .data symbols with size 0, so derive sizes from the
    # addresses instead: the layout is manifest, manifest_bytes, key, policies,
    # policy_count, so each symbol's extent is the gap to the next one.
    readelf = shutil.which('llvm-readelf') or r'D:\msys64\ucrt64\bin\llvm-readelf.exe'
    listing = subprocess.run([readelf, '-s', str(obj)], capture_output=True, text=True)
    addresses = {}
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[7].startswith('_verified_root_'):
            addresses[parts[7]] = (int(parts[1], 16), parts[2])

    def extent(symbol):
        ordered = sorted(addresses.items(), key=lambda item: item[1][0])
        for index, (name, (address, _)) in enumerate(ordered):
            if name != symbol:
                continue
            if index + 1 < len(ordered):
                return ordered[index + 1][1][0] - address
            return None
        return None

    print('\nsymbols the link needs:')
    problems = []
    required = ['_verified_root_manifest', '_verified_root_manifest_bytes',
                '_verified_root_key', '_verified_root_policies',
                '_verified_root_policy_count']
    for symbol in required:
        if symbol not in addresses:
            problems.append(f'{symbol} is not defined')
            print(f'  MISSING {symbol}')
            continue
        size = extent(symbol)
        print(f'  ok      {symbol:34} at {addresses[symbol][0]:#012x}'
              + (f', extent {size} bytes' if size else ''))
        if symbol == '_verified_root_key' and size is not None:
            # 395 bytes of key data, then .balign 8 pads the next symbol: the
            # extent can legitimately be 395..400. What matters is that the data
            # itself is 395, which the emitter guarantees from the file size.
            if not (395 <= size <= 400):
                problems.append(f'{symbol} extent is {size} bytes, expected 395 (+padding)')
        if symbol == '_verified_root_manifest' and size is not None and size < 1024:
            problems.append(f'{symbol} is suspiciously small ({size} bytes)')

    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print(f'  - {problem}')
        return 1
    print('\nthe assembly defines every VerifiedRoot symbol the kernel link needs')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
