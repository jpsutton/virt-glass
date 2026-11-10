"""GuestView: renders Looking Glass frames with OpenGL and forwards input."""

import collections
import ctypes
import time

from OpenGL import GL
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QImage, QPainter, QPixmap)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from . import keymap
from . import lgclient as lg

VERTEX_SRC = """
#version 330 core
uniform vec4 uRect;   // x0, y0, x1, y1 in NDC (top-left, bottom-right)
uniform int  uRot;
out vec2 vUV;
void main()
{
  vec2 p   = vec2(gl_VertexID & 1, gl_VertexID >> 1);
  vec2 pos = mix(uRect.xy, uRect.zw, p);
  vec2 uv  = p;
  if (uRot == 1)      uv = vec2(uv.y, 1.0 - uv.x);
  else if (uRot == 2) uv = vec2(1.0 - uv.x, 1.0 - uv.y);
  else if (uRot == 3) uv = vec2(1.0 - uv.y, uv.x);
  vUV = uv;
  gl_Position = vec4(pos, 0.0, 1.0);
}
"""

FRAGMENT_SRC = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D uTex;
uniform int   uMode;   // 0 sdr, 1 scRGB linear, 2 PQ/BT.2020
uniform float uWhite;  // SDR white level in nits
uniform float uDim;

vec3 srgbEncode(vec3 c)
{
  c = clamp(c, 0.0, 1.0);
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055,
             step(0.0031308, c));
}

vec3 pqDecode(vec3 e)
{
  const float m1 = 0.1593017578125, m2 = 78.84375;
  const float c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
  vec3 p = pow(max(e, 0.0), vec3(1.0 / m2));
  return 10000.0 * pow(max(p - c1, 0.0) / (c2 - c3 * p), vec3(1.0 / m1));
}

void main()
{
  vec4 c = texture(uTex, vUV);
  if (uMode == 1)
    c.rgb = srgbEncode(c.rgb * 80.0 / uWhite);
  else if (uMode == 2)
  {
    const mat3 bt2020to709 = mat3(
       1.6605, -0.1246, -0.0182,
      -0.5876,  1.1329, -0.1006,
      -0.0728, -0.0083,  1.1187);
    c.rgb = srgbEncode(bt2020to709 * (pqDecode(c.rgb) / uWhite));
  }
  fragColor = vec4(c.rgb * uDim, 1.0);
}
"""

# frame type -> (internal format, pixel format, pixel type, bytes per pixel)
GL_FORMATS = {
    lg.FRAME_BGRA: (GL.GL_RGBA8, GL.GL_BGRA, GL.GL_UNSIGNED_BYTE, 4),
    lg.FRAME_RGBA: (GL.GL_RGBA8, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, 4),
    lg.FRAME_RGBA10: (GL.GL_RGB10_A2, GL.GL_RGBA,
                      GL.GL_UNSIGNED_INT_2_10_10_10_REV, 4),
    lg.FRAME_RGBA16F: (GL.GL_RGBA16F, GL.GL_RGBA, GL.GL_HALF_FLOAT, 8),
    lg.FRAME_BGR_32: (GL.GL_RGB8, GL.GL_BGR, GL.GL_UNSIGNED_BYTE, 3),
    lg.FRAME_RGB_24: (GL.GL_RGB8, GL.GL_BGR, GL.GL_UNSIGNED_BYTE, 3),
}

SCALE_MODES = {
    "fit": "Fit (keep aspect)",
    "stretch": "Stretch",
    "integer": "Integer scale",
    "native": "Native (1:1)",
}


def _compile(src, kind):
    sh = GL.glCreateShader(kind)
    GL.glShaderSource(sh, src)
    GL.glCompileShader(sh)
    if not GL.glGetShaderiv(sh, GL.GL_COMPILE_STATUS):
        raise RuntimeError(GL.glGetShaderInfoLog(sh).decode())
    return sh


class GuestView(QOpenGLWidget):
    """The guest display. All coordinates passed to the guest are in guest
    screen pixels; widget coordinates are logical (device independent)."""

    statsUpdated = Signal(float, float)     # fps, ups
    formatChanged = Signal(object)          # VGFrameInfo
    hostAction = Signal(str)                # host-key combo actions
    filesDropped = Signal(list)
    grabChanged = Signal(bool)

    HOST_KEY = keymap.KEY_PAUSE
    HOST_COMBOS = {
        keymap.KEY_F: "fullscreen",
        keymap.KEY_D: "toggle-panels",
        keymap.KEY_S: "screenshot",
        keymap.KEY_V: "type-clipboard",
        keymap.KEY_R: "resize-guest",
        keymap.KEY_I: "toggle-stats",
        keymap.KEY_Q: "quit",
    }

    def __init__(self, session, settings, parent=None):
        super().__init__(parent)
        self.session = session
        self.client = session.client
        self.settings = settings
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 200)

        self.scale_mode = settings["scale_mode"]
        self.smooth = settings["smooth_scaling"]
        self.show_stats = settings["show_stats_overlay"]
        self.status_text = "Not connected"
        self.status_detail = ""

        self._prog = None
        self._vao = None
        self._tex = None
        self._tex_key = None
        self.frame = None                  # newest VGFrameInfo received
        self._pending = None               # (info, addr) awaiting upload
        self._format_version = None

        self._pressed_keys = set()
        self._host_down = False
        self._host_used = False
        self._host_swallow = set()
        self._key_queue = collections.deque()
        self._key_timer = QTimer(self, interval=self.KEY_PACE_MS,
                                 timeout=self._pump_keys)
        self._host_prefix = QTimer(self, singleShot=True,
                                   interval=self.HOST_PREFIX_MS,
                                   timeout=self._host_prefix_expired)
        self._buttons = set()
        self._wheel_accum = 0
        self.grabbed = False

        self._cursor_shape_serial = -1
        self._cursor_visible = None
        self._cursor_scale = None

        self._frames = 0
        self._paints = 0
        self._fps = 0.0
        self._ups = 0.0
        self._stats_t = time.monotonic()
        self._stat_timer = QTimer(self, interval=1000, timeout=self._roll_stats)
        self._stat_timer.start()

        session.frameReady.connect(self._on_frame)
        session.cursorChanged.connect(self._update_cursor)
        session.stateChanged.connect(self._on_state)
        session.connected.connect(self._on_connected)

    # --------------------------------------------------------------- GL
    def initializeGL(self):
        vs = _compile(VERTEX_SRC, GL.GL_VERTEX_SHADER)
        fs = _compile(FRAGMENT_SRC, GL.GL_FRAGMENT_SHADER)
        prog = GL.glCreateProgram()
        GL.glAttachShader(prog, vs)
        GL.glAttachShader(prog, fs)
        GL.glLinkProgram(prog)
        if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(prog).decode())
        GL.glDeleteShader(vs)
        GL.glDeleteShader(fs)
        self._prog = prog
        self._u = {n: GL.glGetUniformLocation(prog, n)
                   for n in ("uRect", "uRot", "uTex", "uMode", "uWhite", "uDim")}
        self._vao = GL.glGenVertexArrays(1)
        self._tex = GL.glGenTextures(1)
        self.context().aboutToBeDestroyed.connect(self._cleanup_gl)

    def _cleanup_gl(self):
        self.makeCurrent()
        if self._tex:
            GL.glDeleteTextures([self._tex])
        if self._vao:
            GL.glDeleteVertexArrays(1, [self._vao])
        if self._prog:
            GL.glDeleteProgram(self._prog)
        self._tex = self._vao = self._prog = None
        self.doneCurrent()

    def _upload(self, info, addr):
        fmt = GL_FORMATS.get(info.type)
        if not fmt:
            return False
        internal, pfmt, ptype, bpp = fmt
        w, h = info.frameWidth, info.frameHeight
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex)
        key = (internal, w, h)
        if self._tex_key != key:
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal, w, h, 0, pfmt,
                            ptype, None)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S,
                               GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T,
                               GL.GL_CLAMP_TO_EDGE)
            self._tex_key = key

        if info.type == lg.FRAME_BGR_32:
            # packed 24-bit rows stored linearly in a 32bpp surface
            rows = min(h, info.size // (w * 3))
            row_length, align = 0, 1
        elif info.type == lg.FRAME_RGB_24:
            rows = min(h, info.dataHeight)
            row_length, align = info.stride, 1
        else:
            rows = min(h, info.dataHeight)
            row_length, align = info.stride, 4
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, align)
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, row_length)
        GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, w, rows, pfmt, ptype,
                           ctypes.c_void_p(addr))
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, 0)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        return True

    def _on_frame(self):
        """Take the newest frame as soon as it arrives so geometry and input
        mapping never depend on repaint timing; the texture upload happens in
        paintGL. The acquired buffer stays valid until the next acquire."""
        got = self.client.acquire_frame() if self.session.is_connected else None
        if not got:
            return
        info, _ = got
        self._pending = got
        self._frames += 1
        changed = (self.frame is None or
                   (self.frame.type, self.frame.screenWidth,
                    self.frame.screenHeight, self.frame.frameWidth,
                    self.frame.frameHeight, self.frame.rotation,
                    self.frame.hdr) !=
                   (info.type, info.screenWidth, info.screenHeight,
                    info.frameWidth, info.frameHeight, info.rotation,
                    info.hdr))
        self.frame = info
        if changed:
            self._cursor_scale = None
            self.formatChanged.emit(info)
            self._update_cursor()
        self.update()

    def paintGL(self):
        self._paints += 1
        if self._pending is not None:
            info, addr = self._pending
            self._pending = None
            if not self._upload(info, addr):
                self._tex_key = None

        dpr = self.devicePixelRatioF()
        GL.glViewport(0, 0, int(self.width() * dpr), int(self.height() * dpr))
        GL.glClearColor(0.07, 0.07, 0.075, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        if self.frame is not None and self._tex_key is not None:
            r = self.display_rect()
            W, H = self.width(), self.height()
            x0 = r.left() / W * 2 - 1
            x1 = r.right() / W * 2 - 1
            y0 = 1 - r.top() / H * 2
            y1 = 1 - r.bottom() / H * 2
            f = self.frame
            mode = 0
            if f.type == lg.FRAME_RGBA16F:
                mode = 1
            elif f.hdr and f.hdrPQ:
                mode = 2
            filt = GL.GL_LINEAR if (self.smooth and self.scale_mode in
                                    ("fit", "stretch")) else GL.GL_NEAREST
            GL.glUseProgram(self._prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, filt)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, filt)
            GL.glUniform4f(self._u["uRect"], x0, y0, x1, y1)
            GL.glUniform1i(self._u["uRot"], int(f.rotation))
            GL.glUniform1i(self._u["uTex"], 0)
            GL.glUniform1i(self._u["uMode"], mode)
            GL.glUniform1f(self._u["uWhite"], float(f.sdrWhiteLevel or 203))
            GL.glUniform1f(self._u["uDim"],
                           1.0 if self.session.is_connected else 0.35)
            GL.glBindVertexArray(self._vao)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glBindVertexArray(0)
            GL.glUseProgram(0)

        if not self.session.is_connected or self.show_stats:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            if not self.session.is_connected:
                self._paint_status(p)
            if self.show_stats:
                self._paint_stats(p)
            p.end()

    def _paint_status(self, p):
        rect = QRectF(self.rect())
        box = QRectF(0, 0, min(520, rect.width() - 40), 120)
        box.moveCenter(rect.center())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(26, 26, 25, 230))
        p.drawRoundedRect(box, 10, 10)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#f0efec"))
        p.drawText(box.adjusted(16, 16, -16, -60), Qt.AlignCenter,
                   self.status_text)
        f.setBold(False)
        f.setPointSizeF(self.font().pointSizeF())
        p.setFont(f)
        p.setPen(QColor("#c3c2b7"))
        p.drawText(box.adjusted(16, 60, -16, -12),
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                   self.status_detail)

    def _paint_stats(self, p):
        lines = [f"FPS {self._fps:5.1f}   UPS {self._ups:5.1f}"]
        f = self.frame
        if f is not None:
            lines.append(f"{f.screenWidth}x{f.screenHeight} "
                         f"{lg.FRAME_TYPE_NAMES.get(f.type, '?')}"
                         f"{' HDR' if f.hdr else ''}")
            r = self.display_rect()
            lines.append(f"view {r.width():.0f}x{r.height():.0f} "
                         f"({self.scale_mode})")
        font = QFont("monospace")
        font.setStyleHint(QFont.Monospace)
        font.setPointSizeF(self.font().pointSizeF() * 0.9)
        p.setFont(font)
        fm = p.fontMetrics()
        w = max(fm.horizontalAdvance(s) for s in lines) + 16
        h = fm.height() * len(lines) + 12
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(QRectF(8, 8, w, h), 6, 6)
        p.setPen(QColor("#f0efec"))
        for i, s in enumerate(lines):
            p.drawText(QPointF(16, 8 + 6 + fm.ascent() + i * fm.height()), s)

    def _roll_stats(self):
        now = time.monotonic()
        dt = now - self._stats_t
        self._stats_t = now
        self._ups = self._frames / dt
        self._fps = self._paints / dt
        self._frames = self._paints = 0
        self.statsUpdated.emit(self._fps, self._ups)
        if self.show_stats:
            self.update()

    # ----------------------------------------------------------- geometry
    def guest_size(self):
        """Displayed size of the guest image in frame pixels (rotation
        applied)."""
        f = self.frame
        if f is None:
            return None
        w, h = f.frameWidth, f.frameHeight
        if f.rotation in (1, 3):
            w, h = h, w
        return w, h

    def display_rect(self):
        size = self.guest_size()
        W, H = self.width(), self.height()
        if not size:
            return QRectF(0, 0, W, H)
        gw, gh = size
        dpr = self.devicePixelRatioF()
        mode = self.scale_mode
        if mode == "stretch":
            return QRectF(0, 0, W, H)
        if mode == "native":
            w, h = gw / dpr, gh / dpr
        elif mode == "integer":
            s = min(W * dpr / gw, H * dpr / gh)
            s = max(1, int(s)) if s >= 1 else s
            w, h = gw * s / dpr, gh * s / dpr
        else:
            s = min(W / gw, H / gh)
            w, h = gw * s, gh * s
        # snap to physical pixels to keep the image crisp
        x = round((W - w) / 2 * dpr) / dpr
        y = round((H - h) / 2 * dpr) / dpr
        return QRectF(x, y, w, h)

    def to_guest(self, pos):
        """Widget position -> guest screen coordinates, or None if outside."""
        f = self.frame
        if f is None:
            return None
        r = self.display_rect()
        if r.width() <= 0 or r.height() <= 0:
            return None
        u = (pos.x() - r.left()) / r.width()
        v = (pos.y() - r.top()) / r.height()
        inside = 0 <= u < 1 and 0 <= v < 1
        u = min(max(u, 0.0), 0.9999)
        v = min(max(v, 0.0), 0.9999)
        # undo display rotation to get guest-native coordinates
        rot = f.rotation
        if rot == 1:
            u, v = v, 1 - u
        elif rot == 2:
            u, v = 1 - u, 1 - v
        elif rot == 3:
            u, v = 1 - v, u
        x = int(u * f.screenWidth)
        y = int(v * f.screenHeight)
        return x, y, inside

    def preferred_size(self):
        size = self.guest_size()
        if not size:
            return None
        dpr = self.devicePixelRatioF()
        return QSize(round(size[0] / dpr), round(size[1] / dpr))

    # -------------------------------------------------------------- cursor
    def _update_cursor(self):
        if not self.session.is_connected or self.frame is None:
            self.unsetCursor()
            self._cursor_shape_serial = -1
            return
        info, _ = self.client.cursor(False)
        r = self.display_rect()
        scale = r.width() / max(1, self.frame.screenWidth) \
            if self.frame.rotation in (0, 2) else \
            r.height() / max(1, self.frame.screenWidth)
        visible = bool(info.visible)
        if (info.shapeSerial == self._cursor_shape_serial and
                visible == self._cursor_visible and
                scale == self._cursor_scale):
            return
        self._cursor_visible = visible
        self._cursor_scale = scale
        self._cursor_shape_serial = info.shapeSerial
        if not visible:
            self.setCursor(Qt.BlankCursor)
            return
        info, shape = self.client.cursor(True)
        if not shape:
            self.setCursor(Qt.ArrowCursor)
            return
        img = QImage(shape, info.width, info.height, info.width * 4,
                     QImage.Format_ARGB32).copy()
        dpr = self.devicePixelRatioF()
        phys = scale * dpr
        if abs(phys - 1.0) > 0.01:
            img = img.scaled(max(1, round(info.width * phys)),
                             max(1, round(info.height * phys)),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        self.setCursor(QCursor(pm, round(info.hx * scale),
                               round(info.hy * scale)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._cursor_scale = None
        self._update_cursor()

    # -------------------------------------------------------------- state
    def _on_state(self, state):
        if state != lg.STATE_CONNECTED:
            # the shim may reuse that buffer once a new session starts
            self._pending = None
            self._release_all()
            self._cursor_shape_serial = -1
            self.unsetCursor()
        self.update()

    def _on_connected(self, info):
        self.client.set_refresh(self._refresh_rate())
        self._cursor_shape_serial = -1
        if self.settings["wake_on_connect"]:
            # a tiny move wakes a sleeping guest display so frames flow
            QTimer.singleShot(300, self.wake_guest)

    def wake_guest(self):
        if self.session.is_connected:
            self.client.mouse_motion(1, 0)
            self.client.mouse_motion(-1, 0)
            self.client.request_frame()

    def _refresh_rate(self):
        scr = self.screen() or (self.window().screen() if self.window() else None)
        return scr.refreshRate() if scr else 60.0

    def reset_frame(self):
        self.frame = None
        self._pending = None
        self._tex_key = None
        self.update()

    def set_status(self, text, detail=""):
        self.status_text = text
        self.status_detail = detail
        self.update()

    def set_scale_mode(self, mode):
        self.scale_mode = mode
        self._cursor_scale = None
        self._update_cursor()
        self.update()

    def set_show_stats(self, on):
        self.show_stats = on
        self.update()

    # --------------------------------------------------------------- input
    def _input_ok(self):
        return self.session.is_connected and self.frame is not None

    def event(self, e):
        # keep Tab and app shortcuts flowing to the guest while focused
        if e.type() == e.Type.ShortcutOverride and self._input_ok():
            e.accept()
            return True
        return super().event(e)

    def focusNextPrevChild(self, nxt):
        return False if self._input_ok() else super().focusNextPrevChild(nxt)

    # The host key works both held (Pause+F) and as a prefix (Pause, then F
    # within HOST_PREFIX_MS). The prefix form matters on laptop/PS/2
    # keyboards, which report Pause's press and release together.
    HOST_PREFIX_MS = 600

    def _host_armed(self):
        return self._host_down or self._host_prefix.isActive()

    def _host_prefix_expired(self):
        # tapped on its own: toggle keyboard capture
        self.set_grab(not self.grabbed)

    def keyPressEvent(self, e):
        code = keymap.linux_code(e)
        if code == keymap.KEY_F11 or e.key() == Qt.Key_F11:
            # F11 always belongs to the app (fullscreen); the guest can still
            # get it from the Send Keys menu
            if not e.isAutoRepeat():
                self.hostAction.emit("fullscreen")
                if code is not None:
                    self._host_swallow.add(code)
            return
        if code is None:
            return
        if code == self.HOST_KEY:
            if not e.isAutoRepeat():
                self._host_down = True
                self._host_used = False
                self._host_prefix.stop()
            return
        if self._host_armed():
            if e.isAutoRepeat():
                return
            action = self.HOST_COMBOS.get(code)
            self._host_used = True
            self._host_prefix.stop()
            if action:
                self.hostAction.emit(action)
            self._host_swallow.add(code)
            return
        if e.isAutoRepeat() or not self._input_ok():
            return
        if self.client.key(code, True):
            self._pressed_keys.add(code)

    def keyReleaseEvent(self, e):
        code = keymap.linux_code(e)
        if code is None or e.isAutoRepeat():
            return
        if code == self.HOST_KEY:
            self._host_down = False
            if not self._host_used:
                self._host_prefix.start()
            return
        if code in self._host_swallow:
            self._host_swallow.discard(code)
            return
        if code in self._pressed_keys:
            self._pressed_keys.discard(code)
            self.client.key(code, False)

    def set_grab(self, on):
        """Keyboard grab: all keys (within this app) go to the guest."""
        if on == self.grabbed:
            return
        self.grabbed = on
        if on:
            self.setFocus()
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
        self.grabChanged.emit(on)

    def _release_all(self):
        for code in list(self._pressed_keys):
            self.client.key(code, False)
        self._pressed_keys.clear()
        for b in list(self._buttons):
            self.client.mouse_button(b, False)
        self._buttons.clear()
        if self.session.is_connected:
            self.client.input_reset()

    def focusOutEvent(self, e):
        self._release_all()
        self._host_down = False
        self._host_swallow.clear()
        super().focusOutEvent(e)

    def _send_position(self, pos):
        g = self.to_guest(pos)
        if g is None:
            return False
        x, y, inside = g
        if inside or self._buttons:
            self.client.mouse_position(x, y, self.frame.screenWidth,
                                       self.frame.screenHeight)
        return inside

    def mouseMoveEvent(self, e):
        if self._input_ok():
            self._send_position(e.position())

    def mousePressEvent(self, e):
        self.setFocus()
        if not self._input_ok():
            return
        b = keymap.qt_button(e.button())
        if b is None:
            return
        self._send_position(e.position())
        if self.client.mouse_button(b, True):
            self._buttons.add(b)

    def mouseReleaseEvent(self, e):
        b = keymap.qt_button(e.button())
        if b in self._buttons:
            self._buttons.discard(b)
            if self._input_ok():
                self._send_position(e.position())
                self.client.mouse_button(b, False)

    def mouseDoubleClickEvent(self, e):
        self.mousePressEvent(e)

    def wheelEvent(self, e):
        if not self._input_ok():
            return
        self._send_position(e.position())
        self._wheel_accum += e.angleDelta().y()
        while abs(self._wheel_accum) >= 120:
            btn = 4 if self._wheel_accum > 0 else 5
            self.client.mouse_button(btn, True)
            self.client.mouse_button(btn, False)
            self._wheel_accum -= 120 if btn == 4 else -120

    # Synthetic key sequences are paced: an instant press/release pair can be
    # merged into one keyboard report by the input stream and get lost.
    KEY_PACE_MS = 12

    def _queue_keys(self, events):
        self._key_queue.extend(events)
        if not self._key_timer.isActive():
            self._key_timer.start()

    def _pump_keys(self):
        if not self._key_queue or not self._input_ok():
            self._key_queue.clear()
            self._key_timer.stop()
            return
        code, down = self._key_queue.popleft()
        self.client.key(code, down)

    def send_keys(self, codes):
        """Press the keys in order, then release in reverse."""
        if not self._input_ok():
            return False
        self._queue_keys([(c, True) for c in codes] +
                         [(c, False) for c in reversed(codes)])
        return True

    def type_text(self, text):
        """Type text into the guest using US-layout key events. Returns the
        number of characters that could not be typed."""
        if not self._input_ok():
            return len(text)
        skipped = 0
        events = []
        for ch in text:
            mapped = keymap.char_to_key(ch)
            if mapped is None:
                skipped += 1
                continue
            code, shift = mapped
            if shift:
                events.append((keymap.KEY_LEFTSHIFT, True))
            events += [(code, True), (code, False)]
            if shift:
                events.append((keymap.KEY_LEFTSHIFT, False))
        self._queue_keys(events)
        return skipped

    # --------------------------------------------------------- drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile()
                                          for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.filesDropped.emit(files)
            e.acceptProposedAction()

    # --------------------------------------------------------- screenshot
    def screenshot(self):
        """QImage of the current guest frame at native resolution."""
        cur = self.client.current_frame() if self.session.is_connected else None
        if not cur:
            return self.grabFramebuffer()
        info, addr = cur
        qfmt = {
            lg.FRAME_BGRA: QImage.Format_RGB32,
            lg.FRAME_RGBA: QImage.Format_RGBX8888,
            lg.FRAME_RGBA10: QImage.Format_BGR30,
            lg.FRAME_RGBA16F: QImage.Format_RGBX16FPx4,
        }.get(info.type)
        if qfmt is None:
            return self.grabFramebuffer()
        bpp = 8 if info.type == lg.FRAME_RGBA16F else 4
        rows = min(info.frameHeight, info.dataHeight)
        buf = (ctypes.c_uint8 * (rows * info.pitch)).from_address(addr)
        img = QImage(bytes(buf), info.frameWidth, rows, info.stride * bpp, qfmt)
        return img.convertToFormat(QImage.Format_RGB32)
