from __future__ import annotations

import shutil
import sys
from pathlib import Path


UNUSED_QML = {
    "Qt3D",
    "QtCharts",
    "QtDataVisualization",
    "QtGraphs",
    "QtLocation",
    "QtMultimedia",
    "QtPositioning",
    "QtQuick3D",
    "QtRemoteObjects",
    "QtScxml",
    "QtSensors",
    "QtTest",
    "QtTextToSpeech",
}


def slim_dist(root: Path) -> None:
    pyside = root / "_internal" / "PySide6"
    if not pyside.is_dir():
        raise SystemExit(f"PySide6 runtime not found: {pyside}")
    qml = pyside / "qml"
    for name in UNUSED_QML:
        target = qml / name
        if target.is_dir():
            shutil.rmtree(target)
    translations = pyside / "translations"
    for file in translations.glob("*.qm"):
        if "_zh_CN" not in file.name and "_en" not in file.name:
            file.unlink()
    for plugin_dir in ("networkinformation", "position", "qmltooling"):
        target = pyside / "plugins" / plugin_dir
        if target.is_dir():
            shutil.rmtree(target)
    required = (
        pyside / "QtWebEngineProcess.exe",
        pyside / "Qt6WebEngineCore.dll",
        pyside / "resources" / "qtwebengine_resources.pak",
        pyside / "resources" / "icudtl.dat",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("WebEngine runtime became incomplete: " + ", ".join(missing))


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/ReleaseFlow")
    slim_dist(target.resolve())
    total = sum(
        file.stat().st_size for file in target.rglob("*") if file.is_file()
    )
    print(f"Slimmed release: {target} ({total / 1024 / 1024:.1f} MB)")
