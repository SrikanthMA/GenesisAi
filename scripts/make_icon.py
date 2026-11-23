"""Render the Genesis orb to assets/genesis.ico and assets/genesis.png (used by the installer/build)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

from genesis.ui.orb import paint_orb  # noqa: E402


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    img = QImage(256, 256, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    paint_orb(p, QRectF(0, 0, 256, 256), "IDLE", 0.8)
    p.end()
    img.save(str(out / "genesis.png"))
    img.save(str(out / "genesis.ico"))
    print(f"wrote {out / 'genesis.ico'}")


if __name__ == "__main__":
    main()
