"""ctypes binding for libvglass.so, the Looking Glass transport shim."""

import ctypes
import os
from ctypes import (POINTER, Structure, byref, c_bool, c_char, c_char_p, c_int,
                    c_int32, c_int64, c_size_t, c_uint, c_uint8, c_uint32,
                    c_uint64, c_void_p)
from dataclasses import dataclass

LIB_PATH = os.path.join(os.path.dirname(__file__), "native", "libvglass.so")

# session states
STATE_IDLE = 0
STATE_NO_DEVICE = 1
STATE_WAITING = 2
STATE_CONNECTED = 3
STATE_BAD_VERSION = 4

STATE_NAMES = {
    STATE_IDLE: "Idle",
    STATE_NO_DEVICE: "No shared memory device",
    STATE_WAITING: "Waiting for Looking Glass host",
    STATE_CONNECTED: "Connected",
    STATE_BAD_VERSION: "Host version mismatch",
}

# event bits
EV_FRAME = 1
EV_CURSOR = 2
EV_STATE = 4
EV_CLIPBOARD = 8
EV_INPUT = 16

# KVMFR frame types
FRAME_BGRA = 1
FRAME_RGBA = 2
FRAME_RGBA10 = 3
FRAME_RGBA16F = 4
FRAME_BGR_32 = 5
FRAME_RGB_24 = 6
FRAME_TYPE_NAMES = {1: "BGRA", 2: "RGBA", 3: "RGBA10", 4: "RGBA16F",
                    5: "BGR_32", 6: "RGB_24"}

# features
FEATURE_SET_CURSOR_POS = 0x1
FEATURE_WINDOW_SIZE = 0x2
FEATURE_FRAME_SCHEDULE = 0x4
FEATURE_INPUT = 0x8
FEATURE_CLIPBOARD = 0x10

# clipboard data types (LG_ClipboardData)
CLIP_TEXT = 0
CLIP_PNG = 1
CLIP_BMP = 2
CLIP_TIFF = 3
CLIP_JPEG = 4
CLIP_FILES = 5

OS_NAMES = {0: "Linux", 1: "BSD", 2: "macOS", 3: "Windows", 4: "Other"}

# keyboard LED bits
LED_NUM = 0x1
LED_CAPS = 0x2
LED_SCROLL = 0x4


class VGSessionInfo(Structure):
    _fields_ = [
        ("version", c_char * 32),
        ("osName", c_char * 64),
        ("capture", c_char * 32),
        ("cpuModel", c_char * 256),
        ("features", c_uint32),
        ("os", c_uint32),
        ("cpus", c_uint8),
        ("cores", c_uint8),
        ("sockets", c_uint8),
        ("uuidValid", c_uint8),
        ("uuid", c_uint8 * 16),
        ("expectedVersion", c_uint32),
        ("currentVersion", c_uint32),
        ("mismatchComponent", c_char * 16),
    ]


class VGFrameInfo(Structure):
    _fields_ = [
        ("serial", c_uint64),
        ("type", c_uint32),
        ("screenWidth", c_uint32),
        ("screenHeight", c_uint32),
        ("frameWidth", c_uint32),
        ("frameHeight", c_uint32),
        ("dataWidth", c_uint32),
        ("dataHeight", c_uint32),
        ("stride", c_uint32),
        ("pitch", c_uint32),
        ("rotation", c_uint32),
        ("hdr", c_uint32),
        ("hdrPQ", c_uint32),
        ("flags", c_uint32),
        ("sdrWhiteLevel", c_uint32),
        ("size", c_uint64),
    ]


class VGCursorInfo(Structure):
    _fields_ = [
        ("shapeSerial", c_uint64),
        ("posSerial", c_uint64),
        ("x", c_int32),
        ("y", c_int32),
        ("hx", c_int32),
        ("hy", c_int32),
        ("width", c_uint32),
        ("height", c_uint32),
        ("visible", c_uint32),
        ("type", c_uint32),
    ]


@dataclass
class SessionInfo:
    version: str
    os_name: str
    os: str
    capture: str
    cpu_model: str
    cpus: int
    cores: int
    sockets: int
    features: int
    uuid: str | None
    mismatch: str | None


def _sig(lib, name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


class LGClient:
    """Thin wrapper around the shim. A single instance per process."""

    def __init__(self, path=LIB_PATH):
        self.lib = lib = ctypes.CDLL(path)
        _sig(lib, "vg_version", c_int)
        _sig(lib, "vg_eventfd", c_int)
        _sig(lib, "vg_take_events", c_uint)
        _sig(lib, "vg_start", c_bool, c_char_p)
        _sig(lib, "vg_stop", None)
        _sig(lib, "vg_state", c_int)
        _sig(lib, "vg_session", c_bool, POINTER(VGSessionInfo))
        _sig(lib, "vg_frame_count", c_uint64)
        _sig(lib, "vg_frame_acquire", c_bool, POINTER(VGFrameInfo),
             POINTER(c_void_p))
        _sig(lib, "vg_frame_current", c_bool, POINTER(VGFrameInfo),
             POINTER(c_void_p))
        _sig(lib, "vg_cursor_get", c_bool, POINTER(VGCursorInfo), c_void_p,
             c_size_t)
        _sig(lib, "vg_input_available", c_bool)
        _sig(lib, "vg_input_leds", c_int)
        _sig(lib, "vg_input_abs_supported", c_bool)
        _sig(lib, "vg_key", c_bool, c_int, c_bool)
        _sig(lib, "vg_mouse_position", c_bool, c_uint32, c_uint32, c_uint32,
             c_uint32)
        _sig(lib, "vg_mouse_motion", c_bool, c_int32, c_int32)
        _sig(lib, "vg_mouse_button", c_bool, c_uint, c_bool)
        _sig(lib, "vg_keyboard_leds", c_bool, c_bool, c_bool, c_bool)
        _sig(lib, "vg_input_reset", None)
        _sig(lib, "vg_window_size", c_int, c_uint32, c_uint32)
        _sig(lib, "vg_set_cursor_pos", c_int, c_int32, c_int32)
        _sig(lib, "vg_set_refresh", None, ctypes.c_double)
        _sig(lib, "vg_request_frame", None)
        _sig(lib, "vg_clip_available", c_bool)
        _sig(lib, "vg_clip_guest_types", c_int, POINTER(c_int), c_int,
             POINTER(c_uint64))
        _sig(lib, "vg_clip_request", None, c_int)
        _sig(lib, "vg_clip_take", c_int64, c_void_p, c_uint64, POINTER(c_int),
             POINTER(c_uint64))
        _sig(lib, "vg_clip_notify", None, POINTER(c_int), c_int)
        _sig(lib, "vg_clip_guest_request", c_int, POINTER(c_uint64))
        _sig(lib, "vg_clip_reply", c_bool, c_int, c_char_p, c_uint64)
        _sig(lib, "vg_clip_cancel", None)
        self._cursor_buf = (c_uint8 * (512 * 512 * 4))()
        self.started = False

    # lifecycle ------------------------------------------------------------
    def start(self, shm_path):
        self.started = bool(self.lib.vg_start(shm_path.encode()))
        return self.started

    def stop(self):
        if self.started:
            self.lib.vg_stop()
            self.started = False

    def eventfd(self):
        return self.lib.vg_eventfd()

    def take_events(self):
        return self.lib.vg_take_events()

    def state(self):
        return self.lib.vg_state()

    def session(self):
        s = VGSessionInfo()
        self.lib.vg_session(byref(s))
        uuid = None
        if s.uuidValid:
            h = bytes(s.uuid).hex()
            uuid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        mismatch = None
        if s.mismatchComponent:
            mismatch = (f"{s.mismatchComponent.decode(errors='replace')}: "
                        f"client {s.expectedVersion}, host {s.currentVersion}")
        return SessionInfo(
            version=s.version.decode(errors="replace"),
            os_name=s.osName.decode(errors="replace").strip(),
            os=OS_NAMES.get(s.os, "?"),
            capture=s.capture.decode(errors="replace"),
            cpu_model=s.cpuModel.decode(errors="replace"),
            cpus=s.cpus, cores=s.cores, sockets=s.sockets,
            features=s.features, uuid=uuid, mismatch=mismatch)

    # frames -----------------------------------------------------------------
    def frame_count(self):
        return self.lib.vg_frame_count()

    def acquire_frame(self):
        """Return (VGFrameInfo, address) for a new frame, or None."""
        info = VGFrameInfo()
        ptr = c_void_p()
        if self.lib.vg_frame_acquire(byref(info), byref(ptr)):
            return info, ptr.value
        return None

    def current_frame(self):
        info = VGFrameInfo()
        ptr = c_void_p()
        if self.lib.vg_frame_current(byref(info), byref(ptr)):
            return info, ptr.value
        return None

    # cursor -----------------------------------------------------------------
    def cursor(self, with_shape=False):
        info = VGCursorInfo()
        buf = self._cursor_buf if with_shape else None
        self.lib.vg_cursor_get(byref(info), buf, ctypes.sizeof(self._cursor_buf)
                               if with_shape else 0)
        shape = None
        if with_shape and info.width and info.height:
            shape = bytes(self._cursor_buf[: info.width * info.height * 4])
        return info, shape

    # input ------------------------------------------------------------------
    def input_available(self):
        return self.lib.vg_input_available()

    def input_leds(self):
        return self.lib.vg_input_leds()

    def key(self, linux_code, down):
        return self.lib.vg_key(linux_code, down)

    def mouse_position(self, x, y, w, h):
        return self.lib.vg_mouse_position(x, y, w, h)

    def mouse_motion(self, dx, dy):
        return self.lib.vg_mouse_motion(dx, dy)

    def mouse_button(self, button, down):
        return self.lib.vg_mouse_button(button, down)

    def keyboard_leds(self, num, caps, scroll):
        return self.lib.vg_keyboard_leds(num, caps, scroll)

    def input_reset(self):
        self.lib.vg_input_reset()

    # control ----------------------------------------------------------------
    def set_refresh(self, hz):
        self.lib.vg_set_refresh(float(hz))

    def request_frame(self):
        self.lib.vg_request_frame()

    def window_size(self, w, h):
        return self.lib.vg_window_size(w, h)

    # clipboard --------------------------------------------------------------
    def clip_available(self):
        return self.lib.vg_clip_available()

    def clip_guest_types(self):
        arr = (c_int * 8)()
        serial = c_uint64()
        n = self.lib.vg_clip_guest_types(arr, 8, byref(serial))
        return list(arr[:n]), serial.value

    def clip_request(self, type_):
        self.lib.vg_clip_request(type_)

    def clip_take(self):
        """Return (type, bytes, serial) for completed guest data, or None."""
        t = c_int()
        serial = c_uint64()
        size = self.lib.vg_clip_take(None, 0, byref(t), byref(serial))
        while size >= 0:
            buf = ctypes.create_string_buffer(max(size, 1))
            got = self.lib.vg_clip_take(buf, len(buf), byref(t), byref(serial))
            if got < 0:
                return None
            if got <= len(buf):
                return t.value, buf.raw[:got], serial.value
            size = got  # newer, larger data arrived in between; retry
        return None

    def clip_notify(self, types):
        arr = (c_int * max(len(types), 1))(*types)
        self.lib.vg_clip_notify(arr, len(types))

    def clip_guest_request(self):
        serial = c_uint64()
        t = self.lib.vg_clip_guest_request(byref(serial))
        return (t if t >= 0 else None), serial.value

    def clip_reply(self, type_, data):
        return self.lib.vg_clip_reply(type_, data, len(data))

    def clip_cancel(self):
        """Refuse the pending guest request so later requests still work."""
        self.lib.vg_clip_cancel()
