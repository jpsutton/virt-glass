"""Stat tiles with sparklines for the performance panel.

Colours follow the validated dark-mode reference palette: text wears text
tokens, the first series is blue and the second orange; two-series tiles carry
a small legend so identity never relies on colour alone.
"""

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

SURFACE = QColor("#1a1a19")
TEXT_PRIMARY = QColor("#f0efec")
TEXT_SECONDARY = QColor("#c3c2b7")
TEXT_MUTED = QColor("#8d8c84")
GRID = QColor("#2e2e2c")
SERIES = [QColor("#3987e5"), QColor("#d95926")]

HISTORY = 60  # seconds


def fmt_bytes(n, rate=False):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    s = f"{v:.0f} {units[i]}" if v >= 100 or i == 0 else f"{v:.1f} {units[i]}"
    return s + ("/s" if rate else "")


class StatTile(QWidget):
    """Label, headline value, optional sub-line and a sparkline of up to two
    series sharing one axis."""

    def __init__(self, title, series_names=(None,), fixed_max=None,
                 formatter=str, parent=None):
        super().__init__(parent)
        self.title = title
        self.series_names = list(series_names)
        self.fixed_max = fixed_max
        self.formatter = formatter
        self.data = [deque(maxlen=HISTORY) for _ in self.series_names]
        self.value_text = "–"
        self.sub_text = ""
        self._hover = None
        self.setMouseTracking(True)
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def push(self, values, value_text, sub_text=""):
        for d, v in zip(self.data, values):
            d.append(v)
        self.value_text = value_text
        self.sub_text = sub_text
        self.update()

    def clear(self):
        for d in self.data:
            d.clear()
        self.value_text = "–"
        self.sub_text = ""
        self.update()

    def _plot_rect(self):
        return QRectF(12, 62, self.width() - 24, self.height() - 74)

    def _scale_max(self):
        if self.fixed_max:
            return self.fixed_max
        m = max((max(d) for d in self.data if d), default=0)
        return m * 1.15 if m > 0 else 1

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(SURFACE)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        base = QFont(self.font())
        f = QFont(base)
        f.setPointSizeF(base.pointSizeF() * 0.9)
        p.setFont(f)
        p.setPen(TEXT_SECONDARY)
        p.drawText(QRectF(12, 8, self.width() - 24, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, self.title)

        # legend for multi-series tiles: colored key + text-coloured label
        if len(self.series_names) > 1:
            fm = p.fontMetrics()
            x = self.width() - 12
            for i in reversed(range(len(self.series_names))):
                name = self.series_names[i]
                w = fm.horizontalAdvance(name)
                x -= w
                p.setPen(TEXT_SECONDARY)
                p.drawText(QRectF(x, 8, w, 18), Qt.AlignVCenter, name)
                x -= 12
                p.setPen(Qt.NoPen)
                p.setBrush(SERIES[i])
                p.drawRoundedRect(QRectF(x, 13, 8, 8), 2, 2)
                x -= 10

        f = QFont(base)
        f.setPointSizeF(base.pointSizeF() * 1.6)
        f.setBold(True)
        p.setFont(f)
        p.setPen(TEXT_PRIMARY)
        p.drawText(QRectF(12, 26, self.width() - 24, 26),
                   Qt.AlignLeft | Qt.AlignVCenter, self.value_text)
        if self.sub_text:
            f = QFont(base)
            f.setPointSizeF(base.pointSizeF() * 0.85)
            p.setFont(f)
            p.setPen(TEXT_MUTED)
            p.drawText(QRectF(12, 26, self.width() - 24, 26),
                       Qt.AlignRight | Qt.AlignVCenter, self.sub_text)

        r = self._plot_rect()
        p.setPen(QPen(GRID, 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        p.drawLine(QPointF(r.left(), r.top()), QPointF(r.right(), r.top()))

        top = self._scale_max()
        step = r.width() / (HISTORY - 1)
        for i, d in enumerate(self.data):
            if len(d) < 2:
                continue
            path = QPainterPath()
            x0 = r.right() - (len(d) - 1) * step
            for j, v in enumerate(d):
                pt = QPointF(x0 + j * step, r.bottom() - min(v / top, 1) * r.height())
                path.lineTo(pt) if j else path.moveTo(pt)
            if len(self.data) == 1:
                fill = QPainterPath(path)
                fill.lineTo(r.right(), r.bottom())
                fill.lineTo(x0, r.bottom())
                fill.closeSubpath()
                c = QColor(SERIES[0])
                c.setAlpha(40)
                p.fillPath(fill, c)
            pen = QPen(SERIES[i], 2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # hover crosshair
        if self._hover is not None:
            idx, x = self._hover
            p.setPen(QPen(TEXT_MUTED, 1, Qt.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            for i, d in enumerate(self.data):
                k = idx - (HISTORY - len(d))
                if 0 <= k < len(d):
                    y = r.bottom() - min(d[k] / top, 1) * r.height()
                    p.setPen(QPen(SURFACE, 2))
                    p.setBrush(SERIES[i])
                    p.drawEllipse(QPointF(x, y), 4.5, 4.5)
        p.end()

    def mouseMoveEvent(self, e):
        r = self._plot_rect()
        if not r.adjusted(-6, -6, 6, 6).contains(e.position()):
            self.leaveEvent(None)
            return
        step = r.width() / (HISTORY - 1)
        idx = round((e.position().x() - r.left()) / step)
        idx = max(0, min(HISTORY - 1, idx))
        x = r.left() + idx * step
        self._hover = (idx, x)
        lines = [f"{HISTORY - 1 - idx}s ago"]
        for i, d in enumerate(self.data):
            k = idx - (HISTORY - len(d))
            if 0 <= k < len(d):
                label = self.series_names[i] or self.title
                lines.append(f"{label}: {self.formatter(d[k])}")
        QToolTip.showText(e.globalPosition().toPoint(), "\n".join(lines), self)
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        QToolTip.hideText()
        self.update()
