# Virt Glass

A Looking Glass client with built-in libvirt VM management, written in
Python/Qt on top of the official Looking Glass LGMP transport.

The window has a machines sidebar on the left, the live guest display in the
middle and a tabbed details panel (Overview, Performance, Guest, Devices,
Snapshots) on the right.

## What it does

**Display (Looking Glass)**
- Live guest video from IVSHMEM (`/dev/shm/…`) or KVMFR, rendered with
  OpenGL. Handles BGRA/RGBA, 10-bit, packed 24-bit and HDR (scRGB / PQ,
  tone-mapped to SDR) frames and display rotation.
- Scaling: fit, stretch, integer and native (1:1), with optional smooth
  filtering; fullscreen (F11 or Pause+F; touch the top edge for a control
  bar). By default the guest resolution follows the window size, like the
  stock client (Guest → Auto-resize Guest to Window).
- The guest's cursor shape becomes the native host cursor (zero-lag pointer,
  no capture needed on Wayland).
- Absolute mouse, wheel, side buttons and full keyboard over the KVMFR input
  channel. Keyboard capture toggle (tap **Pause**), "Send keys" menu
  (Ctrl+Alt+Del, Win key, Alt+Tab, …).
- **Type clipboard text**: types the host clipboard as keystrokes, which works
  on the Windows login screen where clipboard sync can't.
- Two-way clipboard sync (text and images) over the Looking Glass channel.
- Guest audio (and optionally the microphone, off by default) over SPICE,
  like the stock client.
- Frame scheduling at your display's refresh rate, a wake nudge for sleeping
  guest displays, auto-reconnect when the host app or VM restarts.
- Screenshots at native guest resolution (`~/Pictures`).
- Stats overlay (FPS/UPS, format) — Pause+I.

**VM control (libvirt)**
- Machines sidebar with live state; start, graceful shutdown (ACPI or guest
  agent), reboot, pause/resume, hard reset, force off, autostart.
- Overview: hardware summary (vCPU topology, firmware, disks, NICs, PCI
  passthrough devices with names, graphics, shared memory), Looking Glass host
  info, and guest-agent info (OS, hostname, logged-in users, IPs, disk usage).
- Performance: CPU, host memory, disk and network throughput and LG frame rate
  sparklines (hover for values).
- Devices: hot-plug host USB devices into the VM; insert/eject ISOs.
- Snapshots: list, take, revert, delete.
- Guest tools (QEMU guest agent): run PowerShell/cmd commands and see the
  output, send files to the guest (or drag and drop onto the display), fetch
  files from the guest, sync the guest clock.

## Install

Requirements (Arch package names): `libvirt-python`, `python-gobject`,
`spice-gtk` (for audio), `gst-plugins-good`, a C compiler and `make`.

```sh
./install.sh      # venv + PySide6/PyOpenGL, builds the native shim, adds a menu entry
./virt-glass      # or launch "Virt Glass" from the app menu
```

Your user needs access to `qemu:///system` (the `libvirt` group) and
read/write access to the shared memory file, exactly as for the stock client.

The shared memory path is read from the domain XML (`<shmem name=…>` →
`/dev/shm/<name>`, or a `mem-path=` in `qemu:commandline`). Override it per
VM from the sidebar's context menu.

## Keyboard shortcuts

While the guest display has focus every key goes to the guest. **Pause** is
the host key (same as your stock-client `escapeKey`). Combos work either held
(Pause+F) or as a prefix (tap Pause, then F within 0.6 s), since laptop
keyboards report Pause as an instant press+release:

| Keys     | Action                              |
|----------|-------------------------------------|
| Pause    | Capture / release the keyboard      |
| Pause+F  | Fullscreen                          |
| Pause+D  | Show / hide side panels             |
| Pause+S  | Screenshot                          |
| Pause+V  | Type host clipboard into the guest  |
| Pause+R  | Resize guest to the window          |
| Pause+I  | Stats overlay                       |
| Pause+Q  | Quit                                |

## How it works

```
virtglass/            Python/Qt application
  app.py              main window, menus, toolbar, status bar
  display.py          OpenGL guest view + input + cursor
  session.py          eventfd → Qt signal bridge for the shim
  lgclient.py         ctypes binding to libvirtglass
  clipboard.py        host ↔ guest clipboard
  audio.py            SPICE audio via spice-glib
  vm.py               libvirt + guest agent
  panels.py charts.py side panels and sparklines
native/
  vglass.c            C shim: session/frame/cursor/clipboard threads,
                      triple-buffered frames, frame scheduling
  third_party/lg/     vendored Looking Glass sources (see VERSION)
```

The shim compiles the Looking Glass client's own LGMP transport (video,
cursor, input, clipboard) into `libvglass.so`, so the wire protocol is exactly
the upstream one. The vendored tree is pinned to the commit in
`native/third_party/lg/VERSION` and **must match the Looking Glass host
version installed in the guest**. To update it, copy the same directories from
a newer Looking Glass checkout and run `make -C native`.

`tools/snapshot.py` launches the window, runs scripted steps and saves
grabs; handy for testing in a headless compositor
(`kwin_wayland --virtual --socket test`).

## Limitations / ideas

- No relative-mouse "capture" mode: Wayland needs pointer-lock for that, which
  Qt doesn't expose. Absolute input works for the desktop and most apps; games
  that require raw relative mouse input will want the stock client.
- No DMA-BUF/KVMFR zero-copy upload yet; frames are copied once (fine up to
  4K60 on a modern CPU).
- Clipboard file transfer isn't implemented; use the Guest tab or drag and
  drop (guest agent) instead.
- USB redirection over SPICE isn't used; USB passthrough uses libvirt hostdev
  hot-plug (no extra permissions needed).

## License

GPL-2.0-or-later (the vendored Looking Glass code is GPL-2.0-or-later).
