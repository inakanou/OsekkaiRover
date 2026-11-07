"""顔を覚える・消す・一覧のコマンドの口（`python -m osekkai.features.presence`。5.x）。"""

from osekkai.features.presence.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
