"""Side-panel pages: overview, performance, snapshots, devices, guest tools."""

import datetime
import html
import os

import libvirt
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QVBoxLayout, QWidget)

from . import vm as vmlib
from .charts import StatTile, fmt_bytes


def _section(title):
    lab = QLabel(title)
    f = lab.font()
    f.setBold(True)
    lab.setFont(f)
    lab.setStyleSheet("color: #c3c2b7; margin-top: 8px;")
    return lab


class PanelBase(QWidget):
    """Panels receive the manager and the selected domain name."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.domain = None

    def set_domain(self, name):
        self.domain = name
        self.refresh()

    def refresh(self):
        pass

    def dom(self):
        return self.manager.lookup(self.domain) if self.domain else None


# --------------------------------------------------------------------------
class OverviewPanel(PanelBase):
    agentInfo = Signal(dict)

    def __init__(self, manager, parent=None):
        super().__init__(manager, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        lay.addWidget(self.view)
        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.xml_btn = QPushButton("View XML…")
        self.xml_btn.clicked.connect(self.show_xml)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.xml_btn)
        row.addStretch()
        lay.addLayout(row)
        self.lg_info = None
        self._agent = {}
        self._agent_busy = False
        self._has_channel = False
        self.agentInfo.connect(self._got_agent)
        # The agent is down while a guest boots or reboots, so a single ping
        # can leave "not responding" on screen. Keep retrying until it answers.
        self._agent_retry = QTimer(self, interval=5000,
                                   timeout=self._retry_agent)
        self._agent_retry.start()

    def set_lg_info(self, info):
        self.lg_info = info
        self._render()

    def refresh(self):
        self._agent = {}
        self._render()
        self._fetch_agent()

    def _fetch_agent(self):
        if self._agent_busy:
            return
        dom = self.dom()
        if dom is None or not dom.isActive():
            return
        name = self.domain
        self._agent_busy = True

        def fetch():
            try:
                ga = vmlib.GuestAgent(dom)
                if ga.ping():
                    info = ga.summary()
                    info["_domain"] = name
                else:
                    info = {"_domain": name, "_unavailable": True}
            except Exception:
                info = {"_domain": name, "_unavailable": True}
            self.agentInfo.emit(info)
        self.manager.pool.submit(fetch)

    def _retry_agent(self):
        if (self.isVisible() and self._has_channel
                and self._agent.get("_unavailable")):
            self._fetch_agent()

    def _got_agent(self, info):
        self._agent_busy = False
        if info.get("_domain") == self.domain:
            self._agent = info
            self._render()

    def _render(self):
        dom = self.dom()
        if dom is None:
            self.view.setHtml("<p>No VM selected.</p>")
            return
        try:
            d = vmlib.parse_details(dom)
            state, _ = dom.state()
        except libvirt.libvirtError as e:
            self.view.setHtml(f"<p>{html.escape(str(e))}</p>")
            return
        self._has_channel = d.has_agent_channel
        e = html.escape
        rows = []

        def sect(title):
            rows.append(f"<tr><td colspan=2 style='padding-top:10px'><b>{e(title)}"
                        f"</b></td></tr>")

        def kv(k, v):
            rows.append(f"<tr><td style='color:#c3c2b7;padding-right:12px'>"
                        f"{e(k)}</td><td>{v}</td></tr>")

        sect("Virtual machine")
        kv("Name", e(d.name))
        kv("State", e(vmlib.STATE_NAMES.get(state, "?")))
        kv("vCPUs", f"{d.vcpus} ({e(d.cpu_mode)}; {e(d.topology)})" if d.topology
           else str(d.vcpus))
        kv("Memory", fmt_bytes(d.memory_kib * 1024))
        kv("Machine", e(d.machine))
        kv("Firmware", e(d.firmware))
        for disk in d.disks:
            src = disk["source"] or "(empty)"
            kv(f"{disk['device'].title()} {disk['target']}",
               f"{e(src)} <span style='color:#8d8c84'>{e(disk['bus'])} "
               f"{e(disk['format'])}</span>")
        for nic in d.nics:
            kv("NIC", f"{e(nic['mac'])} → {e(nic['source'] or '')} "
                      f"<span style='color:#8d8c84'>{e(nic['model'])}</span>")
        for hd in d.hostdevs:
            kv("Passthrough", e(hd))
        for g in d.graphics:
            kv("Graphics", e(f"{g['type']} {g['listen']}:{g['port']}"))
        kv("LG shared memory", e(f"{d.shm_path} ({d.shm_size})") if d.shm_path
           else "<i>not configured</i>")

        if self.lg_info:
            i = self.lg_info
            sect("Looking Glass host")
            kv("Version", e(i.version))
            kv("Capture", e(i.capture))
            kv("Guest OS", e(i.os_name or i.os))
            kv("Guest CPU", e(f"{i.cpu_model} ({i.sockets}s/{i.cores}c/{i.cpus}t)"))
            feats = [n for bit, n in ((0x1, "cursor-pos"), (0x2, "window-size"),
                                      (0x4, "frame-schedule"), (0x8, "input"),
                                      (0x10, "clipboard")) if i.features & bit]
            kv("Features", e(", ".join(feats)))

        a = self._agent
        if a.get("_unavailable"):
            sect("Guest agent")
            kv("Status", "not responding" if d.has_agent_channel
               else "no agent channel configured")
        elif a:
            sect("Guest agent")
            os_ = a.get("osinfo") or {}
            if os_:
                kv("OS", e(f"{os_.get('version') or os_.get('name', '')} "
                           f"(build {os_.get('kernel-release', '?')})"))
            if "hostname" in a:
                kv("Hostname", e(a["hostname"]))
            users = a.get("users")
            if users is not None:
                kv("Logged in", e(", ".join(
                    (u.get("domain") + "\\" if u.get("domain") else "") + u["user"]
                    for u in users) or "nobody"))
            tz = a.get("timezone")
            if tz:
                kv("Time zone", e(f"{tz.get('zone', '')} "
                                  f"(UTC{tz.get('offset', 0) / 3600:+g})"))
            for iface in a.get("interfaces") or []:
                ips = [ip["ip-address"] for ip in iface.get("ip-addresses", [])
                       if ip.get("ip-address-type") == "ipv4"
                       and not ip["ip-address"].startswith("127.")]
                if ips:
                    kv(iface.get("name", "nic"), e(", ".join(ips)))
            for fs in a.get("fsinfo") or []:
                total = fs.get("total-bytes")
                used = fs.get("used-bytes")
                if total and used is not None and fs.get("mountpoint"):
                    pct = used / total * 100
                    kv(f"Disk {fs['mountpoint']}",
                       f"{fmt_bytes(used)} / {fmt_bytes(total)} "
                       f"({pct:.0f}%) {e(fs.get('type', ''))}")
        self.view.setHtml("<table cellspacing=0>" + "".join(rows) + "</table>")

    def show_xml(self):
        dom = self.dom()
        if dom is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{self.domain} — domain XML")
        dlg.resize(760, 640)
        lay = QVBoxLayout(dlg)
        ed = QPlainTextEdit(dom.XMLDesc(0))
        ed.setReadOnly(True)
        ed.setFont(QFont("monospace"))
        lay.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        copy = bb.addButton("Copy", QDialogButtonBox.ActionRole)
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(ed.toPlainText()))
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()


# --------------------------------------------------------------------------
class PerformancePanel(PanelBase):
    def __init__(self, manager, parent=None):
        super().__init__(manager, parent)
        self.tracker = vmlib.StatsTracker()
        self.cpu = StatTile("CPU", fixed_max=100, formatter=lambda v: f"{v:.1f}%")
        self.mem = StatTile("Memory (host RSS)",
                            formatter=lambda v: fmt_bytes(v))
        self.disk = StatTile("Disk I/O", ("Read", "Write"),
                             formatter=lambda v: fmt_bytes(v, True))
        self.net = StatTile("Network", ("Receive", "Send"),
                            formatter=lambda v: fmt_bytes(v, True))
        self.frames = StatTile("Looking Glass frames",
                               formatter=lambda v: f"{v:.0f} fps")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for t in (self.cpu, self.mem, self.disk, self.net, self.frames):
            lay.addWidget(t)
        lay.addStretch()
        self._in_flight = False
        manager.statsReady.connect(self._got_sample)
        self.timer = QTimer(self, interval=1000, timeout=self.sample)
        self.timer.start()

    def set_domain(self, name):
        if name != self.domain:
            self.tracker = vmlib.StatsTracker()
            for t in (self.cpu, self.mem, self.disk, self.net, self.frames):
                t.clear()
        super().set_domain(name)

    def push_lg(self, fps, ups):
        self.frames.push([ups], f"{ups:.0f} fps", f"render {fps:.0f} fps")

    def sample(self):
        # libvirt calls run on the worker pool so a busy domain can't stall
        # the UI; skip a tick if the previous sample is still outstanding
        if self.domain and not self._in_flight:
            self._in_flight = True
            self.manager.sample_stats(self.domain, self.tracker)
            QTimer.singleShot(5000, self._clear_in_flight)

    def _clear_in_flight(self):
        self._in_flight = False

    def _got_sample(self, name, s):
        self._in_flight = False
        if name != self.domain:
            return
        if "cpu_pct" not in s:
            return
        self.cpu.push([s["cpu_pct"]], f"{s['cpu_pct']:.1f}%",
                      f"{s['vcpus']} vCPUs")
        rss = s["mem_rss"]
        self.mem.push([rss], fmt_bytes(rss), f"of {fmt_bytes(s['mem_total'])}")
        self.disk.push([s["rd_rate"], s["wr_rate"]],
                       f"{fmt_bytes(s['rd_rate'] + s['wr_rate'], True)}",
                       f"R {fmt_bytes(s['rd_rate'], True)} · "
                       f"W {fmt_bytes(s['wr_rate'], True)}")
        self.net.push([s["rx_rate"], s["tx_rate"]],
                      f"{fmt_bytes(s['rx_rate'] + s['tx_rate'], True)}",
                      f"↓ {fmt_bytes(s['rx_rate'], True)} · "
                      f"↑ {fmt_bytes(s['tx_rate'], True)}")


# --------------------------------------------------------------------------
class SnapshotsPanel(PanelBase):
    def __init__(self, manager, parent=None):
        super().__init__(manager, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Created", "State"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table)
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("color: #c3c2b7")
        lay.addWidget(self.desc)
        row = QHBoxLayout()
        self.create_btn = QPushButton("Take snapshot…")
        self.revert_btn = QPushButton("Revert")
        self.delete_btn = QPushButton("Delete")
        for b in (self.create_btn, self.revert_btn, self.delete_btn):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        note = QLabel("Note: VMs with PCI passthrough or UEFI pflash NVRAM "
                      "often can't take internal snapshots while running; "
                      "shut the VM down first or use a disk-only snapshot.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8d8c84; font-size: small")
        lay.addWidget(note)
        self.create_btn.clicked.connect(self.create)
        self.revert_btn.clicked.connect(self.revert)
        self.delete_btn.clicked.connect(self.delete)
        self.table.itemSelectionChanged.connect(self._selected)
        self.snaps = []

    def refresh(self):
        self.table.setRowCount(0)
        self.snaps = []
        if not self.domain:
            return
        try:
            self.snaps = self.manager.snapshots(self.domain)
        except libvirt.libvirtError as e:
            self.desc.setText(str(e))
            return
        for s in self.snaps:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(("● " if s.current else "") + s.name)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(
                datetime.datetime.fromtimestamp(s.created).strftime(
                    "%Y-%m-%d %H:%M")))
            self.table.setItem(r, 2, QTableWidgetItem(s.state))
        self.desc.setText("" if self.snaps else "No snapshots.")
        self._selected()

    def _current(self):
        rows = self.table.selectionModel().selectedRows()
        return self.snaps[rows[0].row()] if rows else None

    def _selected(self):
        s = self._current()
        self.revert_btn.setEnabled(s is not None)
        self.delete_btn.setEnabled(s is not None)
        if s:
            self.desc.setText(s.description or "(no description)")

    def create(self):
        if not self.domain:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Take snapshot")
        form = QFormLayout(dlg)
        name = QLineEdit(datetime.datetime.now().strftime("snap-%Y%m%d-%H%M%S"))
        desc = QLineEdit()
        disk_only = QCheckBox("Disk only (external, no memory state)")
        form.addRow("Name", name)
        form.addRow("Description", desc)
        form.addRow("", disk_only)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.Accepted and name.text().strip():
            self.manager.create_snapshot(self.domain, name.text().strip(),
                                         desc.text(), disk_only.isChecked())

    def revert(self):
        s = self._current()
        if s and QMessageBox.question(
                self, "Revert snapshot",
                f"Revert {self.domain} to snapshot '{s.name}'?\n\n"
                "The current state of the VM will be lost.") == QMessageBox.Yes:
            self.manager.revert_snapshot(self.domain, s.name)

    def delete(self):
        s = self._current()
        if s and QMessageBox.question(
                self, "Delete snapshot",
                f"Delete snapshot '{s.name}'?") == QMessageBox.Yes:
            self.manager.delete_snapshot(self.domain, s.name)


# --------------------------------------------------------------------------
class DevicesPanel(PanelBase):
    def __init__(self, manager, parent=None):
        super().__init__(manager, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(_section("USB passthrough"))
        hint = QLabel("Tick a host USB device to hot-plug it into the VM. "
                      "It is unavailable to the host while attached.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8d8c84")
        lay.addWidget(hint)
        self.usb = QListWidget()
        self.usb.itemChanged.connect(self._usb_toggled)
        lay.addWidget(self.usb)
        rescan = QPushButton("Rescan USB")
        rescan.clicked.connect(self.refresh)
        lay.addWidget(rescan, 0, Qt.AlignLeft)

        lay.addWidget(_section("Optical drives"))
        self.cd_box = QVBoxLayout()
        lay.addLayout(self.cd_box)
        lay.addStretch()
        self._updating = False

    def refresh(self):
        self._updating = True
        self.usb.clear()
        dom = self.dom()
        attached = set()
        if dom is not None:
            try:
                attached = vmlib.attached_usb(dom)
            except libvirt.libvirtError:
                pass
        for dev in vmlib.host_usb_devices():
            dev.attached = (dev.vendor, dev.product) in attached
            item = QListWidgetItem(
                f"{dev.name}  [{dev.vendor:04x}:{dev.product:04x}]")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if dev.attached else Qt.Unchecked)
            item.setData(Qt.UserRole, dev)
            if dom is None:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.usb.addItem(item)
        self._updating = False

        while self.cd_box.count():
            w = self.cd_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        if dom is None:
            return
        try:
            drives = vmlib.cdrom_drives(dom)
        except libvirt.libvirtError:
            drives = []
        if not drives:
            self.cd_box.addWidget(QLabel("No CD/DVD drives."))
        for drv in drives:
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(f"<b>{drv.target}</b> ({drv.bus}): "
                         f"{html.escape(os.path.basename(drv.source)) if drv.source else '<i>empty</i>'}")
            lab.setToolTip(drv.source or "")
            row.addWidget(lab, 1)
            ins = QPushButton("Insert…")
            ins.clicked.connect(lambda _=False, d=drv: self._insert(d))
            ej = QPushButton("Eject")
            ej.setEnabled(bool(drv.source))
            ej.clicked.connect(lambda _=False, d=drv: self.manager.change_media(
                self.domain, d, None))
            row.addWidget(ins)
            row.addWidget(ej)
            self.cd_box.addWidget(w)

    def _usb_toggled(self, item):
        if self._updating or not self.domain:
            return
        dev = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.manager.attach_usb(self.domain, dev)
        else:
            self.manager.detach_usb(self.domain, dev)

    def _insert(self, drv):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose disc image", os.path.expanduser("~"),
            "Disc images (*.iso *.img);;All files (*)")
        if path:
            self.manager.change_media(self.domain, drv, path)


# --------------------------------------------------------------------------
class GuestPanel(PanelBase):
    """Guest-agent powered tools."""

    commandDone = Signal(str)
    uploadProgress = Signal(str)

    def __init__(self, manager, settings, parent=None):
        super().__init__(manager, parent)
        self.settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        lay.addWidget(_section("Run command in guest"))
        row = QHBoxLayout()
        self.shell = QComboBox()
        self.shell.addItems(["PowerShell", "cmd.exe", "Program"])
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("e.g. Get-Process | Sort CPU -desc | select -first 5")
        self.cmd.returnPressed.connect(self.run)
        run = QPushButton("Run")
        run.clicked.connect(self.run)
        row.addWidget(self.shell)
        row.addWidget(self.cmd, 1)
        row.addWidget(run)
        lay.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("monospace"))
        self.output.setMaximumBlockCount(5000)
        lay.addWidget(self.output, 1)

        lay.addWidget(_section("Files & time"))
        row = QHBoxLayout()
        up = QPushButton("Send files to guest…")
        up.clicked.connect(self.pick_upload)
        dl = QPushButton("Fetch file from guest…")
        dl.clicked.connect(self.download)
        ts = QPushButton("Sync guest clock")
        ts.clicked.connect(self.sync_time)
        row.addWidget(up)
        row.addWidget(dl)
        row.addWidget(ts)
        row.addStretch()
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Upload folder:"))
        self.upload_dir = QLineEdit(settings["upload_dir"])
        self.upload_dir.editingFinished.connect(
            lambda: settings.__setitem__("upload_dir", self.upload_dir.text()))
        row.addWidget(self.upload_dir, 1)
        lay.addLayout(row)
        tip = QLabel("Tip: drag files onto the guest display to upload them.")
        tip.setStyleSheet("color: #8d8c84")
        lay.addWidget(tip)

        self.commandDone.connect(self._append)
        self.uploadProgress.connect(self._append)

    def _append(self, text):
        self.output.appendPlainText(text)

    def _agent(self):
        dom = self.dom()
        if dom is None or not dom.isActive():
            self._append("VM is not running.")
            return None
        return vmlib.GuestAgent(dom)

    def run(self):
        text = self.cmd.text().strip()
        ga = self._agent()
        if not text or ga is None:
            return
        kind = self.shell.currentText()
        if kind == "PowerShell":
            path, args = "powershell.exe", ["-NoProfile", "-NonInteractive",
                                            "-Command", text]
        elif kind == "cmd.exe":
            path, args = "cmd.exe", ["/c", text]
        else:
            parts = text.split()
            path, args = parts[0], parts[1:]
        self._append(f"> {text}")

        def work():
            try:
                code, out, err = ga.exec(path, args, timeout=120)
                msg = out.decode("utf-8", errors="replace")
                if err:
                    msg += err.decode("utf-8", errors="replace")
                msg = msg.replace("\r\n", "\n").rstrip()
                self.commandDone.emit((msg + "\n" if msg else "") +
                                      f"[exit code {code}]")
            except vmlib.AgentError as e:
                self.commandDone.emit(f"[agent error] {e}")
            except Exception as e:  # noqa: BLE001 - surface unexpected replies
                self.commandDone.emit(f"[error] {type(e).__name__}: {e}")
        self.manager.pool.submit(work)

    def pick_upload(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Send files to guest",
                                                os.path.expanduser("~"))
        if files:
            self.upload(files)

    def upload(self, files):
        ga = self._agent()
        if ga is None:
            return
        target = self.upload_dir.text().rstrip("\\/") or "C:\\Users\\Public"
        sep = "\\" if ("\\" in target or ":" in target) else "/"

        def work():
            for f in files:
                if not os.path.isfile(f):
                    self.uploadProgress.emit(f"Skipping {f} (not a file)")
                    continue
                dest = f"{target}{sep}{os.path.basename(f)}"
                try:
                    last = [0]

                    def prog(sent, total):
                        pct = int(sent * 100 / total) if total else 100
                        if pct - last[0] >= 25:
                            last[0] = pct
                            self.uploadProgress.emit(f"  {pct}% …")
                    self.uploadProgress.emit(f"Uploading {f} → {dest}")
                    n = ga.upload(f, dest, progress=prog)
                    self.uploadProgress.emit(f"Uploaded {fmt_bytes(n)} to {dest}")
                except (vmlib.AgentError, OSError) as e:
                    self.uploadProgress.emit(f"Upload of {f} failed: {e}")
        self.manager.pool.submit(work)

    def download(self):
        ga = self._agent()
        if ga is None:
            return
        src = QLineEditDialog.ask(self, "Fetch file from guest",
                                  "Guest path:", "C:\\Windows\\win.ini")
        if not src:
            return
        name = src.replace("\\", "/").rsplit("/", 1)[-1] or "download"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save as", os.path.join(os.path.expanduser("~"), name))
        if not dest:
            return

        def work():
            try:
                n = ga.download(src, dest)
                self.uploadProgress.emit(f"Fetched {fmt_bytes(n)} → {dest}")
            except (vmlib.AgentError, OSError) as e:
                self.uploadProgress.emit(f"Fetch failed: {e}")
        self.manager.pool.submit(work)

    def sync_time(self):
        ga = self._agent()
        if ga is None:
            return

        def work():
            try:
                ga.sync_time()
                self.commandDone.emit("Guest clock synchronised with host.")
            except vmlib.AgentError as e:
                self.commandDone.emit(f"Time sync failed: {e}")
        self.manager.pool.submit(work)


class QLineEditDialog:
    @staticmethod
    def ask(parent, title, label, text=""):
        dlg = QDialog(parent)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        ed = QLineEdit(text)
        ed.setMinimumWidth(380)
        form.addRow(label, ed)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        return ed.text().strip() if dlg.exec() == QDialog.Accepted else None


class ScrollPanel(QScrollArea):
    def __init__(self, inner, parent=None):
        super().__init__(parent)
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
