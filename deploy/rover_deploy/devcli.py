"""開発 PC の入口（`rover-deploy`）。今は引数の解析だけで、サブコマンドは後で足す。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

USAGE_ERROR = 2  # argparse の使い方の誤りと同じ


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="rover-deploy",
        description="開発 PC から、実機（PiCar-X）に版を入れる道具",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # サブコマンドがまだないので、使い方を出して終える
    parser.print_usage(sys.stderr)
    print("rover-deploy: サブコマンドを指定してください", file=sys.stderr)
    return USAGE_ERROR
