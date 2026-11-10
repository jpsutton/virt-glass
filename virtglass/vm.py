"""libvirt integration: domain listing, lifecycle, stats, snapshots, devices
and the QEMU guest agent."""

import base64
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import libvirt
import libvirt_qemu
from PySide6.QtCore import QObject, Signal

STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "No state",
    libvirt.VIR_DOMAIN_RUNNING: "Running",
    libvirt.VIR_DOMAIN_BLOCKED: "Blocked",
    libvirt.VIR_DOMAIN_PAUSED: "Paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "Shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "Shut off",
    libvirt.VIR_DOMAIN_CRASHED: "Crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "Suspended",
}

RUNNING = libvirt.VIR_DOMAIN_RUNNING
PAUSED = libvirt.VIR_DOMAIN_PAUSED
SHUTOFF = libvirt.VIR_DOMAIN_SHUTOFF


def is_active_state(state):
    return state not in (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_CRASHED,
                         libvirt.VIR_DOMAIN_NOSTATE)


@dataclass
class DomainSummary:
    name: str
    uuid: str
    state: int
    vcpus: int
    memory_kib: int
    persistent: bool
    autostart: bool
    has_lg: bool

    @property
    def state_name(self):
        return STATE_NAMES.get(self.state, "Unknown")


@dataclass
class Snapshot:
    name: str
    created: int
    state: str
    description: str
    current: bool
    parent: str | None


@dataclass
class CdromDrive:
    target: str
    bus: str
    source: str | None


@dataclass
class UsbDevice:
    vendor: int
    product: int
    bus: int
    dev: int
    name: str
    attached: bool = False


@dataclass
class DomainDetails:
    name: str
    uuid: str
    os_type: str
    machine: str
    firmware: str
    vcpus: int
    cpu_mode: str
    topology: str
    memory_kib: int
    disks: list = field(default_factory=list)
    nics: list = field(default_factory=list)
    hostdevs: list = field(default_factory=list)
    graphics: list = field(default_factory=list)
    shm_path: str | None = None
    shm_size: str | None = None
    has_agent_channel: bool = False


# --------------------------------------------------------------------------
# XML helpers

def lg_shm_path(xml):
    """Find the Looking Glass shared memory path from a domain's XML."""
    root = ET.fromstring(xml)
    for shm in root.findall("./devices/shmem"):
        name = shm.get("name")
        if name:
            return f"/dev/shm/{name}", _shm_size(shm)
    # kvmfr / memory-backend-file passed via qemu:commandline
    ns = {"qemu": "http://libvirt.org/schemas/domain/qemu/1.0"}
    for arg in root.findall("./qemu:commandline/qemu:arg", ns):
        value = arg.get("value", "")
        if "mem-path=" in value:
            for part in value.split(","):
                if part.startswith("mem-path="):
                    return part.split("=", 1)[1], None
    for arg in root.findall(".//qemu:override//qemu:property", ns):
        if arg.get("name") == "mem-path":
            return arg.get("value"), None
    return None, None


def _shm_size(el):
    size = el.find("size")
    if size is None:
        return None
    return f"{size.text} {size.get('unit', 'B')}"


def parse_details(dom):
    xml = dom.XMLDesc(0)
    root = ET.fromstring(xml)
    os_el = root.find("./os")
    otype = os_el.find("type") if os_el is not None else None
    cpu = root.find("./cpu")
    topo = cpu.find("topology") if cpu is not None else None
    topology = ""
    if topo is not None:
        topology = (f"{topo.get('sockets', '1')} socket(s), "
                    f"{topo.get('cores', '1')} core(s), "
                    f"{topo.get('threads', '1')} thread(s)")
    firmware = os_el.get("firmware", "") if os_el is not None else ""
    loader = os_el.find("loader") if os_el is not None else None
    if loader is not None and loader.text:
        firmware = (firmware or "uefi") + (
            " (secure boot)" if loader.get("secure") == "yes" else "")
    det = DomainDetails(
        name=dom.name(), uuid=dom.UUIDString(),
        os_type=otype.text if otype is not None else "",
        machine=otype.get("machine", "") if otype is not None else "",
        firmware=firmware or "bios",
        vcpus=int(root.findtext("./vcpu", "0")),
        cpu_mode=cpu.get("mode", "") if cpu is not None else "",
        topology=topology,
        memory_kib=int(root.findtext("./memory", "0")),
    )
    for disk in root.findall("./devices/disk"):
        src = disk.find("source")
        tgt = disk.find("target")
        drv = disk.find("driver")
        path = None
        if src is not None:
            path = src.get("file") or src.get("dev") or src.get("name")
        det.disks.append({
            "device": disk.get("device", "disk"),
            "target": tgt.get("dev") if tgt is not None else "?",
            "bus": tgt.get("bus") if tgt is not None else "?",
            "format": drv.get("type") if drv is not None else "",
            "source": path,
        })
    for nic in root.findall("./devices/interface"):
        mac = nic.find("mac")
        src = nic.find("source")
        model = nic.find("model")
        tgt = nic.find("target")
        det.nics.append({
            "type": nic.get("type"),
            "mac": mac.get("address") if mac is not None else "",
            "source": (src.get("network") or src.get("bridge") or src.get("dev"))
            if src is not None else "",
            "model": model.get("type") if model is not None else "",
            "dev": tgt.get("dev") if tgt is not None else None,
        })
    for hd in root.findall("./devices/hostdev"):
        t = hd.get("type")
        src = hd.find("source")
        if t == "pci" and src is not None:
            a = src.find("address")
            if a is not None:
                det.hostdevs.append(
                    "PCI " + _pci_addr(a) + _pci_name(a))
        elif t == "usb" and src is not None:
            v = src.find("vendor")
            p = src.find("product")
            if v is not None and p is not None:
                det.hostdevs.append(f"USB {v.get('id')}:{p.get('id')}")
    for gfx in root.findall("./devices/graphics"):
        det.graphics.append({
            "type": gfx.get("type"),
            "port": gfx.get("port"),
            "listen": gfx.get("listen") or "",
        })
    for ch in root.findall("./devices/channel"):
        tgt = ch.find("target")
        if tgt is not None and tgt.get("name") == "org.qemu.guest_agent.0":
            det.has_agent_channel = True
    det.shm_path, det.shm_size = lg_shm_path(xml)
    return det


def _pci_addr(a):
    def h(k, w):
        return int(a.get(k, "0"), 16).__format__(f"0{w}x")
    return f"{h('domain', 4)}:{h('bus', 2)}:{h('slot', 2)}.{h('function', 1)}"


def _pci_name(a):
    """Best-effort device name from sysfs + pci.ids."""
    addr = _pci_addr(a)
    base = f"/sys/bus/pci/devices/{addr}"
    try:
        vendor = open(f"{base}/vendor").read().strip()[2:]
        device = open(f"{base}/device").read().strip()[2:]
    except OSError:
        return ""
    name = _pci_ids_lookup(vendor, device)
    return f" — {name}" if name else f" [{vendor}:{device}]"


_PCI_IDS = None


def _pci_ids_lookup(vendor, device):
    global _PCI_IDS
    if _PCI_IDS is None:
        _PCI_IDS = {}
        for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    cur = None
                    for line in f:
                        if not line.strip() or line.startswith("#"):
                            continue
                        if line.startswith("C "):
                            break
                        if not line.startswith("\t"):
                            cur = line[:4]
                            _PCI_IDS[cur] = (line[6:].strip(), {})
                        elif not line.startswith("\t\t") and cur:
                            _PCI_IDS[cur][1][line[1:5]] = line[7:].strip()
                break
            except OSError:
                continue
    v = _PCI_IDS.get(vendor)
    if not v:
        return None
    d = v[1].get(device)
    vname = v[0].split("[")[-1].rstrip("]") if "[" in v[0] else v[0]
    if d:
        d = d.split("[")[-1].rstrip("]") if "[" in d else d
        return f"{vname} {d}"
    return vname


def cdrom_drives(dom):
    root = ET.fromstring(dom.XMLDesc(0))
    out = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "cdrom":
            continue
        tgt = disk.find("target")
        src = disk.find("source")
        out.append(CdromDrive(
            target=tgt.get("dev"), bus=tgt.get("bus"),
            source=src.get("file") if src is not None else None))
    return out


def host_usb_devices():
    """Enumerate host USB devices from sysfs (hubs excluded)."""
    base = "/sys/bus/usb/devices"
    devs = []
    try:
        entries = os.listdir(base)
    except OSError:
        return devs

    def rd(path, name):
        try:
            with open(os.path.join(path, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    for e in sorted(entries):
        if ":" in e or e.startswith("usb"):
            continue
        p = os.path.join(base, e)
        if rd(p, "bDeviceClass") == "09":
            continue  # hub
        vid = rd(p, "idVendor")
        pid = rd(p, "idProduct")
        if not vid or not pid:
            continue
        name = " ".join(x for x in (rd(p, "manufacturer"), rd(p, "product")) if x)
        try:
            devs.append(UsbDevice(
                vendor=int(vid, 16), product=int(pid, 16),
                bus=int(rd(p, "busnum") or 0), dev=int(rd(p, "devnum") or 0),
                name=name or f"USB device {vid}:{pid}"))
        except ValueError:
            continue
    return devs


def attached_usb(dom):
    root = ET.fromstring(dom.XMLDesc(0))
    out = set()
    for hd in root.findall("./devices/hostdev[@type='usb']"):
        src = hd.find("source")
        v = src.find("vendor") if src is not None else None
        p = src.find("product") if src is not None else None
        if v is not None and p is not None:
            out.add((int(v.get("id"), 16), int(p.get("id"), 16)))
    return out


def usb_hostdev_xml(vendor, product):
    return (f"<hostdev mode='subsystem' type='usb' managed='yes'><source>"
            f"<vendor id='0x{vendor:04x}'/><product id='0x{product:04x}'/>"
            f"</source></hostdev>")


def cdrom_xml(drive, path):
    src = f"<source file='{_xml_escape(path)}'/>" if path else ""
    return (f"<disk type='file' device='cdrom'><driver name='qemu' type='raw'/>"
            f"{src}<target dev='{drive.target}' bus='{drive.bus}'/><readonly/>"
            f"</disk>")


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("'", "&apos;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------
# guest agent

class AgentError(Exception):
    pass


class GuestAgent:
    def __init__(self, dom):
        self.dom = dom

    def cmd(self, execute, arguments=None, timeout=5):
        req = {"execute": execute}
        if arguments is not None:
            req["arguments"] = arguments
        try:
            reply = libvirt_qemu.qemuAgentCommand(
                self.dom, json.dumps(req), timeout, 0)
        except libvirt.libvirtError as e:
            raise AgentError(str(e)) from e
        data = json.loads(reply)
        if "error" in data:
            raise AgentError(data["error"].get("desc", str(data["error"])))
        return data.get("return")

    def ping(self):
        try:
            self.cmd("guest-ping", timeout=2)
            return True
        except AgentError:
            return False

    def summary(self):
        """Collect a dictionary of guest information. Failures are tolerated
        per field."""
        out = {}

        def tryset(key, fn):
            try:
                out[key] = fn()
            except (AgentError, KeyError, TypeError, ValueError):
                pass

        tryset("osinfo", lambda: self.cmd("guest-get-osinfo"))
        tryset("hostname", lambda: self.cmd("guest-get-host-name")["host-name"])
        tryset("users", lambda: self.cmd("guest-get-users"))
        tryset("timezone", lambda: self.cmd("guest-get-timezone"))
        tryset("fsinfo", lambda: self.cmd("guest-get-fsinfo"))
        tryset("interfaces", lambda: self.cmd("guest-network-get-interfaces"))
        tryset("info", lambda: self.cmd("guest-info"))
        return out

    def exec(self, path, args=(), capture=True, timeout=30, stdin=None):
        """Run a program in the guest and wait for it. Returns
        (exitcode, stdout, stderr)."""
        req = {"path": path, "arg": list(args), "capture-output": capture}
        if stdin is not None:
            req["input-data"] = base64.b64encode(stdin).decode()
        pid = self.cmd("guest-exec", req)["pid"]
        deadline = time.monotonic() + timeout
        while True:
            st = self.cmd("guest-exec-status", {"pid": pid})
            if st.get("exited"):
                out = base64.b64decode(st.get("out-data", "")) if capture else b""
                err = base64.b64decode(st.get("err-data", "")) if capture else b""
                return st.get("exitcode"), out, err
            if time.monotonic() > deadline:
                raise AgentError(f"Command timed out after {timeout}s (pid {pid})")
            time.sleep(0.1)

    # Large guest-file-write payloads can stall the Windows agent for good
    # (a 1 MiB chunk timed out and wedged it); 48 KiB stays responsive.
    CHUNK = 48 * 1024

    def _close(self, handle):
        """Close a guest file handle, retrying: a handle left open keeps the
        file locked in the guest until the agent restarts."""
        for attempt in range(5):
            try:
                self.cmd("guest-file-close", {"handle": handle}, timeout=10)
                return
            except AgentError:
                time.sleep(1 + attempt)

    def upload(self, local_path, guest_path, progress=None, chunk=CHUNK):
        size = os.path.getsize(local_path)
        handle = self.cmd("guest-file-open", {"path": guest_path, "mode": "wb"})
        sent = 0
        try:
            with open(local_path, "rb") as f:
                while True:
                    data = f.read(chunk)
                    if not data:
                        break
                    self.cmd("guest-file-write", {
                        "handle": handle,
                        "buf-b64": base64.b64encode(data).decode()}, timeout=30)
                    sent += len(data)
                    if progress:
                        progress(sent, size)
        finally:
            self._close(handle)
        return sent

    def download(self, guest_path, local_path, chunk=CHUNK):
        handle = self.cmd("guest-file-open", {"path": guest_path, "mode": "rb"})
        total = 0
        try:
            with open(local_path, "wb") as f:
                while True:
                    r = self.cmd("guest-file-read",
                                 {"handle": handle, "count": chunk}, timeout=30)
                    data = base64.b64decode(r.get("buf-b64", ""))
                    f.write(data)
                    total += len(data)
                    if r.get("eof") or not data:
                        break
        finally:
            self._close(handle)
        return total

    def sync_time(self):
        # without a time argument the agent reads the RTC; pass host time
        self.cmd("guest-set-time", {"time": time.time_ns()})


# --------------------------------------------------------------------------
# stats

class StatsTracker:
    """Turns cumulative libvirt counters into rates."""

    def __init__(self):
        self.prev = {}
        self.prev_time = None

    def reset(self):
        self.prev = {}
        self.prev_time = None

    def sample(self, conn, dom):
        flags = (libvirt.VIR_DOMAIN_STATS_CPU_TOTAL |
                 libvirt.VIR_DOMAIN_STATS_BALLOON |
                 libvirt.VIR_DOMAIN_STATS_INTERFACE |
                 libvirt.VIR_DOMAIN_STATS_BLOCK |
                 libvirt.VIR_DOMAIN_STATS_VCPU)
        stats = conn.domainListGetStats(
            [dom], flags, libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_NOWAIT)[0][1]
        now = time.monotonic()
        cur = {
            "cpu": stats.get("cpu.time", 0),
            "rd": sum(stats.get(f"block.{i}.rd.bytes", 0)
                      for i in range(stats.get("block.count", 0))),
            "wr": sum(stats.get(f"block.{i}.wr.bytes", 0)
                      for i in range(stats.get("block.count", 0))),
            "rx": sum(stats.get(f"net.{i}.rx.bytes", 0)
                      for i in range(stats.get("net.count", 0))),
            "tx": sum(stats.get(f"net.{i}.tx.bytes", 0)
                      for i in range(stats.get("net.count", 0))),
        }
        vcpus = stats.get("vcpu.current", 0) or 1
        out = {
            "vcpus": vcpus,
            "mem_total": stats.get("balloon.maximum", 0) * 1024,
            "mem_current": stats.get("balloon.current", 0) * 1024,
            "mem_rss": stats.get("balloon.rss", 0) * 1024,
            "mem_unused": stats.get("balloon.unused", 0) * 1024
            if "balloon.unused" in stats else None,
            "mem_available": stats.get("balloon.available", 0) * 1024
            if "balloon.available" in stats else None,
        }
        if self.prev_time is not None:
            dt = now - self.prev_time
            if dt > 0:
                out["cpu_pct"] = max(0.0, min(100.0, (cur["cpu"] - self.prev["cpu"])
                                              / (dt * 1e9 * vcpus) * 100))
                for k in ("rd", "wr", "rx", "tx"):
                    out[k + "_rate"] = max(0, (cur[k] - self.prev[k]) / dt)
        self.prev = cur
        self.prev_time = now
        return out


# --------------------------------------------------------------------------
# connection manager

class LibvirtManager(QObject):
    """Owns the libvirt connection. Lifecycle events arrive on a background
    event-loop thread and are re-emitted as Qt signals."""

    domainEvent = Signal(str, int, int)        # name, event, detail
    domainsChanged = Signal()
    connectionChanged = Signal(bool, str)      # connected, message
    taskFinished = Signal(str, bool, str)      # label, ok, message
    statsReady = Signal(str, dict)             # domain, sample
    _closed = Signal()                         # from the libvirt event thread

    _event_loop_started = False

    def __init__(self, uri, parent=None):
        super().__init__(parent)
        self.uri = uri
        self.conn = None
        self.pool = ThreadPoolExecutor(max_workers=4,
                                       thread_name_prefix="libvirt")
        self._cb_ids = []
        self._closed.connect(self._handle_closed)

    @classmethod
    def _start_event_loop(cls):
        if cls._event_loop_started:
            return
        libvirt.virEventRegisterDefaultImpl()

        def run():
            while True:
                libvirt.virEventRunDefaultImpl()

        threading.Thread(target=run, name="libvirt-events", daemon=True).start()
        cls._event_loop_started = True

    def connect(self):
        self._start_event_loop()
        try:
            self.conn = libvirt.open(self.uri)
        except libvirt.libvirtError as e:
            self.conn = None
            self.connectionChanged.emit(False, str(e))
            return False
        try:
            self._cb_ids.append(self.conn.domainEventRegisterAny(
                None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                self._on_lifecycle, None))
            self.conn.registerCloseCallback(self._on_close, None)
        except libvirt.libvirtError:
            pass
        self.connectionChanged.emit(True, self.conn.getURI())
        return True

    def close(self):
        if not self.conn:
            return
        for cid in self._cb_ids:
            try:
                self.conn.domainEventDeregisterAny(cid)
            except libvirt.libvirtError:
                pass
        self._cb_ids = []
        try:
            self.conn.unregisterCloseCallback()
            self.conn.close()
        except libvirt.libvirtError:
            pass
        self.conn = None

    def _on_lifecycle(self, conn, dom, event, detail, opaque):
        self.domainEvent.emit(dom.name(), event, detail)
        self.domainsChanged.emit()

    def _on_close(self, conn, reason, opaque):
        # event-loop thread: hand over to the GUI thread (queued signal)
        self._closed.emit()

    def _handle_closed(self):
        conn, self.conn = self.conn, None
        self._cb_ids = []
        if conn is not None:
            try:
                conn.close()
            except libvirt.libvirtError:
                pass
        self.connectionChanged.emit(False, "Connection to libvirt closed")

    # queries ------------------------------------------------------------------
    def domains(self):
        conn = self.conn
        if not conn:
            return []
        out = []
        try:
            doms = conn.listAllDomains(0)
        except libvirt.libvirtError:
            return []
        for d in doms:
            try:
                state, _ = d.state()
                info = d.info()
                xml = d.XMLDesc(0)
                shm, _ = lg_shm_path(xml)
                out.append(DomainSummary(
                    name=d.name(), uuid=d.UUIDString(), state=state,
                    vcpus=info[3], memory_kib=info[1],
                    persistent=bool(d.isPersistent()),
                    autostart=bool(d.autostart()) if d.isPersistent() else False,
                    has_lg=shm is not None))
            except libvirt.libvirtError:
                continue
        out.sort(key=lambda s: (not s.has_lg, s.name.lower()))
        return out

    def lookup(self, name):
        conn = self.conn
        if not conn:
            return None
        try:
            return conn.lookupByName(name)
        except libvirt.libvirtError:
            return None

    def sample_stats(self, name, tracker):
        """Sample stats on the worker pool; emits statsReady(name, sample)."""
        conn = self.conn
        dom = self.lookup(name)
        if conn is None or dom is None:
            return

        def work():
            try:
                if not dom.isActive():
                    tracker.reset()
                    return
                self.statsReady.emit(name, tracker.sample(conn, dom))
            except libvirt.libvirtError:
                pass
        self.pool.submit(work)

    # async tasks ----------------------------------------------------------------
    def run_task(self, label, fn, *args, on_done=None):
        """Run fn(*args) on the worker pool; emits taskFinished. on_done is
        invoked (in the worker thread) with the result if successful."""
        def work():
            try:
                result = fn(*args)
                if on_done:
                    on_done(result)
                self.taskFinished.emit(label, True, "")
            except Exception as e:  # noqa: BLE001 - surface to the UI
                self.taskFinished.emit(label, False, str(e))
        self.pool.submit(work)

    # lifecycle
    def start(self, name):
        self.run_task(f"Start {name}", lambda: self.lookup(name).create())

    def shutdown(self, name, agent=False):
        flags = (libvirt.VIR_DOMAIN_SHUTDOWN_GUEST_AGENT if agent
                 else libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)
        self.run_task(f"Shut down {name}",
                      lambda: self.lookup(name).shutdownFlags(flags))

    def reboot(self, name):
        self.run_task(f"Reboot {name}", lambda: self.lookup(name).reboot(0))

    def force_off(self, name):
        self.run_task(f"Force off {name}", lambda: self.lookup(name).destroy())

    def reset(self, name):
        self.run_task(f"Reset {name}", lambda: self.lookup(name).reset(0))

    def pause(self, name):
        self.run_task(f"Pause {name}", lambda: self.lookup(name).suspend())

    def resume(self, name):
        self.run_task(f"Resume {name}", lambda: self.lookup(name).resume())

    def set_autostart(self, name, value):
        self.run_task(f"Autostart {name}",
                      lambda: self.lookup(name).setAutostart(1 if value else 0))

    # snapshots
    def snapshots(self, name):
        dom = self.lookup(name)
        if not dom:
            return []
        out = []
        for s in dom.listAllSnapshots(0):
            root = ET.fromstring(s.getXMLDesc(0))
            parent = root.findtext("./parent/name")
            out.append(Snapshot(
                name=s.getName(),
                created=int(root.findtext("creationTime", "0")),
                state=root.findtext("state", ""),
                description=root.findtext("description", "") or "",
                current=bool(s.isCurrent()),
                parent=parent))
        out.sort(key=lambda s: s.created)
        return out

    def create_snapshot(self, name, snap_name, description, disk_only):
        desc = f"<description>{_xml_escape(description)}</description>" \
            if description else ""
        xml = f"<domainsnapshot><name>{_xml_escape(snap_name)}</name>{desc}" \
              f"</domainsnapshot>"
        flags = 0
        if disk_only:
            flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
            flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
        self.run_task(f"Create snapshot '{snap_name}'",
                      lambda: self.lookup(name).snapshotCreateXML(xml, flags))

    def revert_snapshot(self, name, snap_name):
        def work():
            dom = self.lookup(name)
            dom.revertToSnapshot(dom.snapshotLookupByName(snap_name, 0), 0)
        self.run_task(f"Revert to '{snap_name}'", work)

    def delete_snapshot(self, name, snap_name):
        def work():
            self.lookup(name).snapshotLookupByName(snap_name, 0).delete(0)
        self.run_task(f"Delete snapshot '{snap_name}'", work)

    # devices
    def _live_flags(self, dom):
        flags = libvirt.VIR_DOMAIN_AFFECT_LIVE if dom.isActive() else \
            libvirt.VIR_DOMAIN_AFFECT_CONFIG
        return flags

    def attach_usb(self, name, dev):
        def work():
            dom = self.lookup(name)
            dom.attachDeviceFlags(usb_hostdev_xml(dev.vendor, dev.product),
                                  self._live_flags(dom))
        self.run_task(f"Attach {dev.name}", work)

    def detach_usb(self, name, dev):
        def work():
            dom = self.lookup(name)
            dom.detachDeviceFlags(usb_hostdev_xml(dev.vendor, dev.product),
                                  self._live_flags(dom))
        self.run_task(f"Detach {dev.name}", work)

    def change_media(self, name, drive, path):
        def work():
            dom = self.lookup(name)
            flags = self._live_flags(dom)
            if dom.isPersistent() and dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_CONFIG
            dom.updateDeviceFlags(cdrom_xml(drive, path), flags)
        label = f"Insert {os.path.basename(path)}" if path else \
            f"Eject {drive.target}"
        self.run_task(label, work)
