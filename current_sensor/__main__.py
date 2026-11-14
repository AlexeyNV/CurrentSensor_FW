import argparse
import sys
from PySide6 import QtCore, QtGui, QtWidgets
from .device_worker import DeviceWorker
from .main_window import MainWindow
from .mock_device import MockSerial
from .lifecycle_trace import close_lifecycle_trace
from . import __version__


def main():
    parser = argparse.ArgumentParser(description=f"CurrentSensor GUI v{__version__}")
    parser.add_argument("--port", help="optional real COM port to connect")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="synthetic device, no hardware")
    mode.add_argument("--replay", help="replay a recorded .csf4 wire file, no hardware")
    parser.add_argument("--record-dir", help="arm RAW recording on demo start")
    parser.add_argument("--smoke-seconds", type=float, help="automatic demo close for smoke tests")
    parser.add_argument("--screenshot", help="save a GUI screenshot during a smoke test")
    args = parser.parse_args()
    simulated = bool(args.demo or args.replay)
    if args.smoke_seconds is not None and (not simulated or args.smoke_seconds < 1):
        parser.error("smoke-seconds >= 1 requires demo/replay")
    app = QtWidgets.QApplication(sys.argv[:1])
    if sys.platform == "win32":
        # The offscreen Qt platform has no system font discovery on Windows.
        from pathlib import Path
        import os
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
        if font.exists():
            QtGui.QFontDatabase.addApplicationFont(str(font))
        app.setFont(QtGui.QFont("Segoe UI", 9))
    factory = (lambda *a, **kw: MockSerial(*a, replay=args.replay, **kw)) if simulated else None
    worker = DeviceWorker(factory)
    window = MainWindow(worker, demo=simulated, auto_port="DEMO" if simulated else args.port,
                        auto_start=simulated, record_directory=args.record_dir)
    if args.record_dir:
        window.record_button.setChecked(True)
    window.show()
    if args.smoke_seconds:
        QtCore.QTimer.singleShot(int(args.smoke_seconds * 1000), window.close)
        if args.screenshot:
            QtCore.QTimer.singleShot(int(args.smoke_seconds * 700), lambda: window.grab().save(args.screenshot))
    try:
        return app.exec()
    finally:
        close_lifecycle_trace()


if __name__ == "__main__":
    raise SystemExit(main())
