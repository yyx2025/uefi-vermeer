#!/usr/bin/env python3
"""Render a generated font header to a PNG, to eyeball the glyphs.

Reads the C header produced by gen-cjk-font.py (the tables are plain numbers),
draws every CJK and a sample of ASCII glyphs on a grid, and writes a PNG.
"""
import re
import sys
from pathlib import Path

from PIL import Image


def parse_arrays(text, name):
    """Return a list of glyph byte-lists for the named C array."""
    # C arrays here may have more than one dimension: mName[95][16] = {
    match = re.search(rf'm{name}(?:\s*\[[^\]]*\])+\s*=\s*\{{', text)
    if not match:
        return []
    start = match.end()
    depth = 1
    index = start
    while index < len(text) and depth:
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
        index += 1
    body = text[start:index]
    out = []
    for row in re.finditer(r'\{([^{}]*)\}', body):
        values = [int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', row.group(1))]
        out.append(values)
    return out


def draw(glyph, width, height, scale=3):
    """One glyph as a black-on-white PIL image."""
    image = Image.new('1', (width, height), 1)
    pixels = image.load()
    stride = width // 8
    for y in range(height):
        for byte_index in range(stride):
            byte = glyph[y * stride + byte_index] if y * stride + byte_index < len(glyph) else 0
            for bit in range(8):
                if byte & (0x80 >> bit):
                    pixels[byte_index * 8 + bit, y] = 0
    return image.resize((width * scale, height * scale), Image.NEAREST)


def main(path, out):
    text = Path(path).read_text(encoding='utf-8')
    ascii_glyphs = parse_arrays(text, 'AsciiGlyphs')
    cjk_glyphs = parse_arrays(text, 'CjkGlyphs')
    print(f'{path}: {len(ascii_glyphs)} ascii glyphs, {len(cjk_glyphs)} cjk glyphs')

    if not cjk_glyphs:
        print('no CJK glyphs to draw')
        return 1

    # CJK: 32 bytes each (16 rows x 2 bytes), ASCII: 16 bytes each (16 rows x 1).
    cjk_flat = [g for g in cjk_glyphs]
    ascii_flat = [g for g in ascii_glyphs]

    scale = 3
    cols = 10
    cell_w, cell_h = 16 * scale + 8, 16 * scale + 8
    rows = (len(cjk_flat) + cols - 1) // cols + 2
    canvas = Image.new('1', (cols * cell_w, rows * cell_h), 1)

    for index, glyph in enumerate(cjk_flat[:60]):
        x = (index % cols) * cell_w + 4
        y = (index // cols) * cell_h + 4
        canvas.paste(draw(glyph[:32], 16, 16, scale), (x, y))

    start = (len(cjk_flat[:60]) + cols - 1) // cols
    for index, glyph in enumerate(ascii_flat[:40]):
        x = (index % cols) * cell_w + 4
        y = (start + index // cols) * cell_h + 4
        canvas.paste(draw(glyph[:16], 8, 16, scale), (x, y))

    canvas.save(out)
    print(f'wrote {out} ({canvas.width}x{canvas.height})')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
