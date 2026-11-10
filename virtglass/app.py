"""Main window."""

import datetime
import os

import libvirt
from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QColor, QGuiApplication,
                           QIcon, QKeySequence, QPainter, QPalette, QPixmap)
from PySide6.QtWidgets import (QApplication, QDockWidget, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget,
                               QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QStyleFactory,
                               QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import keymap
from . import lgclient as lg
from . import vm as vmlib
from .audio import SpiceAudio, spice_uri
from .clipboard import ClipboardBridge
from .display import SCALE_MODES, GuestView
from .panels import (DevicesPanel, GuestPanel, OverviewPanel, PerformancePanel,
                     ScrollPanel, SnapshotsPanel)
from .session import LGSession
from .settings import Settings

APP_NAME = "Virt Glass"

STATE_COLORS = {
    libvirt.VIR_DOMAIN_RUNNING: "#199e70",
    libvirt.VIR_DOMAIN_PAUSED: "#c98500",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "#c98500",
    libvirt.VIR_DOMAIN_SHUTDOWN: "#c98500",
    libvirt.VIR_DOMAIN_CRASHED: "#e66767",
}


def dot_icon(color):
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QIcon(pm)


def icon(*names):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QIcon()


def apply_dark_theme(app):
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    base = QColor("#121212")
    window = QColor("#1e1e1d")
    text = QColor("#f0efec")
    pal.setColor(QPalette.Window, window)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, QColor("#242423"))
    pal.setColor(QPalette.ToolTipBase, QColor("#2a2a29"))
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, QColor("#2a2a29"))
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.BrightText, QColor("#e66767"))
    pal.setColor(QPalette.Highlight, QColor("#3987e5"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.Link, QColor("#6da7ec"))
    pal.setColor(QPalette.PlaceholderText, QColor("#8d8c84"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, QColor("#6b6a64"))
    app.setPalette(pal)
    app.setStyleSheet("""
        QToolBar { border: none; spacing: 2px; padding: 3px; }
        QDockWidget::title { padding: 6px; background: #1a1a19; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { padding: 6px 10px; background: transparent;
                       border-bottom: 2px solid transparent; color: #c3c2b7; }
        QTabBar::tab:selected { color: #f0efec; border-bottom-color: #3987e5; }
        QListWidget#machines { border: none; background: #1a1a19; }
        QListWidget#machines::item { padding: 8px 6px; border-radius: 6px; }
        QListWidget#machines::item:selected { background: #2b3a4f; }
        QStatusBar QLabel { color: #c3c2b7; padding: 0 6px; }
        QTextBrowser, QPlainTextEdit, QTableWidget, QListWidget {
            border: 1px solid #2e2e2c; border-radius: 6px; }
    """)


class FullscreenBar(QWidget):
    """Slim bar at the top of the guest view in fullscreen. It shows briefly
    on entering fullscreen and whenever the pointer touches the top edge."""

    def __init__(self, view, actions, parent_window):
        super().__init__(view)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("fullscreenBar")
        self.setStyleSheet("""
            #fullscreenBar { background: rgba(26, 26, 25, 235);
                             border-bottom-left-radius: 8px;
                             border-bottom-right-radius: 8px; }
            QLabel { color: #c3c2b7; padding: 0 8px; }
            QToolButton { color: #f0efec; padding: 4px 10px; border: none;
                          border-radius: 6px; }
            QToolButton:hover { background: #2b3a4f; }
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.label = QLabel()
        lay.addWidget(self.label)
        for a in actions:
            b = QToolButton()
            b.setDefaultAction(a)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setFocusPolicy(Qt.NoFocus)
            lay.addWidget(b)
        self._hide = QTimer(self, singleShot=True, interval=2500,
                            timeout=self._maybe_hide)
        self.hide()

    def recenter(self):
        self.adjustSize()
        self.move((self.parentWidget().width() - self.width()) // 2, 0)

    def reveal(self, text=None, hold_ms=2500):
        if text is not None:
            self.label.setText(text)
        self.recenter()
        self.show()
        self.raise_()
        self._hide.start(hold_ms)

    def _maybe_hide(self):
        if self.underMouse():
            self._hide.start(800)
        else:
            self.hide()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon("computer", "video-display"))
        self.resize(1440, 900)

        self.manager = vmlib.LibvirtManager(self.settings["libvirt_uri"], self)
        self.session = LGSession(self)
        self.audio = SpiceAudio(self)
        self.current = None              # selected domain name
        self.current_state = None
        self._fullscreen_saved = None
        self._last_window_size = None

        self.view = GuestView(self.session, self.settings)
        self.setCentralWidget(self.view)
        self.clip = ClipboardBridge(self.session, self.settings["clipboard_sync"],
                                    self)

        self._build_sidebar()
        self._build_panels()
        self._build_actions()
        self._build_menus_and_toolbar()
        self._build_statusbar()
        self._wire()

        geo = self.settings["geometry"]
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        st = self.settings["window_state"]
        if st:
            self.restoreState(QByteArray.fromBase64(st.encode()))
        self.machines_dock.setVisible(self.settings["show_sidebar"])
        self.panel_dock.setVisible(self.settings["show_panel"])

        self._resize_timer = QTimer(self, singleShot=True, interval=400,
                                    timeout=self._send_window_size)
        self._audio_retry = QTimer(self, interval=5000,
                                   timeout=self._ensure_audio)
        self._audio_retry.start()

        QTimer.singleShot(0, self._connect_libvirt)

    # ------------------------------------------------------------------ build
    def _build_sidebar(self):
        self.machines = QListWidget()
        self.machines.setObjectName("machines")
        self.machines.setIconSize(QSize(14, 14))
        self.machines.setContextMenuPolicy(Qt.CustomContextMenu)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.machines)
        self.conn_label = QLabel()
        self.conn_label.setStyleSheet("color: #8d8c84; font-size: small")
        self.conn_label.setWordWrap(True)
        lay.addWidget(self.conn_label)
        self.machines_dock = QDockWidget("Machines", self)
        self.machines_dock.setObjectName("machinesDock")
        self.machines_dock.setWidget(w)
        self.machines_dock.setFeatures(QDockWidget.DockWidgetClosable |
                                       QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.machines_dock)

    def _build_panels(self):
        self.overview = OverviewPanel(self.manager)
        self.perf = PerformancePanel(self.manager)
        self.snapshots = SnapshotsPanel(self.manager)
        self.devices = DevicesPanel(self.manager)
        self.guest = GuestPanel(self.manager, self.settings)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.overview, "Overview")
        self.tabs.addTab(ScrollPanel(self.perf), "Performance")
        self.tabs.addTab(self.guest, "Guest")
        self.tabs.addTab(self.devices, "Devices")
        self.tabs.addTab(self.snapshots, "Snapshots")
        self.panel_dock = QDockWidget("Details", self)
        self.panel_dock.setObjectName("panelDock")
        self.panel_dock.setWidget(self.tabs)
        self.panel_dock.setMinimumWidth(380)
        self.panel_dock.setFeatures(QDockWidget.DockWidgetClosable |
                                    QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, self.panel_dock)
        self.panels = [self.overview, self.perf, self.snapshots, self.devices,
                       self.guest]

    def _act(self, text, slot=None, icon_names=(), shortcut=None,
             checkable=False, checked=False, tip=None):
        a = QAction(icon(*icon_names) if icon_names else QIcon(), text, self)
        if slot:
            a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        return a

    def _build_actions(self):
        s = self.settings
        self.a_start = self._act("Start", self._vm(self.manager.start),
                                 ("media-playback-start",), tip="Start the VM")
        self.a_shutdown = self._act(
            "Shut Down", self._vm(self.manager.shutdown),
            ("system-shutdown",), tip="Graceful ACPI shutdown")
        self.a_shutdown_agent = self._act(
            "Shut Down via Guest Agent",
            self._vm(lambda n: self.manager.shutdown(n, agent=True)))
        self.a_reboot = self._act("Reboot", self._vm(self.manager.reboot),
                                  ("system-reboot",), tip="Graceful reboot")
        self.a_pause = self._act("Pause", self._toggle_pause,
                                 ("media-playback-pause",), checkable=True,
                                 tip="Pause / resume the VM")
        self.a_reset = self._act("Reset", self._confirm_vm(
            "Reset", "Hard-reset {}? Unsaved work in the guest will be lost.",
            self.manager.reset), ("view-refresh",), tip="Hard reset")
        self.a_force = self._act("Force Off", self._confirm_vm(
            "Force off", "Pull the plug on {}? Unsaved work in the guest will "
            "be lost.", self.manager.force_off), ("process-stop",),
            tip="Immediately power off")
        self.a_autostart = self._act("Start with Host", self._toggle_autostart,
                                     checkable=True)

        self.a_fullscreen = self._act("Fullscreen", self.toggle_fullscreen,
                                      ("view-fullscreen",), "F11",
                                      checkable=True)
        self.a_screenshot = self._act("Screenshot", self.screenshot,
                                      ("camera-photo", "applets-screenshooter"),
                                      tip="Save the guest screen as PNG")
        self.a_stats = self._act("Show Stats Overlay", self._toggle_stats,
                                 checkable=True,
                                 checked=s["show_stats_overlay"])
        self.a_smooth = self._act("Smooth Scaling", self._toggle_smooth,
                                  checkable=True, checked=s["smooth_scaling"])
        self.a_sidebar = self.machines_dock.toggleViewAction()
        self.a_sidebar.setText("Machines Sidebar")
        self.a_panel = self.panel_dock.toggleViewAction()
        self.a_panel.setText("Details Panel")
        self.scale_group = QActionGroup(self)
        self.scale_actions = {}
        for key, label in SCALE_MODES.items():
            a = self._act(label, lambda _=False, k=key: self._set_scale(k),
                          checkable=True, checked=s["scale_mode"] == key)
            self.scale_group.addAction(a)
            self.scale_actions[key] = a

        self.a_grab = self._act("Capture Keyboard", self._toggle_grab,
                                ("input-keyboard",), checkable=True,
                                tip="Send all keys to the guest (Pause toggles)")
        self.a_cad = self._act("Ctrl+Alt+Del",
                               lambda: self.view.send_keys(keymap.COMBOS[0][1]),
                               ("system-lock-screen",),
                               tip="Send Ctrl+Alt+Del to the guest")
        self.a_type = self._act("Type Clipboard Text", self.type_clipboard,
                                ("edit-paste",),
                                tip="Type the host clipboard into the guest "
                                    "as keystrokes (works on login screens)")
        self.a_auto_resize = self._act(
            "Auto-resize Guest to Window", self._toggle_auto_resize,
            checkable=True, checked=s["auto_resize_guest"],
            tip="Ask the guest to change its resolution to match the window")
        self.a_resize_now = self._act("Resize Guest to Window",
                                      lambda: self._send_window_size(True),
                                      ("zoom-fit-best",))
        self.a_fit_window = self._act("Fit Window to Guest", self.fit_window,
                                      ("zoom-original",))
        self.a_clip = self._act("Clipboard Sync", self._toggle_clip,
                                checkable=True, checked=s["clipboard_sync"])
        self.a_audio = self._act("Audio", self._toggle_audio,
                                 ("audio-volume-high",), checkable=True,
                                 checked=s["audio"])
        self.a_mic = self._act("Allow Microphone", self._toggle_mic,
                               checkable=True, checked=s["microphone"])
        self.a_wake = self._act("Wake Guest Display", self.view.wake_guest)

    def _build_menus_and_toolbar(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(self.a_screenshot)
        m.addAction(self._act("Connect to libvirt URI…", self._change_uri))
        m.addSeparator()
        m.addAction(self._act("Quit", self.close, ("application-exit",),
                              "Ctrl+Q"))

        m = mb.addMenu("&Machine")
        for a in (self.a_start, self.a_shutdown, self.a_shutdown_agent,
                  self.a_reboot, self.a_pause):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_reset)
        m.addAction(self.a_force)
        m.addSeparator()
        m.addAction(self.a_autostart)

        m = mb.addMenu("&View")
        m.addAction(self.a_fullscreen)
        sm = m.addMenu("Scaling")
        for a in self.scale_actions.values():
            sm.addAction(a)
        sm.addSeparator()
        sm.addAction(self.a_smooth)
        m.addAction(self.a_fit_window)
        m.addAction(self.a_stats)
        m.addSeparator()
        m.addAction(self.a_sidebar)
        m.addAction(self.a_panel)

        m = mb.addMenu("&Input")
        m.addAction(self.a_grab)
        self.keys_menu = m.addMenu(icon("input-keyboard"), "Send Keys")
        for label, codes in keymap.COMBOS:
            self.keys_menu.addAction(
                label, lambda c=codes: self.view.send_keys(c))
        m.addAction(self.a_type)

        m = mb.addMenu("&Guest")
        m.addAction(self.a_auto_resize)
        m.addAction(self.a_resize_now)
        m.addSeparator()
        m.addAction(self.a_clip)
        m.addAction(self.a_audio)
        m.addAction(self.a_mic)
        m.addSeparator()
        m.addAction(self.a_wake)

        m = mb.addMenu("&Help")
        m.addAction(self._act("Keyboard Shortcuts", self.show_shortcuts))
        m.addAction(self._act("About", self.about))

        tb = self.addToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(18, 18))
        for a in (self.a_start, self.a_shutdown, self.a_reboot, self.a_pause):
            tb.addAction(a)
        power = QToolButton()
        power.setIcon(icon("system-shutdown"))
        power.setToolTip("More power actions")
        power.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(power)
        pm.addAction(self.a_shutdown_agent)
        pm.addAction(self.a_reset)
        pm.addAction(self.a_force)
        power.setMenu(pm)
        tb.addWidget(power)
        tb.addSeparator()
        tb.addAction(self.a_cad)
        keys = QToolButton()
        keys.setText("Keys")
        keys.setIcon(icon("input-keyboard"))
        keys.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        keys.setPopupMode(QToolButton.InstantPopup)
        keys.setMenu(self.keys_menu)
        tb.addWidget(keys)
        tb.addAction(self.a_type)
        tb.addAction(self.a_grab)
        tb.addSeparator()
        scale = QToolButton()
        scale.setText("Scale")
        scale.setIcon(icon("zoom-fit-best"))
        scale.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        scale.setPopupMode(QToolButton.InstantPopup)
        smenu = QMenu(scale)
        for a in self.scale_actions.values():
            smenu.addAction(a)
        smenu.addSeparator()
        smenu.addAction(self.a_smooth)
        smenu.addAction(self.a_auto_resize)
        smenu.addAction(self.a_resize_now)
        smenu.addAction(self.a_fit_window)
        scale.setMenu(smenu)
        tb.addWidget(scale)
        tb.addAction(self.a_fullscreen)
        tb.addAction(self.a_screenshot)
        tb.addAction(self.a_audio)
        self.toolbar = tb

        # Actions only reachable through the menu bar/toolbar lose their
        # shortcuts when those are hidden (fullscreen); register them on the
        # window too.
        for a in self.findChildren(QAction):
            if not a.shortcut().isEmpty():
                self.addAction(a)

        self.fs_bar = FullscreenBar(
            self.view, [self.a_cad, self.a_screenshot, self.a_fullscreen], self)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.sb_vm = QLabel()
        self.sb_lg = QLabel()
        self.sb_res = QLabel()
        self.sb_fps = QLabel()
        self.sb_input = QLabel()
        self.sb_leds = QLabel()
        self.sb_clip = QLabel()
        self.sb_audio = QLabel()
        for w in (self.sb_vm, self.sb_lg, self.sb_res, self.sb_fps):
            sb.addWidget(w)
        for w in (self.sb_input, self.sb_leds, self.sb_clip, self.sb_audio):
            sb.addPermanentWidget(w)

    def _wire(self):
        m = self.manager
        m.connectionChanged.connect(self._on_connection)
        m.domainsChanged.connect(self._refresh_list)
        m.domainEvent.connect(self._on_domain_event)
        m.taskFinished.connect(self._on_task)
        self.machines.currentItemChanged.connect(self._on_select)
        self.machines.customContextMenuRequested.connect(self._machine_menu)
        self.machines.itemDoubleClicked.connect(
            lambda it: self.manager.start(it.data(Qt.UserRole))
            if self.current_state == libvirt.VIR_DOMAIN_SHUTOFF else None)

        s = self.session
        s.stateChanged.connect(self._on_lg_state)
        s.connected.connect(self._on_lg_connected)
        s.inputChanged.connect(self._update_input_status)

        v = self.view
        v.statsUpdated.connect(self._on_stats)
        v.formatChanged.connect(self._on_format)
        v.hostAction.connect(self._on_host_action)
        v.filesDropped.connect(self._upload_files)
        v.grabChanged.connect(self._on_grab_changed)
        v.installEventFilter(self)

        self.clip.activity.connect(lambda t: self.statusBar().showMessage(t, 3000))
        self.clip.availabilityChanged.connect(lambda _a: self._update_clip_status())
        self.audio.stateChanged.connect(self._update_audio_status)
        self._update_clip_status()
        self._update_audio_status(self.audio.state)

    # -------------------------------------------------------------- libvirt
    def _connect_libvirt(self):
        if not self.manager.connect():
            return
        self._refresh_list()
        names = [self.machines.item(i).data(Qt.UserRole)
                 for i in range(self.machines.count())]
        last = self.settings["last_vm"]
        pick = last if last in names else None
        if pick is None:
            # prefer a running VM that has Looking Glass configured
            for d in self._domains:
                if d.has_lg and vmlib.is_active_state(d.state):
                    pick = d.name
                    break
            else:
                pick = names[0] if names else None
        if pick:
            self._select(pick)

    def _on_connection(self, ok, msg):
        if ok:
            self.conn_label.setText(f"Connected to {msg}")
        else:
            self.conn_label.setText(f"libvirt: {msg}")
            self.view.set_status("Not connected to libvirt", msg)
            QTimer.singleShot(5000, self._reconnect_libvirt)

    def _reconnect_libvirt(self):
        if self.manager.conn is None and self.manager.connect():
            self._refresh_list()
            if self.current:
                self._apply_vm_state()

    def _change_uri(self):
        uri, ok = QInputDialog.getText(self, "libvirt connection", "URI:",
                                       text=self.manager.uri)
        if ok and uri.strip():
            self.session.stop()
            self.manager.close()
            self.manager.uri = uri.strip()
            self.settings["libvirt_uri"] = self.manager.uri
            self.current = None
            self._connect_libvirt()

    def _refresh_list(self):
        self._domains = self.manager.domains()
        sel = self.current
        self.machines.blockSignals(True)
        self.machines.clear()
        for d in self._domains:
            it = QListWidgetItem(dot_icon(STATE_COLORS.get(d.state, "#6b6a64")),
                                 f"{d.name}\n{d.state_name}"
                                 f"{'' if d.has_lg else ' · no Looking Glass'}")
            it.setData(Qt.UserRole, d.name)
            it.setToolTip(f"{d.vcpus} vCPU · "
                          f"{d.memory_kib / 1048576:.0f} GiB")
            self.machines.addItem(it)
            if d.name == sel:
                self.machines.setCurrentItem(it)
        self.machines.blockSignals(False)
        if sel and sel not in [d.name for d in self._domains]:
            self.current = None
            self.session.stop()
            self.audio.stop()
        self._update_actions()

    def _select(self, name):
        for i in range(self.machines.count()):
            if self.machines.item(i).data(Qt.UserRole) == name:
                self.machines.setCurrentRow(i)
                return

    def _on_select(self, item, _prev=None):
        name = item.data(Qt.UserRole) if item else None
        if name == self.current:
            return
        self.current = name
        self.current_state = None
        self.settings["last_vm"] = name
        self.session.stop()
        self.audio.stop()
        self.view.reset_frame()
        for p in self.panels:
            p.set_domain(name)
        self.overview.set_lg_info(None)
        self._apply_vm_state()

    def _summary(self, name):
        for d in getattr(self, "_domains", []):
            if d.name == name:
                return d
        return None

    def _apply_vm_state(self):
        """Bring the LG session/audio in line with the selected VM's state."""
        name = self.current
        dom = self.manager.lookup(name) if name else None
        if dom is None:
            self.current_state = None
            self.session.stop()
            self.view.set_status("No virtual machine selected",
                                 "Pick a VM from the sidebar.")
            self._update_actions()
            return
        try:
            state, _ = dom.state()
            xml = dom.XMLDesc(0)
        except libvirt.libvirtError as e:
            self.view.set_status("Error", str(e))
            return
        self.current_state = state
        self.setWindowTitle(f"{name} — {APP_NAME}")
        self.sb_vm.setText(f"{name}: {vmlib.STATE_NAMES.get(state, '?')}")

        shm = self.settings.vm(name).get("shm") or vmlib.lg_shm_path(xml)[0]
        active = vmlib.is_active_state(state)
        if active and shm:
            if self.session.shm_path != shm:
                self.session.start(shm)
            self._on_lg_state(self.session.state)
            if state == libvirt.VIR_DOMAIN_PAUSED:
                self.view.set_status("Paused", f"{name} is paused. "
                                     "Resume it to continue.")
        else:
            self.session.stop()
            if not shm:
                self.view.set_status(
                    "Looking Glass is not configured for this VM",
                    "Add an IVSHMEM <shmem> device (or kvmfr) to the domain "
                    "to display it here. All other controls still work.")
            elif state == libvirt.VIR_DOMAIN_SHUTOFF:
                if self.isFullScreen():
                    self.toggle_fullscreen(False)
                self.view.set_status(f"{name} is shut off",
                                     "Press Start (or double-click it in the "
                                     "sidebar) to boot it.")
            else:
                self.view.set_status(f"{name}: "
                                     f"{vmlib.STATE_NAMES.get(state, '?')}")
        self._ensure_audio(xml if active else None)
        self._update_actions()

    def _ensure_audio(self, xml=None):
        if not (self.settings["audio"] and self.current and
                self.current_state is not None and
                vmlib.is_active_state(self.current_state)):
            if self.audio.state != "off":
                self.audio.stop()
            return
        if self.audio.state in ("playing", "connecting"):
            return
        if xml is None:
            dom = self.manager.lookup(self.current)
            if dom is None:
                return
            try:
                xml = dom.XMLDesc(0)
            except libvirt.libvirtError:
                return
        self.audio.start(spice_uri(xml), self.settings["microphone"])

    def _on_domain_event(self, name, event, detail):
        if name != self.current:
            return
        if event == libvirt.VIR_DOMAIN_EVENT_STARTED:
            self.audio.stop()
        self._apply_vm_state()
        for p in (self.overview, self.devices, self.snapshots):
            p.refresh()

    def _on_task(self, label, ok, msg):
        if ok:
            self.statusBar().showMessage(f"{label}: done", 4000)
        else:
            self.statusBar().showMessage(f"{label}: failed", 6000)
            QMessageBox.warning(self, label, msg)
        self.snapshots.refresh()
        self.devices.refresh()
        self._refresh_list()

    def _vm(self, fn):
        return lambda *_: fn(self.current) if self.current else None

    def _confirm_vm(self, title, text, fn):
        def run(*_):
            if self.current and QMessageBox.question(
                    self, title, text.format(self.current)) == QMessageBox.Yes:
                fn(self.current)
        return run

    def _toggle_pause(self):
        if not self.current:
            return
        if self.current_state == libvirt.VIR_DOMAIN_PAUSED:
            self.manager.resume(self.current)
        else:
            self.manager.pause(self.current)

    def _toggle_autostart(self, on):
        if self.current:
            self.manager.set_autostart(self.current, on)

    def _update_actions(self):
        st = self.current_state
        has = self.current is not None and st is not None
        running = st == libvirt.VIR_DOMAIN_RUNNING
        paused = st == libvirt.VIR_DOMAIN_PAUSED
        off = st in (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_CRASHED)
        self.a_start.setEnabled(has and off)
        for a in (self.a_shutdown, self.a_shutdown_agent, self.a_reboot):
            a.setEnabled(has and running)
        self.a_pause.setEnabled(has and (running or paused))
        self.a_pause.setChecked(paused)
        self.a_pause.setText("Resume" if paused else "Pause")
        self.a_pause.setIcon(icon("media-playback-start" if paused
                                  else "media-playback-pause"))
        self.a_reset.setEnabled(has and (running or paused))
        self.a_force.setEnabled(has and not off)
        summ = self._summary(self.current) if self.current else None
        self.a_autostart.setEnabled(bool(summ and summ.persistent))
        self.a_autostart.blockSignals(True)
        self.a_autostart.setChecked(bool(summ and summ.autostart))
        self.a_autostart.blockSignals(False)
        lg_ok = self.session.is_connected
        for a in (self.a_cad, self.a_type, self.a_grab, self.a_screenshot,
                  self.a_resize_now, self.a_fit_window, self.a_wake):
            a.setEnabled(lg_ok)
        self.keys_menu.setEnabled(lg_ok)

    def _machine_menu(self, pos):
        item = self.machines.itemAt(pos)
        if not item:
            return
        self._select(item.data(Qt.UserRole))
        m = QMenu(self)
        for a in (self.a_start, self.a_shutdown, self.a_reboot, self.a_pause,
                  self.a_reset, self.a_force):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_autostart)
        m.addAction(self._act("Set shared memory path…", self._set_shm_path))
        m.exec(self.machines.mapToGlobal(pos))

    def _set_shm_path(self):
        if not self.current:
            return
        cur = self.settings.vm(self.current).get("shm", "")
        path, ok = QInputDialog.getText(
            self, "Shared memory path",
            "Override the IVSHMEM / KVMFR path for this VM\n"
            "(leave empty to auto-detect from the domain XML):", text=cur)
        if ok:
            self.settings.set_vm(self.current, "shm", path.strip() or None)
            self.session.stop()
            self._apply_vm_state()

    # -------------------------------------------------------------- LG
    def _on_lg_state(self, state):
        self.sb_lg.setText(f"Looking Glass: {lg.STATE_NAMES.get(state, '?')}")
        if state == lg.STATE_NO_DEVICE:
            self.view.set_status(
                "Shared memory device not found",
                f"Couldn't open {self.session.shm_path}. Check the path and "
                "that your user can read/write it.")
        elif state == lg.STATE_WAITING:
            self.view.set_status(
                "Waiting for the Looking Glass host…",
                "The VM is running but the Looking Glass host application "
                "hasn't started yet. It usually appears once Windows boots.")
        elif state == lg.STATE_BAD_VERSION:
            info = self.session.client.session()
            self.view.set_status(
                "Looking Glass version mismatch",
                info.mismatch or "The guest host application uses a different "
                "protocol version than this client.")
        if state != lg.STATE_CONNECTED:
            self.sb_res.setText("")
            self.sb_fps.setText("")
            self.view.set_grab(False)
        self._update_input_status()
        self._update_clip_status()
        self._update_actions()

    def _on_lg_connected(self, info):
        self.overview.set_lg_info(info)
        self.statusBar().showMessage(
            f"Connected to Looking Glass host {info.version} "
            f"({info.capture})", 5000)
        self._last_window_size = None
        if self.settings["auto_resize_guest"]:
            self._resize_timer.start()

    def _on_format(self, f):
        self.sb_res.setText(f"{f.screenWidth}×{f.screenHeight} "
                            f"{lg.FRAME_TYPE_NAMES.get(f.type, '')}"
                            f"{' HDR' if f.hdr else ''}")

    def _on_stats(self, fps, ups):
        if self.session.is_connected:
            self.sb_fps.setText(f"{ups:.0f} fps")
            self.perf.push_lg(fps, ups)

    def _update_input_status(self):
        c = self.session.client
        if not self.session.is_connected:
            self.sb_input.setText("")
            self.sb_leds.setText("")
            return
        self.sb_input.setText("Input ✓" if c.input_available()
                              else "Input unavailable")
        leds = c.input_leds()
        if leds >= 0:
            parts = []
            if leds & lg.LED_CAPS:
                parts.append("CAPS")
            if leds & lg.LED_NUM:
                parts.append("NUM")
            if leds & lg.LED_SCROLL:
                parts.append("SCRL")
            self.sb_leds.setText(" ".join(parts))
        else:
            self.sb_leds.setText("")

    def _update_clip_status(self):
        if not self.settings["clipboard_sync"]:
            self.sb_clip.setText("Clipboard off")
        elif self.session.is_connected:
            self.sb_clip.setText("Clipboard ✓" if self.clip.available else
                                 "Clipboard waiting")
            self.sb_clip.setToolTip(
                "" if self.clip.available else
                "The guest clipboard helper runs inside a logged-in Windows "
                "session. Until then, use Input → Type Clipboard Text.")
        else:
            self.sb_clip.setText("")

    def _update_audio_status(self, state):
        self.sb_audio.setText({
            "playing": "Audio ✓" + (" + mic" if self.settings["microphone"] else ""),
            "connecting": "Audio…",
            "error": "Audio error",
            "unavailable": "No SPICE audio",
        }.get(state, ""))

    # -------------------------------------------------------------- view
    def _set_scale(self, mode):
        self.settings["scale_mode"] = mode
        self.view.set_scale_mode(mode)

    def _toggle_smooth(self, on):
        self.settings["smooth_scaling"] = on
        self.view.smooth = on
        self.view.update()

    def _toggle_stats(self, on):
        self.settings["show_stats_overlay"] = on
        self.view.set_show_stats(on)

    def _toggle_grab(self, on):
        self.view.set_grab(on)

    def _on_grab_changed(self, on):
        self.a_grab.setChecked(on)
        self.statusBar().showMessage(
            "Keyboard captured — press Pause to release" if on
            else "Keyboard released", 3000)

    def _toggle_auto_resize(self, on):
        self.settings["auto_resize_guest"] = on
        if on:
            self._send_window_size()

    def _toggle_clip(self, on):
        self.settings["clipboard_sync"] = on
        self.clip.set_enabled(on)
        self._update_clip_status()

    def _toggle_audio(self, on):
        self.settings["audio"] = on
        if on:
            self._ensure_audio()
        else:
            self.audio.stop()

    def _toggle_mic(self, on):
        self.settings["microphone"] = on
        self.audio.set_mic(on)
        self._update_audio_status(self.audio.state)

    def eventFilter(self, obj, ev):
        if obj is self.view:
            t = ev.type()
            if t == ev.Type.Resize:
                if self.settings["auto_resize_guest"]:
                    self._resize_timer.start()
                if self.fs_bar.isVisible():
                    self.fs_bar.recenter()
            elif t == ev.Type.MouseMove and self.isFullScreen() and \
                    ev.position().y() <= 2:
                self.fs_bar.reveal(self._fs_text())
        return super().eventFilter(obj, ev)

    def _send_window_size(self, force=False):
        """Ask the guest to match the view size in physical pixels. Like the
        stock client this is debounced (resize timer) and only sent when the
        size changed, unless forced from the menu."""
        if not self.session.is_connected:
            return
        dpr = self.view.devicePixelRatioF()
        w = round(self.view.width() * dpr)
        h = round(self.view.height() * dpr)
        if not force and (w, h) == self._last_window_size:
            return
        f = self.view.frame
        if not force and f is not None and (f.screenWidth, f.screenHeight) == (w, h):
            self._last_window_size = (w, h)
            return
        rc = self.session.client.window_size(w, h)
        if rc == 0:
            self._last_window_size = (w, h)
            self.statusBar().showMessage(f"Requested guest resolution {w}×{h}",
                                         3000)
        else:
            self.statusBar().showMessage(
                "The guest did not accept a resolution change", 4000)

    def fit_window(self):
        size = self.view.preferred_size()
        if not size or self.isFullScreen():
            return
        if self.isMaximized():
            self.showNormal()
        extra_w = self.width() - self.view.width()
        extra_h = self.height() - self.view.height()
        avail = self.screen().availableGeometry()
        self.resize(min(size.width() + extra_w, avail.width()),
                    min(size.height() + extra_h, avail.height()))

    def toggle_fullscreen(self, on=None):
        if on is None or isinstance(on, bool) and on == self.isFullScreen():
            on = not self.isFullScreen()
        if on:
            self._fullscreen_saved = (self.machines_dock.isVisible(),
                                      self.panel_dock.isVisible(),
                                      self.isMaximized())
            self.machines_dock.hide()
            self.panel_dock.hide()
            self.toolbar.hide()
            self.menuBar().hide()
            self.statusBar().hide()
            self.showFullScreen()
            self.view.setFocus()
            QTimer.singleShot(200, lambda: self.fs_bar.reveal(
                self._fs_text(), hold_ms=3500))
        else:
            self.toolbar.show()
            self.menuBar().show()
            self.statusBar().show()
            sb, pd, maxed = self._fullscreen_saved or (True, True, False)
            self.machines_dock.setVisible(sb)
            self.panel_dock.setVisible(pd)
            self.fs_bar.hide()
            self.showMaximized() if maxed else self.showNormal()
        self.a_fullscreen.setChecked(on)
        self.a_fullscreen.setText("Exit Fullscreen" if on else "Fullscreen")

    def _fs_text(self):
        return (f"{self.current or ''}  ·  F11 or Pause+F to exit")

    def _on_host_action(self, action):
        if action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "toggle-panels":
            vis = not (self.machines_dock.isVisible() or
                       self.panel_dock.isVisible())
            self.machines_dock.setVisible(vis)
            self.panel_dock.setVisible(vis)
        elif action == "screenshot":
            self.screenshot()
        elif action == "type-clipboard":
            self.type_clipboard()
        elif action == "resize-guest":
            self._send_window_size()
        elif action == "toggle-stats":
            self.a_stats.trigger()
        elif action == "quit":
            self.close()

    # -------------------------------------------------------------- tools
    def screenshot(self):
        img = self.view.screenshot()
        if img.isNull():
            return
        folder = self.settings["screenshot_dir"]
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(folder, f"{self.current or 'guest'}-{stamp}.png")
        if img.save(path):
            self.statusBar().showMessage(f"Saved screenshot to {path}", 5000)
        else:
            QMessageBox.warning(self, "Screenshot", f"Couldn't write {path}")

    def type_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if not text:
            self.statusBar().showMessage("Host clipboard has no text", 3000)
            return
        if len(text) > 2000 and QMessageBox.question(
                self, "Type clipboard",
                f"Type {len(text)} characters into the guest?") != QMessageBox.Yes:
            return
        skipped = self.view.type_text(text)
        msg = f"Typed {len(text) - skipped} characters"
        if skipped:
            msg += f" ({skipped} non-US-keyboard characters skipped)"
        self.statusBar().showMessage(msg, 4000)

    def _upload_files(self, files):
        self.tabs.setCurrentWidget(self.guest)
        self.panel_dock.show()
        self.guest.upload(files)

    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts", (
            "While the guest display has focus every key goes to the guest.\n\n"
            "The host key combos work held (Pause+F) or as a prefix\n"
            "(tap Pause, then F) — the latter on laptop keyboards.\n\n"
            "Pause (tap)\tCapture / release the keyboard\n"
            "Pause+F\tToggle fullscreen\n"
            "Pause+D\tShow / hide side panels\n"
            "Pause+S\tScreenshot\n"
            "Pause+V\tType host clipboard text into guest\n"
            "Pause+R\tResize guest to window\n"
            "Pause+I\tToggle stats overlay\n"
            "Pause+Q\tQuit\n"
            "F11\tToggle fullscreen (always; use Send Keys for a guest F11)\n"
            "In fullscreen, touch the top screen edge for a control bar.\n\n"
            "Drop files on the guest display to copy them into the guest."))

    def about(self):
        QMessageBox.about(self, APP_NAME, (
            f"<h3>{APP_NAME}</h3>"
            "<p>A Looking Glass client with libvirt VM controls, guest-agent "
            "tools, USB passthrough, snapshots and SPICE audio.</p>"
            "<p>Video, cursor, input and clipboard use the Looking Glass LGMP "
            "transport (vendored from Looking Glass, GPL-2.0-or-later).</p>"))

    # -------------------------------------------------------------- close
    def closeEvent(self, e):
        if self.isFullScreen():
            self.toggle_fullscreen(False)
        self.settings.data["geometry"] = bytes(
            self.saveGeometry().toBase64()).decode()
        self.settings.data["window_state"] = bytes(
            self.saveState().toBase64()).decode()
        self.settings.data["show_sidebar"] = self.machines_dock.isVisible()
        self.settings.data["show_panel"] = self.panel_dock.isVisible()
        self.settings.save()
        self.view.set_grab(False)
        self.session.stop()
        self.audio.stop()
        self.manager.close()
        self.manager.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)


def main(argv):
    from PySide6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(argv)
    app.setApplicationName("virt-glass")
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("virt-glass")
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()
