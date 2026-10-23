#!/usr/bin/env python3
"""Trim the generated font to exactly the glyphs the UI prints.

The charset file is a superset kept for convenience while the menu evolves; the
committed VermeerCjkFontData.c should carry only what is used, because every
unused CJK glyph is 32 bytes of firmware volume for nothing.

This regenerates from the union of (used characters) + ASCII, and is safe to run
repeatedly: it reads the used set from the C source, not from the charset file.
"""
import re
import subprocess
import sys
from pathlib import Path

PKG = Path(r'D:\ios hack\src\uefi-vermeer\Platforms\Xiaomi\vermeerPkg'
           r'\Drivers\VermeerChineseUi')
UI = PKG / 'VermeerChineseUi.c'
FONT = PKG / 'VermeerCjkFontData.c'
GEN = Path(r'D:\ios hack\work\zhbuild\gen-cjk-font.py')
UNIFONT = Path(r'D:\ios hack\work\zhbuild\unifont.hex.gz')


def used_characters():
    text = UI.read_text(encoding='utf-8')
    used = set()
    for literal in re.findall(r'L"((?:[^"\\]|\\.)*)"', text):
        used.update(literal)
    return {c for c in used if ord(c) > 0x7E}


def font_size(path):
    text = path.read_text(encoding='utf-8')
    return len(text)


def glyph_count(path):
    text = path.read_text(encoding='utf-8')
    match = re.search(r'mCjkCodepoints\[\d+\] = \{(.*?)\n\};', text, re.S)
    return len(re.findall(r'0x[0-9A-Fa-f]{4}', match.group(1))) if match else 0


def main():
    chars = used_characters()
    before_size, before_count = font_size(FONT), glyph_count(FONT)
    print(f'used CJK characters: {len(chars)}')
    print(f'font before: {before_count} glyphs, {before_size:,} bytes of source')

    charset = Path(r'D:\ios hack\work\zhbuild\charset-used.txt')
    charset.write_text(''.join(sorted(chars)), encoding='utf-8')

    result = subprocess.run(
        [sys.executable, str(GEN), '--hex', str(UNIFONT),
         '--extra-file', str(charset),
         '--chars', '0123456789:.-/%()[]<>',
         '--out', str(FONT)], capture_output=True)
    print(result.stdout.decode('utf-8', 'replace').strip())
    if result.returncode:
        print(result.stderr.decode('utf-8', 'replace')[:800])
        return 1

    after_size, after_count = font_size(FONT), glyph_count(FONT)
    print(f'font after:  {after_count} glyphs, {after_size:,} bytes of source')
    print(f'removed {before_count - after_count} glyphs, '
          f'{before_size - after_size:,} bytes of source')
    return 0


if __name__ == '__main__':
    sys.exit(main())
