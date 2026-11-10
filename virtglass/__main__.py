import os
import sys


def run():
    # Qt uses EGL on Wayland; PyOpenGL must resolve functions the same way.
    if (os.environ.get("WAYLAND_DISPLAY") and
            os.environ.get("QT_QPA_PLATFORM", "wayland").startswith("wayland")):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    from .app import main
    code = main(sys.argv)
    # Settings are saved and the shim stopped in closeEvent. Don't wait for
    # in-flight guest-agent work (uploads, long commands) to finish.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(run())
