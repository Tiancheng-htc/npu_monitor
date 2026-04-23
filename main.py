import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from parser import ChipStatus, parse_npu_smi
from ssh_client import parse_ssh_config, run_ssh, run_ssh_pipe


COLOR_IDLE = "#16a34a"
COLOR_BUSY = "#dc2626"
COLOR_PENDING = "#ca8a04"
COLOR_OFFLINE = "#475569"
COLOR_HELD = "#a855f7"
COLOR_CARD = "#1e293b"
COLOR_BG = "#0f172a"
COLOR_TEXT = "#e2e8f0"
COLOR_TEXT_DIM = "#94a3b8"

HOLD_SCRIPT_NAME = "hold_npu.py"
HOLD_REMOTE_PATH = "/tmp/npu_monitor_hold.py"
HOLD_REMOTE_LOG = "/tmp/npu_monitor_hold.log"
HOLD_PROC_MARKER = "NPU_HOLD"
HOLD_STARTUP_TIMEOUT_S = 180
MIN_CHIPS_FOR_AUTO_HOLD = 16


# ---------------------------------------------------------------------------
# SSH workers (query / hold / release)
# ---------------------------------------------------------------------------


class WorkerSignals(QObject):
    finished = Signal(str, object)


class HostRunnable(QRunnable):
    def __init__(self, host: str):
        super().__init__()
        self.host = host
        self.signals = WorkerSignals()

    def run(self):
        rc, stdout, stderr = run_ssh(self.host, "npu-smi info", timeout=45.0)
        if rc != 0:
            msg = (stderr or stdout or f"exit {rc}").strip()
            if len(msg) > 240:
                msg = msg[:240] + "..."
            self.signals.finished.emit(self.host, {"error": msg})
            return
        try:
            chips = parse_npu_smi(stdout)
        except Exception as e:
            self.signals.finished.emit(self.host, {"error": f"parse error: {e}"})
            return
        if not chips:
            self.signals.finished.emit(
                self.host, {"error": "npu-smi returned no chips"}
            )
            return
        self.signals.finished.emit(self.host, {"chips": chips})


class HoldRunnable(QRunnable):
    """Ship hold_npu.py to the remote and launch it in the background."""

    def __init__(self, host: str, script_bytes: bytes, percent: float):
        super().__init__()
        self.host = host
        self.script_bytes = script_bytes
        self.percent = percent
        self.signals = WorkerSignals()

    def run(self):
        remote_cmd = (
            "set -e; "
            f"DEST={HOLD_REMOTE_PATH}; "
            f"LOG={HOLD_REMOTE_LOG}; "
            "cat > $DEST; "
            "chmod +x $DEST; "
            f"nohup python3 $DEST --percent {self.percent:g} "
            ">$LOG 2>&1 </dev/null & "
            "disown 2>/dev/null || true; "
            'echo "HOLD_OK pid=$!"'
        )
        rc, stdout, stderr = run_ssh_pipe(
            self.host,
            remote_cmd,
            stdin_bytes=self.script_bytes,
            timeout=90.0,
        )
        if rc != 0 or "HOLD_OK" not in stdout:
            msg = (stderr or stdout or f"exit {rc}").strip()
            if len(msg) > 240:
                msg = msg[:240] + "..."
            self.signals.finished.emit(self.host, {"error": msg})
        else:
            pid = ""
            for tok in stdout.split():
                if tok.startswith("pid="):
                    pid = tok
                    break
            self.signals.finished.emit(self.host, {"ok": True, "pid": pid})


class ReleaseRunnable(QRunnable):
    def __init__(self, host: str):
        super().__init__()
        self.host = host
        self.signals = WorkerSignals()

    def run(self):
        remote_cmd = (
            f"pkill -9 -f {HOLD_PROC_MARKER} 2>/dev/null; "
            "pkill -9 -f npu_monitor_hold 2>/dev/null; "
            f"rm -f {HOLD_REMOTE_PATH} {HOLD_REMOTE_LOG} 2>/dev/null; "
            "echo RELEASE_OK"
        )
        rc, stdout, stderr = run_ssh(self.host, remote_cmd, timeout=30.0)
        if "RELEASE_OK" in stdout:
            self.signals.finished.emit(self.host, {"ok": True})
        else:
            msg = (stderr or stdout or f"exit {rc}").strip()
            self.signals.finished.emit(self.host, {"error": msg})


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class NpuCell(QLabel):
    SIZE = 54

    def __init__(self, chip: ChipStatus):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setTextFormat(Qt.TextFormat.RichText)

        label = chip.phy_id if chip.phy_id >= 0 else chip.chip_id
        self.setText(f"<b>{label}</b>")

        idle = chip.is_idle
        held_by_us = any(p.name.startswith(HOLD_PROC_MARKER) for p in chip.processes)
        if held_by_us:
            color = COLOR_HELD
        elif idle:
            color = COLOR_IDLE
        else:
            color = COLOR_BUSY
        self.setStyleSheet(
            f"""
            QLabel {{
                background-color: {color};
                color: white;
                border-radius: 8px;
                font-size: 17px;
                font-weight: 700;
            }}
            """
        )

        hbm_pct = 100 * chip.hbm_used / chip.hbm_total if chip.hbm_total else 0
        if held_by_us:
            status = "HELD (by NPU Monitor)"
        elif idle:
            status = "IDLE"
        else:
            status = "BUSY"
        lines: List[str] = [
            f"NPU {chip.npu_id} / Chip {chip.chip_id}  (Phy-ID {chip.phy_id})",
            f"Status : {status}",
            f"Health : {chip.health}",
            f"AICore : {chip.aicore_pct}%",
            f"HBM    : {chip.hbm_used} / {chip.hbm_total} MB  ({hbm_pct:.1f}%)",
            f"Temp   : {chip.temp_c}°C    Power: {chip.power_w}W",
        ]
        if chip.processes:
            lines.append("")
            lines.append("Processes:")
            for p in chip.processes:
                lines.append(f"  - {p.name}  pid={p.pid}  {p.memory_mb} MB")
        self.setToolTip("\n".join(lines))


class HostCard(QFrame):
    release_requested = Signal(str)
    retry_hold_requested = Signal(str)

    def __init__(self, alias: str):
        super().__init__()
        self.alias = alias
        self.in_flight = False
        self.idle_count = 0
        self.total_count = 0
        self.hold_state = "idle"  # idle | holding | held | failed

        self.setObjectName("HostCard")
        self.setStyleSheet(
            f"""
            #HostCard {{
                background-color: {COLOR_CARD};
                border-radius: 12px;
                border: 1px solid #334155;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {COLOR_OFFLINE}; font-size: 16px;")

        self.title = QLabel(f"<b>{alias}</b>")
        self.title.setStyleSheet(f"font-size: 15px; color: {COLOR_TEXT};")

        self.hold_badge = QLabel("")
        self.hold_badge.setStyleSheet(f"color: {COLOR_HELD}; font-size: 12px; font-weight: 600;")
        self.hold_badge.hide()

        self.release_btn = QPushButton("Release")
        self.release_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.release_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #b91c1c;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover { background-color: #991b1b; }
            """
        )
        self.release_btn.hide()
        self.release_btn.clicked.connect(lambda: self.release_requested.emit(self.alias))

        self.retry_btn = QPushButton("Retry hold")
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #334155;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #475569; }
            """
        )
        self.retry_btn.hide()
        self.retry_btn.clicked.connect(lambda: self.retry_hold_requested.emit(self.alias))

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")

        self.status_text = QLabel("Pending")
        self.status_text.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")

        header.addWidget(self.status_dot)
        header.addWidget(self.title)
        header.addSpacing(8)
        header.addWidget(self.hold_badge)
        header.addWidget(self.release_btn)
        header.addWidget(self.retry_btn)
        header.addStretch()
        header.addWidget(self.summary)
        header.addSpacing(14)
        header.addWidget(self.status_text)
        layout.addLayout(header)

        self.grid_holder = QWidget()
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setSpacing(6)
        self.grid.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.grid_holder)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(
            "color: #f87171; font-size: 12px; font-family: Consolas, monospace;"
        )
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

    def set_pending(self):
        self.status_dot.setStyleSheet(f"color: {COLOR_PENDING}; font-size: 16px;")
        self.status_text.setText("Querying...")

    def set_error(self, msg: str):
        self.clear_grid()
        self.status_dot.setStyleSheet(f"color: {COLOR_OFFLINE}; font-size: 16px;")
        self.status_text.setText("Offline")
        self.summary.setText("")
        self.error_label.setText(msg)
        self.error_label.show()
        self.idle_count = 0
        self.total_count = 0

    def set_chips(self, chips: List[ChipStatus]):
        self.clear_grid()
        self.error_label.hide()
        self.status_dot.setStyleSheet(f"color: {COLOR_IDLE}; font-size: 16px;")
        now = datetime.now().strftime("%H:%M:%S")
        self.status_text.setText(f"Online · {now}")

        idle = sum(1 for c in chips if c.is_idle)
        self.idle_count = idle
        self.total_count = len(chips)
        if idle == len(chips):
            clr = COLOR_IDLE
        elif idle == 0:
            clr = COLOR_BUSY
        else:
            clr = COLOR_PENDING
        self.summary.setText(
            f'<span style="color:{clr}; font-weight:600;">{idle}/{len(chips)}</span>'
            f'<span style="color:{COLOR_TEXT_DIM};"> idle</span>'
        )

        cols = 8
        for i, chip in enumerate(chips):
            cell = NpuCell(chip)
            r, c = divmod(i, cols)
            self.grid.addWidget(cell, r, c)
        self.grid.setColumnStretch(cols, 1)

    def set_hold_state(self, state: str, detail: str = ""):
        self.hold_state = state
        if state == "idle":
            self.hold_badge.hide()
            self.release_btn.hide()
            self.retry_btn.hide()
        elif state == "holding":
            self.hold_badge.setText("⏳ Holding…")
            self.hold_badge.setStyleSheet(
                f"color: {COLOR_PENDING}; font-size: 12px; font-weight: 600;"
            )
            self.hold_badge.show()
            self.release_btn.hide()
            self.retry_btn.hide()
        elif state == "held":
            text = "🔒 Held"
            if detail:
                text += f" · {detail}"
            self.hold_badge.setText(text)
            self.hold_badge.setStyleSheet(
                f"color: {COLOR_HELD}; font-size: 12px; font-weight: 600;"
            )
            self.hold_badge.show()
            self.release_btn.show()
            self.retry_btn.hide()
        elif state == "failed":
            text = "⚠ Hold failed"
            if detail:
                text += f": {detail[:60]}"
            self.hold_badge.setText(text)
            self.hold_badge.setStyleSheet(
                "color: #f87171; font-size: 12px; font-weight: 600;"
            )
            self.hold_badge.show()
            self.release_btn.hide()
            self.retry_btn.show()

    def clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget() if item else None
            if w:
                w.deleteLater()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NPU Monitor")
        self.resize(1200, 860)

        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(16)
        self.cards: Dict[str, HostCard] = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.running = False

        # Hold state.
        self.hold_in_progress: Dict[str, float] = {}  # alias -> started_at
        self.hold_failed: Dict[str, str] = {}  # alias -> last error
        self.hold_script_path = Path(__file__).resolve().parent / HOLD_SCRIPT_NAME
        self.hold_script_bytes: Optional[bytes] = None
        if self.hold_script_path.is_file():
            self.hold_script_bytes = self.hold_script_path.read_bytes()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # --- Toolbar row 1: config + polling controls ------------------
        tb1 = QHBoxLayout()
        tb1.setSpacing(8)
        tb1.addWidget(QLabel("SSH config:"))
        self.config_edit = QLineEdit(self.default_config_path())
        self.config_edit.editingFinished.connect(self.reload_hosts)
        tb1.addWidget(self.config_edit, 3)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.pick_config)
        tb1.addWidget(browse)

        tb1.addSpacing(14)
        tb1.addWidget(QLabel("Interval (s):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(2, 120)
        self.interval_spin.setValue(5)
        self.interval_spin.valueChanged.connect(self.on_interval_changed)
        tb1.addWidget(self.interval_spin)

        self.start_btn = QPushButton("▶ Start")
        self.start_btn.clicked.connect(self.toggle_running)
        tb1.addWidget(self.start_btn)
        self.refresh_btn = QPushButton("↻ Refresh")
        self.refresh_btn.clicked.connect(self.refresh_all)
        tb1.addWidget(self.refresh_btn)
        root.addLayout(tb1)

        # --- Toolbar row 2: auto-hold controls -------------------------
        tb2 = QHBoxLayout()
        tb2.setSpacing(8)
        self.auto_hold_chk = QCheckBox("Auto-hold when all 16 chips idle")
        self.auto_hold_chk.setToolTip(
            "When a host reports every chip idle, SSH in and launch a Python\n"
            "process that claims most HBM on every chip, so no one else grabs\n"
            "it. Only 16-chip hosts are targeted."
        )
        self.auto_hold_chk.toggled.connect(self.on_auto_hold_toggled)
        tb2.addWidget(self.auto_hold_chk)

        tb2.addSpacing(10)
        tb2.addWidget(QLabel("Hold %:"))
        self.hold_pct_spin = QDoubleSpinBox()
        self.hold_pct_spin.setRange(10.0, 99.0)
        self.hold_pct_spin.setDecimals(0)
        self.hold_pct_spin.setValue(90.0)
        self.hold_pct_spin.setSuffix(" %")
        tb2.addWidget(self.hold_pct_spin)

        self.release_all_btn = QPushButton("Release all")
        self.release_all_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #b91c1c;
            }
            QPushButton:hover { background-color: #991b1b; }
            """
        )
        self.release_all_btn.clicked.connect(self.release_all_held)
        tb2.addWidget(self.release_all_btn)

        tb2.addSpacing(20)
        self.only_idle_chk = QCheckBox("Only show hosts with idle chips")
        self.only_idle_chk.stateChanged.connect(self.apply_filter)
        tb2.addWidget(self.only_idle_chk)

        tb2.addStretch()
        tb2.addWidget(self._legend_item(COLOR_IDLE, "Idle"))
        tb2.addWidget(self._legend_item(COLOR_BUSY, "Busy"))
        tb2.addWidget(self._legend_item(COLOR_HELD, "Held"))
        tb2.addWidget(self._legend_item(COLOR_PENDING, "Querying"))
        tb2.addWidget(self._legend_item(COLOR_OFFLINE, "Offline"))
        root.addLayout(tb2)

        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        root.addWidget(self.status_label)

        # --- Scrollable card list --------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setSpacing(12)
        self.card_layout.setContentsMargins(2, 2, 2, 2)
        self.card_layout.addStretch()
        scroll.setWidget(self.card_container)
        root.addWidget(scroll, 1)

        self.apply_theme()

        if self.hold_script_bytes is None:
            self.auto_hold_chk.setEnabled(False)
            self.auto_hold_chk.setToolTip(
                f"Cannot find {HOLD_SCRIPT_NAME} next to main.py — auto-hold disabled."
            )

        self.reload_hosts()

    # -- init helpers -----------------------------------------------------

    @staticmethod
    def default_config_path() -> str:
        return str(Path.home() / ".ssh" / "config")

    def _legend_item(self, color: str, label: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 13px;")
        text = QLabel(label)
        text.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; margin-right: 6px;")
        h.addWidget(dot)
        h.addWidget(text)
        return w

    def apply_theme(self):
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background-color: {COLOR_BG};
                color: {COLOR_TEXT};
                font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 5px 8px;
                selection-background-color: #2563eb;
            }}
            QPushButton {{
                background-color: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 500;
            }}
            QPushButton:hover {{ background-color: #1d4ed8; }}
            QPushButton:pressed {{ background-color: #1e40af; }}
            QPushButton:disabled {{ background-color: #334155; color: #64748b; }}
            QCheckBox {{ color: {COLOR_TEXT_DIM}; font-size: 12px; }}
            QCheckBox::indicator {{
                width: 14px; height: 14px;
                border: 1px solid #475569; border-radius: 3px;
                background: {COLOR_CARD};
            }}
            QCheckBox::indicator:checked {{ background: #2563eb; border-color: #2563eb; }}
            QCheckBox::indicator:disabled {{ background: #1e293b; border-color: #334155; }}
            QScrollBar:vertical {{ background: {COLOR_BG}; width: 10px; }}
            QScrollBar::handle:vertical {{
                background: #334155; border-radius: 4px; min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{ background: #475569; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QToolTip {{
                background-color: #0b1220;
                color: {COLOR_TEXT};
                border: 1px solid #334155;
                padding: 8px;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }}
            """
        )

    # -- host loading -----------------------------------------------------

    def pick_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH config", str(Path.home()), "All files (*)"
        )
        if path:
            self.config_edit.setText(path)
            self.reload_hosts()

    def reload_hosts(self):
        path = self.config_edit.text().strip()
        if not path or not Path(path).is_file():
            self.status_label.setText(f"SSH config not found: {path}")
            return
        try:
            hosts = parse_ssh_config(path)
        except Exception as e:
            self.status_label.setText(f"Parse error: {e}")
            return

        for card in list(self.cards.values()):
            self.card_layout.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        self.hold_in_progress.clear()
        self.hold_failed.clear()

        for h in hosts:
            card = HostCard(h.alias)
            card.release_requested.connect(self.release_host)
            card.retry_hold_requested.connect(self.retry_hold)
            self.cards[h.alias] = card
            self.card_layout.insertWidget(self.card_layout.count() - 1, card)

        self.status_label.setText(f"{len(hosts)} host(s) loaded")

    # -- polling ----------------------------------------------------------

    def toggle_running(self):
        if self.running:
            self.timer.stop()
            self.running = False
            self.start_btn.setText("▶ Start")
        else:
            self.running = True
            self.start_btn.setText("⏸ Pause")
            self.refresh_all()
            self.timer.start(self.interval_spin.value() * 1000)

    def on_interval_changed(self, v: int):
        if self.running:
            self.timer.start(v * 1000)

    def refresh_all(self):
        if not self.cards:
            self.reload_hosts()
            if not self.cards:
                return
        started = 0
        for alias, card in self.cards.items():
            if card.in_flight:
                continue
            card.in_flight = True
            card.set_pending()
            r = HostRunnable(alias)
            r.signals.finished.connect(self.on_host_finished)
            self.thread_pool.start(r)
            started += 1
        self.status_label.setText(
            f"Last refresh: {datetime.now().strftime('%H:%M:%S')} · "
            f"{started} in flight · total hosts: {len(self.cards)}"
        )

    @Slot(str, object)
    def on_host_finished(self, alias: str, result: object):
        card = self.cards.get(alias)
        if not card:
            return
        card.in_flight = False

        if isinstance(result, dict) and "error" in result:
            card.set_error(result["error"])
            self.apply_filter()
            return
        if not (isinstance(result, dict) and "chips" in result):
            return

        chips: List[ChipStatus] = result["chips"]
        card.set_chips(chips)

        held_by_us = any(
            p.name.startswith(HOLD_PROC_MARKER)
            for c in chips
            for p in c.processes
        )
        all_idle = all(c.is_idle for c in chips) and len(chips) > 0

        # Update hold badge state based on what the remote shows us.
        if held_by_us:
            # Summarize how many MB we're holding and across how many chips.
            chips_with_hold = sum(
                1 for c in chips if any(p.name.startswith(HOLD_PROC_MARKER) for p in c.processes)
            )
            total_mb = sum(
                p.memory_mb
                for c in chips
                for p in c.processes
                if p.name.startswith(HOLD_PROC_MARKER)
            )
            card.set_hold_state("held", f"{chips_with_hold}/{len(chips)} chips · {total_mb} MB")
            # Once we can see the holder processes, this host is no longer
            # "starting up" — clear any in-progress or failed flags.
            self.hold_in_progress.pop(alias, None)
            self.hold_failed.pop(alias, None)
        else:
            if alias in self.hold_in_progress:
                if time.time() - self.hold_in_progress[alias] > HOLD_STARTUP_TIMEOUT_S:
                    # Hold was dispatched but the processes never showed up —
                    # treat as failed so the user knows.
                    self.hold_in_progress.pop(alias, None)
                    err = "holder processes never appeared (check /tmp/npu_monitor_hold.log)"
                    self.hold_failed[alias] = err
                    card.set_hold_state("failed", err)
                else:
                    card.set_hold_state("holding")
            elif alias in self.hold_failed:
                card.set_hold_state("failed", self.hold_failed[alias])
            else:
                card.set_hold_state("idle")

        # Maybe trigger auto-hold.
        if (
            self.auto_hold_chk.isChecked()
            and self.hold_script_bytes is not None
            and all_idle
            and len(chips) >= MIN_CHIPS_FOR_AUTO_HOLD
            and not held_by_us
            and alias not in self.hold_in_progress
            and alias not in self.hold_failed
        ):
            self.dispatch_hold(alias)

        self.apply_filter()

    # -- hold / release ---------------------------------------------------

    def on_auto_hold_toggled(self, checked: bool):
        if not checked:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Enable auto-hold?")
        box.setText(
            "Auto-hold will SSH into any host that reports all 16 chips idle "
            "and launch a Python process there that claims ~{pct:.0f}% of every "
            "chip's HBM.\n\n"
            "The hold persists until you click Release (per-host) or Release all.\n\n"
            "Enable it now?".format(pct=self.hold_pct_spin.value())
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.auto_hold_chk.blockSignals(True)
            self.auto_hold_chk.setChecked(False)
            self.auto_hold_chk.blockSignals(False)

    def dispatch_hold(self, alias: str):
        if self.hold_script_bytes is None:
            return
        card = self.cards.get(alias)
        if not card:
            return
        self.hold_in_progress[alias] = time.time()
        card.set_hold_state("holding")
        r = HoldRunnable(
            alias,
            self.hold_script_bytes,
            self.hold_pct_spin.value(),
        )
        r.signals.finished.connect(self.on_hold_finished)
        self.thread_pool.start(r)

    @Slot(str, object)
    def on_hold_finished(self, alias: str, result: object):
        card = self.cards.get(alias)
        if isinstance(result, dict) and result.get("error"):
            err = result["error"]
            self.hold_in_progress.pop(alias, None)
            self.hold_failed[alias] = err
            if card:
                card.set_hold_state("failed", err)
        # On success we keep hold_in_progress set — it clears once the
        # next poll actually sees NPU_HOLD_* processes on the host.

    @Slot(str)
    def retry_hold(self, alias: str):
        self.hold_failed.pop(alias, None)
        card = self.cards.get(alias)
        if card:
            card.set_hold_state("idle")
        # The next successful poll showing all_idle will re-dispatch.
        self.dispatch_hold(alias)

    @Slot(str)
    def release_host(self, alias: str):
        card = self.cards.get(alias)
        if not card:
            return
        card.set_hold_state("holding")  # visual: in-progress
        card.hold_badge.setText("⏳ Releasing…")
        self.hold_in_progress.pop(alias, None)
        self.hold_failed.pop(alias, None)
        r = ReleaseRunnable(alias)
        r.signals.finished.connect(self.on_release_finished)
        self.thread_pool.start(r)

    @Slot(str, object)
    def on_release_finished(self, alias: str, result: object):
        card = self.cards.get(alias)
        if not card:
            return
        if isinstance(result, dict) and result.get("ok"):
            card.set_hold_state("idle")
            self.status_label.setText(
                f"{datetime.now().strftime('%H:%M:%S')} · released {alias}"
            )
        else:
            err = result.get("error", "release failed") if isinstance(result, dict) else "?"
            card.set_hold_state("failed", err)

    def release_all_held(self):
        held = [a for a, c in self.cards.items() if c.hold_state in ("held", "holding", "failed")]
        if not held:
            self.status_label.setText("No held hosts to release.")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Release all?")
        box.setText(
            f"Release {len(held)} host(s): {', '.join(held)}?\n\n"
            "This also clears hold-failed flags so auto-hold can retry."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        for alias in held:
            self.release_host(alias)

    # -- filter -----------------------------------------------------------

    def apply_filter(self):
        only_idle = self.only_idle_chk.isChecked()
        for card in self.cards.values():
            if only_idle:
                card.setVisible(card.idle_count > 0)
            else:
                card.setVisible(True)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
