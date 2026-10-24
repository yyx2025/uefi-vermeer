#!/usr/bin/env python3
"""Report how xnu boot entries are configured, and whether they trip DarwinBoot.

DarwinBoot refuses a request that carries a device tree:

    if (KextCount != 0 || Request->DeviceTree != NULL || ...) return EFI_UNSUPPORTED;

UniXNU passes `Request.DeviceTree = Fdt`, and Fdt is only non-NULL when the entry
sets `dtb`. So an xnu entry must NOT set dtb -- the tree DarwinBoot wants is the
`.adt` sidecar next to the kernel image, not an FDT from the configuration.

This scans the configuration files and reports any xnu entry that would be
rejected, which is exactly the mistake that produces
"DARWINBOOT: unsupported inputs or exception level; firmware retained".
"""
import re
import sys
from pathlib import Path

ROOTS = [Path(r'D:\ios hack\src')]


def scan(path):
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    if 'type' not in text:
        return []

    entries = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r'id\s*=\s*"([^"]*)"', stripped)
        if match:
            if current:
                entries.append(current)
            current = {'id': match.group(1), 'file': path, 'dtb': None, 'type': None}
            continue
        if current is None:
            continue
        for key in ('type', 'dtb', 'path'):
            found = re.match(rf'{key}\s*=\s*"([^"]*)"', stripped)
            if found:
                current[key] = found.group(1)
    if current:
        entries.append(current)
    return [e for e in entries if e['type'] == 'xnu']


def main():
    xnu_entries = []
    for root in ROOTS:
        for path in root.rglob('*.toml'):
            xnu_entries.extend(scan(path))

    print(f'xnu boot entries found: {len(xnu_entries)}')
    bad = 0
    for entry in xnu_entries:
        name = entry['file'].name
        dtb = entry['dtb']
        if dtb is None:
            print(f'  OK    {name}: id "{entry["id"]}" sets no dtb')
        else:
            print(f'  REJECT {name}: id "{entry["id"]}" sets dtb = "{dtb}"')
            print('         DarwinBoot refuses a request carrying a device tree;')
            print('         the tree it needs is the .adt sidecar next to the kernel.')
            bad += 1

    if not xnu_entries:
        print('\nNo xnu entry configured yet: the vermeer image currently boots')
        print('UniXNU (an efi entry). Adding an iOS kernel means adding one.')
    print(f'\n{"no conflicts" if bad == 0 else f"{bad} entry/entries would be rejected"}')
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
