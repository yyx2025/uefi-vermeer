#!/usr/bin/env python3
"""Check that every character the UI prints exists in the generated font.

This is the check that should have existed when the font was generated: the
glyph set comes from cjk-charset.txt, and if a label gains a character that the
charset file lacks, the firmware renders an empty box for it. Nothing else
catches that -- not the C compiler, not the CI build, not the FV listing.

Exit code 0 when the font covers the UI, 1 otherwise, with the missing
characters listed so cjk-charset.txt can be fixed.
"""
import re
import sys
from pathlib import Path

PKG = Path(r'D:\ios hack\src\uefi-vermeer\Platforms\Xiaomi\vermeerPkg'
           r'\Drivers\VermeerChineseUi')
UI = PKG / 'VermeerChineseUi.c'
FONT = PKG / 'VermeerCjkFontData.c'
CHARSET = Path(r'D:\ios hack\work\zhbuild\cjk-charset.txt')


def ui_characters():
    """Every non-ASCII character appearing in a wide string literal."""
    text = UI.read_text(encoding='utf-8')
    literals = re.findall(r'L"((?:[^"\\]|\\.)*)"', text)
    needed = set()
    for literal in literals:
        for char in literal:
            if ord(char) > 0x7E:
                needed.add(char)
    return needed, len(literals)


def font_characters():
    text = FONT.read_text(encoding='utf-8')
    match = re.search(r'mCjkCodepoints\[\d+\] = \{(.*?)\n\};', text, re.S)
    if not match:
        raise SystemExit('mCjkCodepoints not found in the font source')
    return {chr(int(v, 16)) for v in re.findall(r'0x([0-9A-Fa-f]{4})', match.group(1))}


def main():
    needed, literal_count = ui_characters()
    have = font_characters()
    missing = sorted(needed - have)
    unused = sorted(have - needed)

    print(f'UI: {literal_count} wide literals, {len(needed)} distinct CJK characters')
    print(f'font: {len(have)} CJK glyphs')
    if missing:
        print(f'\nMISSING {len(missing)} glyph(s) -- these render as empty boxes:')
        for char in missing:
            print(f'  U+{ord(char):04X}  {char}')
    else:
        print('\nall UI characters have glyphs')
    if unused:
        print(f'\n{len(unused)} unused glyph(s): {"".join(unused)}')

    if CHARSET.is_file():
        charset = CHARSET.read_text(encoding='utf-8')
        not_listed = [c for c in missing if c not in charset]
        if not_listed:
            print(f'\nadd these to {CHARSET.name}: {"".join(not_listed)}')
    return 0 if not missing else 1


if __name__ == '__main__':
    sys.exit(main())
