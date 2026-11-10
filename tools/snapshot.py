"""Dev helper: launch the main window, run scripted steps and save window
grabs. Usage: python tools/snapshot.py OUTDIR [step ...]

Steps: wait:<sec>  grab:<name>  tab:<index>  scale:<mode>  stats:on|off
       resize:<w>x<h>  wake  key:<linuxcode>
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from virtglass import app as vgapp  # noqa: E402


def main():
    out = sys.argv[1]
    steps = sys.argv[2:]
    os.makedirs(out, exist_ok=True)
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtCore import Qt
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    qapp = QApplication(sys.argv[:1])
    vgapp.apply_dark_theme(qapp)
    win = vgapp.MainWindow()
    win.resize(1600, 960)
    win.show()

    def run(i=0):
        if i >= len(steps):
            win.close()
            qapp.quit()
            return
        kind, _, arg = steps[i].partition(":")
        delay = 0
        if kind == "wait":
            delay = int(float(arg) * 1000)
        elif kind == "grab":
            win.grab().save(os.path.join(out, arg + ".png"))
            print("grabbed", arg, flush=True)
        elif kind == "tab":
            win.tabs.setCurrentIndex(int(arg))
        elif kind == "scale":
            win._set_scale(arg)
        elif kind == "stats":
            win.view.set_show_stats(arg == "on")
        elif kind == "resize":
            w, h = arg.split("x")
            win.resize(int(w), int(h))
        elif kind == "wake":
            win.view.wake_guest()
        elif kind == "key":
            win.view.send_keys([int(arg)])
        elif kind == "eval":
            print(eval(arg, {"win": win}), flush=True)
        QTimer.singleShot(delay, lambda: run(i + 1))

    QTimer.singleShot(500, run)
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
