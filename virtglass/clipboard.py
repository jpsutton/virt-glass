"""Two-way clipboard sync (text and images) over the Looking Glass
clipboard channel."""

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, Signal
from PySide6.QtGui import QGuiApplication, QImage

from . import lgclient as lg

IMAGE_TYPES = (lg.CLIP_PNG, lg.CLIP_BMP, lg.CLIP_JPEG, lg.CLIP_TIFF)


class ClipboardBridge(QObject):
    activity = Signal(str)           # human readable log line
    availabilityChanged = Signal(bool)

    def __init__(self, session, enabled=True, parent=None):
        super().__init__(parent)
        self.session = session
        self.client = session.client
        self.enabled = enabled
        self.cb = QGuiApplication.clipboard()
        self._guest_serial = None
        self._request_serial = None
        self._data_serial = None
        self._suppress = 0
        self._available = False
        self.cb.dataChanged.connect(self._host_changed)
        session.clipboardEvent.connect(self._guest_event)
        session.connected.connect(lambda _info: self._reset())
        session.disconnected.connect(self._reset)

    def set_enabled(self, on):
        self.enabled = on
        if on:
            self._host_changed()
        elif self.session.is_connected and self._available:
            self.client.clip_notify([])  # withdraw our offer

    def _reset(self):
        self._guest_serial = None
        self._request_serial = None
        self._data_serial = None
        self._set_available(False)

    def _set_available(self, avail):
        if avail != self._available:
            self._available = avail
            self.availabilityChanged.emit(avail)
            if avail:
                self._host_changed()

    @property
    def available(self):
        return self._available

    # host -> guest ------------------------------------------------------------
    def _host_changed(self):
        if self._suppress:
            self._suppress -= 1
            return
        if not (self.enabled and self.session.is_connected and self._available):
            return
        md = self.cb.mimeData()
        types = []
        if md is not None:
            if md.hasImage():
                types.append(lg.CLIP_PNG)
            if md.hasText():
                types.append(lg.CLIP_TEXT)
        self.client.clip_notify(types)

    def _host_payload(self, type_):
        md = self.cb.mimeData()
        if md is None:
            return None
        if type_ == lg.CLIP_TEXT and md.hasText():
            return md.text().encode("utf-8")
        if type_ in IMAGE_TYPES and md.hasImage():
            img = QImage(md.imageData())
            fmt = {lg.CLIP_PNG: "PNG", lg.CLIP_BMP: "BMP",
                   lg.CLIP_JPEG: "JPEG", lg.CLIP_TIFF: "TIFF"}[type_]
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            img.save(buf, fmt)
            buf.close()
            return bytes(ba.data())
        return None

    # guest -> host -------------------------------------------------------------
    def _guest_event(self):
        self._set_available(self.client.clip_available())

        # guest asked for our data; always answer, or its request slot stays
        # busy and later pastes are dropped
        req_type, req_serial = self.client.clip_guest_request()
        if req_type is not None and req_serial != self._request_serial:
            self._request_serial = req_serial
            payload = self._host_payload(req_type) if self.enabled else None
            if payload is not None and self.client.clip_reply(req_type, payload):
                self.activity.emit(f"Sent {len(payload)} bytes to guest")
            else:
                self.client.clip_cancel()

        if not self.enabled:
            return

        # guest has new clipboard contents
        types, serial = self.client.clip_guest_types()
        if serial != self._guest_serial:
            self._guest_serial = serial
            if lg.CLIP_TEXT in types:
                self.client.clip_request(lg.CLIP_TEXT)
            else:
                for t in IMAGE_TYPES:
                    if t in types:
                        self.client.clip_request(t)
                        break

        got = self.client.clip_take()
        if got:
            type_, data, serial = got
            if serial != self._data_serial:
                self._data_serial = serial
                self._apply_guest_data(type_, data)

    def _apply_guest_data(self, type_, data):
        md = QMimeData()
        if type_ == lg.CLIP_TEXT:
            text = data.decode("utf-8", errors="replace").rstrip("\0")
            md.setText(text)
            desc = f"{len(text)} characters"
        elif type_ in IMAGE_TYPES:
            img = QImage.fromData(data)
            if img.isNull():
                return
            md.setImageData(img)
            desc = f"{img.width()}x{img.height()} image"
        else:
            return
        self._suppress += 1
        self.cb.setMimeData(md)
        self.activity.emit(f"Copied {desc} from guest")
