"""Make the phone web app's icons (docs/icons/icon-180.png, icon-192.png, icon-512.png) from assets/icon.ico.

Run from anywhere; paths are relative to this repository:
    set QT_QPA_PLATFORM=offscreen
    python tools/make_icons.py            # writes only icons that do not exist yet
    python tools/make_icons.py --force    # also replaces existing icons

The icon is drawn centred on the app's cream background (#FFF7EF), because iOS shows transparent
corners of a Home Screen icon as black.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "icon.ico"
OUT_DIR = ROOT / "docs" / "icons"
SIZES = (180, 192, 512)
BACKGROUND = "#FFF7EF"
FILL = 0.84  # share of the square the artwork covers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="replace icons that already exist")
    args = ap.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841 (Qt needs it alive)
    if not SOURCE.exists():
        print(f"missing {SOURCE.relative_to(ROOT)}", file=sys.stderr)
        return 1
    icon = QIcon(str(SOURCE))
    available = icon.availableSizes()
    if not available:
        print("could not read any image from the .ico file", file=sys.stderr)
        return 1
    biggest = max(available, key=lambda s: s.width() * s.height())
    art = icon.pixmap(biggest).toImage()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        out = OUT_DIR / f"icon-{size}.png"
        if out.exists() and not args.force:
            print(f"kept {out.relative_to(ROOT)} (exists; use --force to replace)")
            continue
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(QColor(BACKGROUND))
        p = QPainter(img)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        side = size * FILL
        scaled = art.scaled(int(side), int(side), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (size - scaled.width()) / 2
        y = (size - scaled.height()) / 2
        p.drawImage(QRectF(x, y, scaled.width(), scaled.height()), scaled)
        p.end()
        if not img.save(str(out), "PNG"):
            print(f"could not write {out.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print(f"wrote {out.relative_to(ROOT)} ({size}x{size}, from a {biggest.width()}px source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
