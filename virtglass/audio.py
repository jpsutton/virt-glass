"""Guest audio over SPICE (playback, optional microphone) via spice-glib.

The Looking Glass host streams video over shared memory but audio still
travels over the VM's SPICE channel, just like the stock client.
"""

import xml.etree.ElementTree as ET

import gi

gi.require_version("SpiceClientGLib", "2.0")
from gi.repository import GLib, GObject  # noqa: E402
from gi.repository import SpiceClientGLib as Spice  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402


def spice_uri(dom_xml):
    """spice://host:port for a domain's SPICE graphics, or None."""
    root = ET.fromstring(dom_xml)
    for g in root.findall("./devices/graphics[@type='spice']"):
        port = g.get("port")
        if not port or port == "-1":
            continue
        host = g.get("listen") or "127.0.0.1"
        listen = g.find("listen")
        if listen is not None and listen.get("address"):
            host = listen.get("address")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"spice://{host}:{port}"
    return None


class SpiceAudio(QObject):
    stateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.audio = None
        self.allow_mic = False
        self.uri = None
        self.state = "off"
        self._channels = []

    def _set_state(self, s):
        if s != self.state:
            self.state = s
            self.stateChanged.emit(s)

    def start(self, uri, allow_mic=False):
        self.stop()
        self.uri = uri
        self.allow_mic = allow_mic
        if not uri:
            self._set_state("unavailable")
            return
        s = Spice.Session()
        s.props.uri = uri
        s.props.enable_usbredir = False
        GObject.Object.connect(s, "channel-new", self._channel_new)
        self.audio = Spice.Audio.get(s, None)
        self.session = s
        self._set_state("connecting")
        if not s.connect():
            self._set_state("error")

    def stop(self):
        if self.session is not None:
            self.session.disconnect()
        self.session = None
        self.audio = None
        self._channels = []
        self._set_state("off")

    def set_mic(self, allow):
        if allow == self.allow_mic:
            return
        # reconnect so the record channel is (re)negotiated
        if self.uri:
            self.start(self.uri, allow)
        else:
            self.allow_mic = allow

    def _channel_new(self, session, ch):
        if session is not self.session:
            return
        self._channels.append(ch)
        if isinstance(ch, Spice.RecordChannel) and not self.allow_mic:
            GLib.idle_add(self._drop, ch)
        elif isinstance(ch, Spice.PlaybackChannel):
            GObject.Object.connect(ch, "channel-event", self._playback_event)

    def _drop(self, ch):
        Spice.Channel.disconnect(ch, Spice.ChannelEvent.CLOSED)
        return False

    def _playback_event(self, ch, event):
        # ignore late events from a session we've already replaced
        if ch not in self._channels:
            return
        if event == Spice.ChannelEvent.OPENED:
            self._set_state("playing")
        elif event in (Spice.ChannelEvent.CLOSED,
                       Spice.ChannelEvent.ERROR_CONNECT,
                       Spice.ChannelEvent.ERROR_IO,
                       Spice.ChannelEvent.ERROR_LINK):
            if self.session is not None:
                self._set_state("error" if event != Spice.ChannelEvent.CLOSED
                                else "off")
