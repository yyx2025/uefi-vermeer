#!/usr/bin/env python3
"""Fetch only the IPSW members the verified-root step needs, via HTTP ranges.

The full image is 6.8 GB; the members are a few hundred megabytes at most, and
the zip directory tells us exactly where each one sits. unixnu already has this
machinery (scripts/unixnu/ipsw.py, class RangeFile) but it opens URLs through
urllib's default opener, which picks up this host's machine-wide proxy -- and
that proxy is down (it accepts TCP then refuses the handshake).

So this script installs a direct opener before importing unixnu, then reuses the
real implementation rather than re-implementing range reads. Members land in the
same cache layout unixnu uses (out/ipsw/<build>/), so a later `unixnu ipsw fetch`
finds them already there.

Usage:
  python fetch_ipsw_members.py --out DIR [--members KernelCache,DeviceTree,...]
"""
import argparse
import os
import sys
import urllib.request
from pathlib import Path

UNIXNU = Path(r'D:\ios hack\src\UniXNU\scripts')
DEVICE = Path(r'D:\ios hack\src\UniXNU-vermeer')

# Must happen before unixnu's modules are imported: they call urlopen directly.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_DIRECT)
print('installed a direct opener: the machine-wide proxy is bypassed')

sys.path.insert(0, str(UNIXNU))
from unixnu import ipsw as ipsw_module  # noqa: E402
from unixnu.config import load_toml  # noqa: E402


class Args:
    """The subset of the unixnu context object Ipsw touches.

    Ipsw reads ctx.board (= board.toml), ctx.out_root (for the member cache) and
    ctx.ipsw (an explicit source). Everything else it needs comes from the board.
    """

    def __init__(self, device, out, source=None):
        self.device = Path(device)
        self.out_root = Path(out)
        self.out = str(out)
        self.ipsw = source
        self.skip_preflight = False
        self.board = load_toml(self.device / 'board.toml')

    def option_text(self, flag, env=None):
        """The CLI-flag/env accessor the unixnu context provides."""
        value = getattr(self, flag, None)
        if value:
            return value
        return os.environ.get(env) if env else None

    def option(self, flag, env=None):
        value = self.option_text(flag, env)
        return Path(value) if value else None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default=r'D:\ios hack\work\ipsw-cache')
    parser.add_argument('--members', default='KernelCache,DeviceTree,StaticTrustCache')
    args = parser.parse_args()

    board = load_toml(DEVICE / 'board.toml')
    spec = board['ipsw']
    print(f'target: {spec["product"]} {spec["version"]} ({spec["build"]})')
    print(f'url: {spec["url"]}')

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    context = Args(DEVICE, out)

    source = ipsw_module.Ipsw(context)
    print(f'opened remotely: size {source.remote} / local {not source.remote}')

    wanted = [m for m in args.members.split(',') if m]
    print(f'members: {", ".join(wanted)}\n')
    for name in wanted:
        path = source.extract(name)
        print(f'  {name:20} -> {path}  ({Path(path).stat().st_size:,} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
