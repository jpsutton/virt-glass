"""Qt-side owner of the Looking Glass client: turns shim events into signals."""

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from . import lgclient as lg


class LGSession(QObject):
    frameReady = Signal()
    cursorChanged = Signal()
    stateChanged = Signal(int)
    clipboardEvent = Signal()
    inputChanged = Signal()
    connected = Signal(object)        # SessionInfo
    disconnected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = lg.LGClient()
        self.shm_path = None
        self.state = lg.STATE_IDLE
        self.info = None
        self._notifier = None

    def start(self, shm_path):
        self.stop()
        self.shm_path = shm_path
        self.client.start(shm_path)
        if self._notifier is None:
            self._notifier = QSocketNotifier(self.client.eventfd(),
                                             QSocketNotifier.Read, self)
            self._notifier.activated.connect(self._pump)
        self._notifier.setEnabled(True)
        # catch any state set before the notifier existed
        QTimer.singleShot(0, self._pump)

    def stop(self):
        if self.client.started:
            self.client.stop()
            self._pump()
        self.shm_path = None

    def _pump(self, *_):
        ev = self.client.take_events()
        if ev & lg.EV_STATE:
            st = self.client.state()
            if st != self.state:
                old = self.state
                self.state = st
                if st == lg.STATE_CONNECTED:
                    self.info = self.client.session()
                    self.connected.emit(self.info)
                elif old == lg.STATE_CONNECTED:
                    self.disconnected.emit()
                self.stateChanged.emit(st)
        if ev & lg.EV_FRAME:
            self.frameReady.emit()
        if ev & lg.EV_CURSOR:
            self.cursorChanged.emit()
        if ev & lg.EV_CLIPBOARD:
            self.clipboardEvent.emit()
        if ev & lg.EV_INPUT:
            self.inputChanged.emit()

    @property
    def is_connected(self):
        return self.state == lg.STATE_CONNECTED
