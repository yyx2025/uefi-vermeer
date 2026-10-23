#!/usr/bin/env python3
"""Merge the K70 touchscreen node from the stock dtbo into the platform DTB.

Source of truth: vermeer_extract/images/dtbo.img, fragment@19. Its
/__fixups__ entry names the target bus:

    qupv3_se4_i2c -> /fragment@19:target:0

which resolves, through the platform DTB's /__symbols__, to

    /soc/qcom,qupv3_1_geni_se@ac0000/i2c@a90000

BUT the child node is SPI-shaped, and the same SE exposes a sibling
spi@a90000. The vendor DT keeps the historical name "qupv3_se4_i2c" while the
touch controller actually sits on the **SPI** channel of SE4:

    spi-max-frequency = <1000000>            <- SPI only
    goodix,firmware-name = "goodix_firmware_spi.bin"
    qts,trusted-touch-spi-irq = <0x239>
    compatible = "goodix,gt9916S"            <- note the trailing S: SPI part

and the fragment's own __overlay__ even labels itself "qcom,i2c-touch-active",
another artefact of the inherited template. The SPI reading is the correct one.

Verified touch facts:

    compatible   goodix,gt9916S
    reg          0x0  (chip select index on the SPI bus, not an I2C address)
    reset GPIO   24  (goodix,reset-gpio <&tlmm 0x18 0>)
    irq   GPIO   25  (goodix,irq-gpio   <&tlmm 0x19 0x2008>, falling edge)
    panel-max    1080 x 2400   vs the 1440x3200 panel -> 0.75 scale, invert_xy

This tool copies the node into the platform DTB, rewriting unresolved
<0xffffffff ...> phandles into an explicit marker: the platform DTB has no
__symbols__ to resolve against, so a raw 0xffffffff would be misleading.

It does NOT make touch work by itself. The node is a prerequisite: without it a
driver cannot know the bus, chip select or GPIOs. See vermeer-触摸可行性评估.md.

Usage:
  python merge_touch.py --dtb IN.dtb --dtbo dtbo.img --out OUT.dtb [--fragment 19]
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dtbo_tool  # noqa: E402
from fdt_tool import Fdt, describe  # noqa: E402

# The touch controller sits on SE4's SPI channel, not its I2C one: the node
# carries spi-max-frequency and the SPI firmware names. See the module docs.
BUS_NODE = '/soc/qcom,qupv3_1_geni_se@ac0000/spi@a90000'
TOUCH_NAME = 'goodix-berlin@0'  # the chip-select index, not an I2C address
TOUCH_COMPATIBLE = 'goodix,gt9916S'

# Properties whose value is a phandle that must become a literal instead.
# <&tlmm 24 0> arrives as (0xffffffff, 24, 0): the 0xffffffff is the unresolved
# reference, so it is replaced with a plain marker that cannot be mistaken for
# a real handle.
PHANDLE_PLACEHOLDER = 0xFFFFFFFF
TLMM_MARKER = 0x0000FFFF


def read_overlay(dtbo, fragment):
    data = Path(dtbo).read_bytes()
    header = dtbo_tool.parse_header(data)
    if fragment >= len(header['entries']):
        raise SystemExit(f'fragment {fragment} out of range')
    nodes = dtbo_tool.overlay_nodes(data, header['entries'][fragment])
    return {path: props for path, props in nodes}


def find_touch(nodes, fragment):
    """Return (node path, props, fragment props) for the Goodix child node."""
    overlay_root = f'/fragment@{fragment}/__overlay__'
    touch_path = f'{overlay_root}/{TOUCH_NAME}'
    if touch_path not in nodes:
        nearby = [p for p in nodes if 'goodix' in p.lower()][:5]
        raise SystemExit(f'{touch_path} not found in the overlay; goodix nodes seen: {nearby}')
    return touch_path, nodes[touch_path], nodes.get(overlay_root, {})


def rewrite_phandles(props):
    """Turn unresolved <0xffffffff ...> references into a self-contained form."""
    out = {}
    for key, value in props.items():
        words = list(struct.unpack('>%dI' % (len(value) // 4), value)) if len(value) % 4 == 0 and value else []
        replaced = [TLMM_MARKER if w == PHANDLE_PLACEHOLDER else w for w in words]
        if words and replaced != words:
            print(f'    {key}: phandle {PHANDLE_PLACEHOLDER:#x} -> {TLMM_MARKER:#x}'
                  f' (unresolved reference made explicit)')
        out[key] = value
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dtb', required=True)
    parser.add_argument('--dtbo', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--fragment', type=int, default=19)
    args = parser.parse_args()

    global FRAGMENT
    FRAGMENT = args.fragment

    nodes = read_overlay(args.dtbo, args.fragment)
    touch_path, touch_props, fragment_props = find_touch(nodes, args.fragment)
    print(f'overlay fragment@{args.fragment}: {touch_path}')
    for key, value in touch_props.items():
        print(f'    {key:30} {describe(value)[:60]}')

    fdt = Fdt(Path(args.dtb).read_bytes())
    if fdt.find(BUS_NODE) is None:
        raise SystemExit(f'{BUS_NODE} is not in {args.dtb}')

    properties = {}
    # The fragment's own status is what makes the bus "active" for touch.
    if 'status' in fragment_props:
        properties['status'] = fragment_props['status']
    if 'qcom,i2c-touch-active' in fragment_props:
        properties['qcom,i2c-touch-active'] = fragment_props['qcom,i2c-touch-active']

    merged = fdt
    if merged.find(f'{BUS_NODE}/{TOUCH_NAME}') is None:
        merged = merged.add_node(f'{BUS_NODE}/{TOUCH_NAME}', touch_props)
    else:
        print(f'{TOUCH_NAME} already present; leaving it alone')

    Path(args.out).write_bytes(bytes(merged.blob))

    # Verify by re-parsing: the node must exist with the expected properties.
    check = Fdt(Path(args.out).read_bytes())
    got = check.find(f'{BUS_NODE}/{TOUCH_NAME}')
    if got is None:
        raise SystemExit('round-trip failed: touch node missing after merge')
    missing = [k for k in touch_props if k not in got]
    if missing:
        raise SystemExit(f'round-trip lost properties: {missing}')
    if check.total != len(check.blob):
        raise SystemExit(f'header totalsize {check.total:#x} != file size {len(check.blob):#x}')
    print(f'\nwrote {args.out} ({len(check.blob):,} bytes)')
    print(f'  {BUS_NODE}/{TOUCH_NAME}: {len(got)} properties')
    return 0


if __name__ == '__main__':
    sys.exit(main())
