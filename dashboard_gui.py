#!/usr/bin/env python3
"""Modern C++ Benchmark & Profiling Studio GUI in PyQt5.

Clean presentation layer featuring interactive flame graphs, per-function
breakdown, syscall attribution, sample distribution charts, stage timings,
and run comparisons.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
from typing import Dict, List, Optional, Any

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QRectF, pyqtSlot

from benchmark_engine import (
    BenchmarkConfig,
    BenchmarkWorker,
    scan_cmake_targets,
    scan_adb_devices,
    format_ns,
)
from flamegraph_widget import FlameGraphWidget, format_duration
from export_utils import export_json, export_csv, export_html_report


DARK_QSS = """
/* Modern Dark Dashboard Theme - High Legibility & Low Noise */
QMainWindow, QDialog, QWidget {
    background-color: #12151b;
    color: #f1f5f9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    font-size: 14px;
}

QTabWidget::pane {
    border: 1px solid #283040;
    background-color: #181c25;
    border-radius: 8px;
    top: -1px;
}

QTabBar::tab {
    background: #141720;
    color: #94a3b8;
    padding: 10px 22px;
    min-width: 140px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
    font-size: 14px;
    border: 1px solid transparent;
}

QTabBar::tab:selected {
    background: #181c25;
    color: #60a5fa;
    border-color: #283040 #283040 #181c25 #283040;
    border-bottom: 2px solid #3b82f6;
}

QTabBar::tab:hover:!selected {
    background: #1c212c;
    color: #cbd5e1;
}

/* Controls */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #1e2430;
    border: 1px solid #333d52;
    border-radius: 6px;
    padding: 6px 12px;
    min-height: 22px;
    font-size: 14px;
    color: #f8fafc;
    selection-background-color: #3b82f6;
}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #3b82f6;
    background-color: #222938;
}

QComboBox::drop-down {
    border: none;
    width: 22px;
}

QComboBox QAbstractItemView {
    background-color: #1e2430;
    border: 1px solid #333d52;
    selection-background-color: #2563eb;
    color: #f8fafc;
    padding: 4px;
    font-size: 14px;
}

QPushButton {
    background-color: #242c3b;
    border: 1px solid #38445a;
    border-radius: 6px;
    padding: 7px 18px;
    min-height: 20px;
    color: #f1f5f9;
    font-size: 14px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #2e384b;
    border-color: #4b5a75;
}

QPushButton:pressed {
    background-color: #1c222e;
}

QPushButton#btn_run {
    background-color: #2563eb;
    border: 1px solid #3b82f6;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    padding: 7px 22px;
    border-radius: 6px;
}

QPushButton#btn_run:hover {
    background-color: #3b82f6;
    border-color: #60a5fa;
}

QPushButton#btn_run:pressed {
    background-color: #1d4ed8;
}

QCheckBox {
    color: #cbd5e1;
    font-size: 14px;
    font-weight: 500;
    spacing: 8px;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #475569;
    border-radius: 4px;
    background-color: #1e2430;
}

QCheckBox::indicator:checked {
    background-color: #3b82f6;
    border-color: #60a5fa;
}

/* Tables */
QTableWidget {
    background-color: #151821;
    gridline-color: #242b38;
    border: 1px solid #283040;
    border-radius: 6px;
    selection-background-color: #1e3a5f;
    selection-color: #ffffff;
    font-size: 14px;
}

QHeaderView::section {
    background-color: #212836;
    color: #cbd5e1;
    padding: 9px 12px;
    border: none;
    border-bottom: 2px solid #283040;
    border-right: 1px solid #202633;
    font-weight: 700;
    font-size: 13px;
}

QHeaderView::section:hover {
    background-color: #242b3a;
    color: #f1f5f9;
}

QProgressBar {
    border: 1px solid #283040;
    border-radius: 4px;
    background-color: #1a1e28;
    height: 10px;
    text-align: center;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #60a5fa);
    border-radius: 3px;
}

QScrollBar:vertical {
    border: none;
    background: #12151b;
    width: 12px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #2b3344;
    min-height: 28px;
    border-radius: 6px;
}

QScrollBar::handle:vertical:hover {
    background: #3f4a62;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollArea {
    border: none;
    background: transparent;
}
"""


class NumericTableWidgetItem(QtWidgets.QTableWidgetItem):
    """QTableWidgetItem that sorts numerically by an underlying numeric value."""

    def __init__(self, display_text: str, sort_value: float | int):
        super().__init__(display_text)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, NumericTableWidgetItem):
            return self.sort_value < other.sort_value
        return super().__lt__(other)


class TimelineChartWidget(QtWidgets.QWidget):
    """Custom chart showing sample durations over iterations and warmup settling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.samples: List[Dict[str, Any]] = []
        self.target_name: str = ""

    def set_samples(self, samples: List[Dict[str, Any]], target_name: str = ""):
        self.samples = samples
        self.target_name = target_name
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor("#151821"))

        if not self.samples:
            painter.setPen(QtGui.QColor("#64748b"))
            painter.setFont(QtGui.QFont("-apple-system", 13))
            painter.drawText(
                rect,
                Qt.AlignCenter,
                "No sample data recorded yet. Run a benchmark to view iteration timeline.",
            )
            painter.end()
            return

        valid_values = [
            s["elapsed_ns"] for s in self.samples
            if "error" not in s and isinstance(s.get("elapsed_ns"), (int, float))
            and s["elapsed_ns"] >= 0
        ]
        if not valid_values or max(valid_values) <= 0:
            painter.setPen(QtGui.QColor("#f87171"))
            painter.setFont(QtGui.QFont("-apple-system", 13))
            painter.drawText(rect, Qt.AlignCenter, "No valid sample durations recorded.")
            painter.end()
            return

        max_v = max(valid_values) * 1.15
        w = float(rect.width())
        h = float(rect.height())
        pad_left = 75
        pad_bottom = 48  # Bug 22: was 35, label at y=h-10 height=20 was clipped
        pad_top = 22
        pad_right = 24

        plot_w = w - pad_left - pad_right
        plot_h = h - pad_top - pad_bottom

        # Grid lines
        painter.setPen(QtGui.QColor("#242b38"))
        for step in range(4):
            y = pad_top + (plot_h / 3) * step
            painter.drawLine(int(pad_left), int(y), int(w - pad_right), int(y))
            val = max_v - (max_v / 3) * step
            painter.setPen(QtGui.QColor("#94a3b8"))
            painter.setFont(QtGui.QFont("-apple-system", 10))
            painter.drawText(
                5,
                int(y + 4),
                int(pad_left - 12),
                16,
                Qt.AlignRight,
                format_ns(val),
            )
            painter.setPen(QtGui.QColor("#242b38"))

        n = len(self.samples)
        if n == 1:
            step_x = plot_w / 2
        else:
            step_x = plot_w / (n - 1)

        points = []
        for i, s in enumerate(self.samples):
            is_valid = (
                "error" not in s
                and isinstance(s.get("elapsed_ns"), (int, float))
                and s["elapsed_ns"] >= 0
            )
            val = s.get("elapsed_ns") if is_valid else None
            x = pad_left + i * step_x
            y = pad_top + plot_h - (val / max_v) * plot_h if is_valid else pad_top + plot_h
            points.append((x, y, s.get("phase") == "warmup", is_valid))

        # Do not connect a valid duration to an invalid/failed measurement.
        painter.setPen(QtGui.QPen(QtGui.QColor("#3b82f6"), 2.0))
        for i in range(len(points) - 1):
            if points[i][3] and points[i + 1][3]:
                painter.drawLine(
                    QtCore.QPointF(points[i][0], points[i][1]),
                    QtCore.QPointF(points[i + 1][0], points[i + 1][1]),
                )

        # Draw sample points
        for x, y, is_warmup, is_valid in points:
            if not is_valid:
                painter.setPen(QtGui.QPen(QtGui.QColor("#f87171"), 2.0))
                painter.drawLine(QtCore.QPointF(x - 4, y - 4), QtCore.QPointF(x + 4, y + 4))
                painter.drawLine(QtCore.QPointF(x - 4, y + 4), QtCore.QPointF(x + 4, y - 4))
            elif is_warmup:
                painter.setBrush(QtGui.QBrush(QtGui.QColor("#f59e0b")))
                painter.setPen(QtGui.QPen(QtGui.QColor("#d97706"), 1.2))
                painter.drawEllipse(QtCore.QPointF(x, y), 4.0, 4.0)
            else:
                painter.setBrush(QtGui.QBrush(QtGui.QColor("#60a5fa")))
                painter.setPen(QtGui.QPen(QtGui.QColor("#2563eb"), 1.2))
                painter.drawEllipse(QtCore.QPointF(x, y), 4.0, 4.0)

        # Draw axis label — anchored to the bottom margin so it's always visible
        painter.setPen(QtGui.QColor("#cbd5e1"))
        painter.setFont(QtGui.QFont("-apple-system", 11, QtGui.QFont.Medium))
        painter.drawText(
            int(pad_left),
            int(h - pad_bottom + 6),  # Bug 22: was h-10 which clipped bottom half
            int(plot_w),
            20,
            Qt.AlignCenter,
            f"Attempts ({n}: Amber = Warmup, Blue = Valid, Red × = Invalid)",
        )
        painter.end()


class MetricCard(QtWidgets.QFrame):
    """Clean stat card with prominent number and high-contrast subtitle."""

    def __init__(
        self,
        title: str,
        value: str = "—",
        subtitle: str = "",
        accent_color: str = "#60a5fa",
        parent=None,
    ):
        super().__init__(parent)
        self.accent_color = accent_color
        self.setStyleSheet(
            """
            MetricCard {
                background-color: #191d26;
                border: 1px solid #283040;
                border-radius: 8px;
            }
            """
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(4)

        self.lbl_title = QtWidgets.QLabel(title.upper())
        self.lbl_title.setStyleSheet(
            "color: #94a3b8; font-size: 12px; font-weight: 700; letter-spacing: 0.5px;"
        )

        self.lbl_value = QtWidgets.QLabel(value)
        self.lbl_value.setStyleSheet(
            f"color: {accent_color}; font-size: 28px; font-weight: 700; margin: 3px 0;"
        )
        self.lbl_value.setWordWrap(True)  # Bug 4: prevent card inflation from long names
        self.lbl_value.setMaximumWidth(220)

        self.lbl_subtitle = QtWidgets.QLabel(subtitle)
        self.lbl_subtitle.setStyleSheet("color: #cbd5e1; font-size: 13px;")
        self.lbl_subtitle.setWordWrap(True)
        self.lbl_subtitle.setMaximumWidth(220)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_value)
        layout.addWidget(self.lbl_subtitle)

    def set_data(self, value: str, subtitle: str = ""):
        self.lbl_value.setText(value)
        self.lbl_subtitle.setText(subtitle)


class BenchmarkStudioWindow(QtWidgets.QMainWindow):
    """Main dashboard window reorganized for minimal clutter and large typography."""

    def __init__(
        self,
        initial_project: Optional[str] = None,
    ):
        super().__init__()
        self.setWindowTitle("Supernote C++ Benchmark & Profiling Studio")
        self.resize(1240, 880)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(DARK_QSS)

        if not initial_project:
            cwd = Path.cwd().resolve()
            if (cwd / "CMakeLists.txt").is_file():
                initial_project = str(cwd)
            elif (cwd / "fixtures" / "sample_project" / "CMakeLists.txt").is_file():
                initial_project = str(cwd / "fixtures" / "sample_project")
            elif (cwd.parent / "CMakeLists.txt").is_file():
                initial_project = str(cwd.parent)
            else:
                initial_project = str(cwd)

        self.project_path: Path = Path(initial_project).resolve()
        self.worker: Optional[BenchmarkWorker] = None
        self.current_report: Dict[str, Any] = {}

        self._build_ui()
        self._rescan_project()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(12)

        # 1. Consolidated Top Control Bar
        main_layout.addWidget(self._create_top_bar())

        # 2. 4 Clean Consolidated Tabs
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._create_overview_tab(), "1. Overview && Benchmark")
        self.tabs.addTab(self._create_functions_tab(), "2. Functions Breakdown")
        self.tabs.addTab(self._create_flamegraph_tab(), "3. Flame Graph")
        self.tabs.addTab(self._create_settings_and_log_tab(), "4. Settings && Build Log")
        main_layout.addWidget(self.tabs, stretch=1)
        self._on_adaptive_toggled(self.chk_adaptive.isChecked())
        self._on_run_mode_changed(self.cmb_run_mode.currentIndex())
        self._set_export_enabled(False)

    def _create_top_bar(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setStyleSheet(
            "background-color: #181c25; border: 1px solid #283040; border-radius: 8px;"
        )
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(5)

        # Target selector
        lbl_target = QtWidgets.QLabel("Target:")
        lbl_target.setStyleSheet("font-weight: 600; color: #cbd5e1;")
        layout.addWidget(lbl_target)
        self.cmb_target = QtWidgets.QComboBox()
        self.cmb_target.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.cmb_target.setMinimumWidth(75)
        layout.addWidget(self.cmb_target, 1)
        self.btn_browse_target = QtWidgets.QPushButton("↻")
        self.btn_browse_target.setToolTip("Rescan executable CMake targets in the selected project")
        self.btn_browse_target.setFixedWidth(28)
        self.btn_browse_target.setStyleSheet("padding: 4px;")
        self.btn_browse_target.clicked.connect(self._rescan_project)
        layout.addWidget(self.btn_browse_target)

        # Compare target
        lbl_compare = QtWidgets.QLabel("Compare:")
        lbl_compare.setStyleSheet("font-weight: 600; color: #cbd5e1;")
        layout.addWidget(lbl_compare)
        self.cmb_compare = QtWidgets.QComboBox()
        self.cmb_compare.addItem("(None)")
        self.cmb_compare.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.cmb_compare.setMinimumWidth(75)
        layout.addWidget(self.cmb_compare, 1)
        self.btn_browse_compare = QtWidgets.QPushButton("↻")
        self.btn_browse_compare.setToolTip("Rescan executable CMake targets in the selected project")
        self.btn_browse_compare.setFixedWidth(28)
        self.btn_browse_compare.setStyleSheet("padding: 4px;")
        self.btn_browse_compare.clicked.connect(self._rescan_project)
        layout.addWidget(self.btn_browse_compare)

        # Runs count
        self.lbl_runs = QtWidgets.QLabel("Min runs:")
        self.lbl_runs.setStyleSheet("font-weight: 600; color: #cbd5e1;")
        layout.addWidget(self.lbl_runs)
        self.spin_runs = QtWidgets.QSpinBox()
        self.spin_runs.setRange(2, 200)
        self.spin_runs.setValue(20)
        self.spin_runs.setFixedWidth(50)
        self.spin_runs.setToolTip("Minimum valid measured runs before adaptive convergence checks begin")
        layout.addWidget(self.spin_runs)

        # Mode
        lbl_mode = QtWidgets.QLabel("Mode:")
        lbl_mode.setStyleSheet("font-weight: 600; color: #cbd5e1;")
        layout.addWidget(lbl_mode)
        self.cmb_run_mode = QtWidgets.QComboBox()
        self.cmb_run_mode.addItems(["Combined", "Timing", "Profile"])
        self.cmb_run_mode.setMinimumWidth(120)
        self.cmb_run_mode.setToolTip(
            "Combined = direct timing plus one instrumented profile pass; "
            "Timing = direct timing only; Profile = one instrumented estimate."
        )
        self.cmb_run_mode.currentIndexChanged.connect(self._on_run_mode_changed)
        layout.addWidget(self.cmb_run_mode)

        layout.addStretch(1)

        # Single live status & progress indicator
        self.lbl_status = QtWidgets.QLabel("Status: Ready")
        self.lbl_status.setStyleSheet("color: #38bdf8; font-weight: 600; font-size: 13px;")
        self.lbl_status.setMinimumWidth(80)
        self.lbl_status.setMaximumWidth(120)
        self.lbl_status.setWordWrap(False)
        layout.addWidget(self.lbl_status)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(70)
        layout.addWidget(self.progress_bar)

        # Primary Run Button
        self.btn_run = QtWidgets.QPushButton("▶ Run Benchmark")
        self.btn_run.setObjectName("btn_run")
        self.btn_run.setStyleSheet("padding: 7px 16px;")
        self.btn_run.setMinimumWidth(130)
        self.btn_run.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
        self.btn_run.clicked.connect(self._on_toggle_run)
        layout.addWidget(self.btn_run)

        return card

    # Tab 1: Overview & Benchmark (Scrollable Dashboard)
    def _create_overview_tab(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        # 1. Prominent Metric Cards Row
        cards_row = QtWidgets.QHBoxLayout()
        cards_row.setSpacing(14)

        self.card_median = MetricCard(
            "Median Latency", "—", "Mean: —", accent_color="#60a5fa"
        )
        self.card_precision = MetricCard(
            "Stability & Error", "—", "Drift: —", accent_color="#34d399"
        )
        self.card_compare = MetricCard(
            "Comparison", "—", "No compare target", accent_color="#f43f5e"
        )
        self.card_runs = MetricCard(
            "Samples", "—", "Warmups: —", accent_color="#a78bfa"
        )

        cards_row.addWidget(self.card_median)
        cards_row.addWidget(self.card_precision)
        cards_row.addWidget(self.card_compare)
        cards_row.addWidget(self.card_runs)
        layout.addLayout(cards_row)

        # 2. Side-by-Side Comparison Section
        comp_header = QtWidgets.QLabel("Side-by-Side Comparison")
        comp_header.setStyleSheet("font-size: 16px; font-weight: 700; color: #f1f5f9; margin-top: 6px;")
        layout.addWidget(comp_header)

        self.tbl_comparison = QtWidgets.QTableWidget()
        self.tbl_comparison.setColumnCount(5)
        self.tbl_comparison.setHorizontalHeaderLabels(
            ["Metric", "Target A", "Target B", "Difference", "Speedup"]
        )
        self.tbl_comparison.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch
        )
        for c in range(1, 5):
            self.tbl_comparison.horizontalHeader().setSectionResizeMode(
                c, QtWidgets.QHeaderView.ResizeToContents
            )
        self.tbl_comparison.verticalHeader().setDefaultSectionSize(36)
        self.tbl_comparison.setMinimumHeight(170)
        layout.addWidget(self.tbl_comparison)

        # Initial prompt row in comparison table
        self.tbl_comparison.setRowCount(1)
        self.tbl_comparison.setItem(
            0,
            0,
            QtWidgets.QTableWidgetItem(
                "Single target mode. Select a 'Compare' target in the top bar to see side-by-side metrics."
            ),
        )

        # 3. Iteration Timeline Chart Section
        timeline_header = QtWidgets.QLabel("Iteration Timeline & Settling")
        timeline_header.setStyleSheet("font-size: 16px; font-weight: 700; color: #f1f5f9; margin-top: 6px;")
        layout.addWidget(timeline_header)

        self.timeline_chart = TimelineChartWidget()
        layout.addWidget(self.timeline_chart)

        # Summary box under timeline
        self.txt_timeline_stats = QtWidgets.QTextBrowser()
        self.txt_timeline_stats.setMaximumHeight(105)
        self.txt_timeline_stats.setStyleSheet(
            "background-color: #151821; border: 1px solid #283040; border-radius: 6px; padding: 10px; color: #cbd5e1; font-size: 13px;"
        )
        self.txt_timeline_stats.setHtml("<span style='color: #64748b;'>Run a benchmark to view iteration statistics.</span>")
        layout.addWidget(self.txt_timeline_stats)

        # 4. Quick Export Row
        export_card = QtWidgets.QFrame()
        export_card.setStyleSheet(
            "background-color: #151821; border: 1px solid #283040; border-radius: 6px;"
        )
        export_layout = QtWidgets.QHBoxLayout(export_card)
        export_layout.setContentsMargins(14, 10, 14, 10)
        export_layout.setSpacing(12)

        lbl_export = QtWidgets.QLabel("Export Results:")
        lbl_export.setStyleSheet("font-weight: 600; font-size: 14px; color: #cbd5e1;")
        export_layout.addWidget(lbl_export)

        self.btn_export_json = QtWidgets.QPushButton("📄 Export JSON")
        self.btn_export_json.clicked.connect(self._on_export_json)
        self.btn_export_csv = QtWidgets.QPushButton("📊 Export CSV")
        self.btn_export_csv.clicked.connect(self._on_export_csv)
        self.btn_export_html = QtWidgets.QPushButton("🌐 Export HTML Report")
        self.btn_export_html.clicked.connect(self._on_export_html)

        export_layout.addWidget(self.btn_export_json)
        export_layout.addWidget(self.btn_export_csv)
        export_layout.addWidget(self.btn_export_html)
        export_layout.addStretch(1)

        layout.addWidget(export_card)
        layout.addStretch(1)

        scroll.setWidget(content)
        return scroll

    # Tab 2: Functions Breakdown Table & Details
    def _create_functions_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Top summary cards for function profile
        top_cards = QtWidgets.QHBoxLayout()
        top_cards.setSpacing(14)
        self.card_bottleneck = MetricCard(
            "Bottleneck Function", "—", "Top self time", accent_color="#fb923c"
        )
        self.card_syscalls = MetricCard(
            "Total Syscalls", "—", "Attributed to calls", accent_color="#38bdf8"
        )
        top_cards.addWidget(self.card_bottleneck)
        top_cards.addWidget(self.card_syscalls)
        top_cards.addStretch(1)
        layout.addLayout(top_cards)

        self.lbl_profile_note = QtWidgets.QLabel("No function profile was collected for this run.")
        self.lbl_profile_note.setWordWrap(True)
        self.lbl_profile_note.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(self.lbl_profile_note)

        # Search bar row
        search_bar = QtWidgets.QHBoxLayout()
        self.txt_func_search = QtWidgets.QLineEdit()
        self.txt_func_search.setPlaceholderText("🔍 Filter functions by name...")
        self.txt_func_search.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.txt_func_search.textChanged.connect(self._filter_functions_table)
        self.txt_func_search.setFixedHeight(36)
        search_bar.addWidget(self.txt_func_search, stretch=1)

        self.lbl_func_count = QtWidgets.QLabel("0 functions recorded")
        self.lbl_func_count.setStyleSheet("color: #94a3b8; font-size: 13px;")
        search_bar.addWidget(self.lbl_func_count)
        layout.addLayout(search_bar)

        # Splitter: Table on left, Details on right
        splitter = QtWidgets.QSplitter(Qt.Horizontal)

        # Functions Table
        self.tbl_functions = QtWidgets.QTableWidget()
        self.tbl_functions.setColumnCount(8)
        self.tbl_functions.setHorizontalHeaderLabels(
            [
                "Function Name",
                "Calls",
                "Total Time",
                "% Total",
                "Self Time",
                "% Self",
                "Avg / Call",
                "Syscalls",
            ]
        )
        self.tbl_functions.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch
        )
        for col in range(1, 8):
            self.tbl_functions.horizontalHeader().setSectionResizeMode(
                col, QtWidgets.QHeaderView.ResizeToContents
            )
        self.tbl_functions.verticalHeader().setDefaultSectionSize(36)
        self.tbl_functions.itemSelectionChanged.connect(
            self._on_function_selected
        )
        self.tbl_functions.doubleClicked.connect(
            self._on_function_double_clicked
        )
        self.tbl_functions.setSortingEnabled(True)
        self.tbl_functions.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)  # Bug 9
        splitter.addWidget(self.tbl_functions)

        # Right pane: Details & Syscall Attribution Sub-Tabs
        right_tabs = QtWidgets.QTabWidget()
        
        # Sub-tab A: Function Details
        detail_widget = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(8)

        self.lbl_detail_title = QtWidgets.QLabel("Select a Function")
        self.lbl_detail_title.setStyleSheet(
            "color: #60a5fa; font-size: 15px; font-weight: 700;"
        )
        self.lbl_detail_title.setWordWrap(True)  # Bug 5: prevent splitter expansion
        self.lbl_detail_title.setMaximumWidth(360)
        detail_layout.addWidget(self.lbl_detail_title)

        self.txt_detail_body = QtWidgets.QTextBrowser()
        self.txt_detail_body.setStyleSheet(
            "background-color: #12151b; border: 1px solid #232a38; border-radius: 6px; padding: 8px; color: #cbd5e1; font-size: 13px;"
        )
        self.txt_detail_body.setHtml("<span style='color: #64748b;'>Click any function in the table to inspect details. Double-click to view in Flame Graph.</span>")
        detail_layout.addWidget(self.txt_detail_body)
        right_tabs.addTab(detail_widget, "Selected Function")

        # Sub-tab B: Global Syscall Attribution
        syscall_widget = QtWidgets.QWidget()
        sc_layout = QtWidgets.QVBoxLayout(syscall_widget)
        sc_layout.setContentsMargins(10, 10, 10, 10)

        self.tbl_syscalls = QtWidgets.QTableWidget()
        self.tbl_syscalls.setColumnCount(4)
        self.tbl_syscalls.setHorizontalHeaderLabels(
            ["Syscall Name", "Total Count", "Attributed Function(s)", "% Calls"]
        )
        self.tbl_syscalls.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.Stretch
        )
        self.tbl_syscalls.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )
        self.tbl_syscalls.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeToContents
        )
        self.tbl_syscalls.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeToContents
        )
        self.tbl_syscalls.verticalHeader().setDefaultSectionSize(32)
        self.tbl_syscalls.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)  # Bug 9
        self.tbl_syscalls.setSortingEnabled(True)  # Bug 10 prerequisite
        sc_layout.addWidget(self.tbl_syscalls)
        right_tabs.addTab(syscall_widget, "Syscalls Summary")

        splitter.addWidget(right_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)
        return w

    # Tab 3: Flame Graph Tab
    def _create_flamegraph_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Minimal controls bar
        ctrl = QtWidgets.QHBoxLayout()
        self.btn_reset_zoom = QtWidgets.QPushButton("↺ Reset Zoom")
        self.btn_reset_zoom.clicked.connect(self._on_reset_flame_zoom)
        ctrl.addWidget(self.btn_reset_zoom)

        self.txt_flame_search = QtWidgets.QLineEdit()
        self.txt_flame_search.setPlaceholderText("🔍 Highlight function...")
        self.txt_flame_search.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.txt_flame_search.textChanged.connect(self._on_flame_search_changed)
        ctrl.addWidget(self.txt_flame_search, stretch=1)

        self.cmb_flame_dir = QtWidgets.QComboBox()
        self.cmb_flame_dir.addItems(["Top-Down (Icicle)", "Bottom-Up (Flame)"])
        self.cmb_flame_dir.currentIndexChanged.connect(
            self._on_flame_dir_changed
        )
        ctrl.addWidget(self.cmb_flame_dir)

        self.cmb_flame_color = QtWidgets.QComboBox()
        self.cmb_flame_color.addItems(
            ["Self-Time Heat", "Function Hash", "Call Depth"]
        )
        self.cmb_flame_color.currentIndexChanged.connect(
            self._on_flame_color_changed
        )
        ctrl.addWidget(self.cmb_flame_color)

        ctrl.addStretch()
        layout.addLayout(ctrl)

        # Breadcrumbs bar
        self.lbl_breadcrumbs = QtWidgets.QLabel("Path: Root")
        self.lbl_breadcrumbs.setStyleSheet(
            "background-color: #191d26; color: #94a3b8; padding: 8px 14px; border-radius: 6px; font-weight: 500; font-size: 13px;"
        )
        self.lbl_breadcrumbs.setWordWrap(True)  # Bug 6: prevent horizontal overflow on deep paths
        layout.addWidget(self.lbl_breadcrumbs)

        # Flame Graph Widget
        self.flame_widget = FlameGraphWidget()
        self.flame_widget.node_selected.connect(
            self._on_flame_node_clicked_from_widget
        )
        self.flame_widget.zoom_changed.connect(
            self._on_flame_zoom_changed
        )
        layout.addWidget(self.flame_widget, stretch=1)

        return w

    # Tab 4: Settings & Build Log
    def _create_settings_and_log_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Configuration Box
        cfg_frame = QtWidgets.QFrame()
        cfg_frame.setStyleSheet("background-color: #161922; border: 1px solid #283040; border-radius: 8px;")
        cfg_layout = QtWidgets.QVBoxLayout(cfg_frame)
        cfg_layout.setContentsMargins(16, 14, 16, 14)
        cfg_layout.setSpacing(12)

        lbl_cfg = QtWidgets.QLabel("Benchmark & Environment Settings")
        lbl_cfg.setStyleSheet("font-size: 15px; font-weight: 700; color: #60a5fa;")
        cfg_layout.addWidget(lbl_cfg)

        # Row 1: Project Path
        r1 = QtWidgets.QHBoxLayout()
        r1.addWidget(QtWidgets.QLabel("Project Path:"))
        self.txt_project = QtWidgets.QLineEdit(str(self.project_path))
        self.txt_project.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.txt_project.editingFinished.connect(self._apply_project_path)
        self.btn_browse = QtWidgets.QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._on_browse_project)
        r1.addWidget(self.txt_project, stretch=1)
        r1.addWidget(self.btn_browse)
        cfg_layout.addLayout(r1)

        # Row 2: Environment & Warmups
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(QtWidgets.QLabel("Execution:"))
        self.cmb_mode = QtWidgets.QComboBox()
        self.cmb_mode.addItems(["Local Host", "Android (ADB)"])
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        r2.addWidget(self.cmb_mode)

        r2.addWidget(QtWidgets.QLabel("Device:"))
        self.cmb_device = QtWidgets.QComboBox()
        self.cmb_device.setEnabled(False)
        self.cmb_device.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.cmb_device.setMinimumWidth(120)
        r2.addWidget(self.cmb_device, stretch=1)

        self.chk_adaptive = QtWidgets.QCheckBox("Adaptive Mode (Run until stable)")
        self.chk_adaptive.setChecked(True)
        self.chk_adaptive.toggled.connect(self._on_adaptive_toggled)
        r2.addWidget(self.chk_adaptive)

        r2.addWidget(QtWidgets.QLabel("Warmups:"))
        self.spin_warmup = QtWidgets.QSpinBox()
        self.spin_warmup.setRange(0, 20)
        self.spin_warmup.setValue(2)
        self.spin_warmup.setFixedWidth(65)
        r2.addWidget(self.spin_warmup)
        
        r2.addStretch(1)
        cfg_layout.addLayout(r2)

        self.lbl_adaptive_limits = QtWidgets.QLabel()
        self.lbl_adaptive_limits.setStyleSheet("color: #94a3b8; font-size: 12px;")
        cfg_layout.addWidget(self.lbl_adaptive_limits)

        # Row 3: Profiling & Syscalls
        r3 = QtWidgets.QHBoxLayout()
        self.lbl_profile_mode = QtWidgets.QLabel()
        self.lbl_profile_mode.setStyleSheet("color: #cbd5e1; font-size: 13px;")
        r3.addWidget(self.lbl_profile_mode)

        self.chk_syscalls = QtWidgets.QCheckBox("Trace Syscalls (strace)")
        self.chk_syscalls.setChecked(True)
        r3.addWidget(self.chk_syscalls)
        r3.addStretch(1)
        cfg_layout.addLayout(r3)

        layout.addWidget(cfg_frame)

        # Compiler & Execution Log
        log_header = QtWidgets.QHBoxLayout()
        lbl_log = QtWidgets.QLabel("Compiler & Execution Log")
        lbl_log.setStyleSheet("font-size: 15px; font-weight: 700; color: #f1f5f9;")
        log_header.addWidget(lbl_log)
        log_header.addStretch(1)

        self.btn_clear_log = QtWidgets.QPushButton("Clear Log")
        self.btn_clear_log.clicked.connect(lambda: self.txt_log.clear())
        log_header.addWidget(self.btn_clear_log)
        layout.addLayout(log_header)

        self.txt_log = QtWidgets.QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(
            "background-color: #0d1016; border: 1px solid #1f2533; border-radius: 6px; font-family: monospace; font-size: 13px; color: #93c5fd; padding: 12px;"
        )
        layout.addWidget(self.txt_log, stretch=1)

        return w

    # Actions & Event Handlers
    def _rescan_project(self):
        targets = scan_cmake_targets(self.project_path)
        self.cmb_target.clear()
        self.cmb_compare.clear()
        self.cmb_compare.addItem("(None)")
        for t in targets:
            self.cmb_target.addItem(t)
            self.cmb_compare.addItem(t)

        devices = scan_adb_devices()
        self.cmb_device.clear()
        for d in devices:
            self.cmb_device.addItem(d)

    def _on_browse_project(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select CMake Project Directory", str(self.project_path)
        )
        if d:
            self.project_path = Path(d).resolve()
            self.txt_project.setText(str(self.project_path))
            self._rescan_project()

    def _apply_project_path(self):
        candidate = Path(self.txt_project.text()).expanduser().resolve()
        if not (candidate / "CMakeLists.txt").is_file():
            self.lbl_status.setText("Status: Project must contain CMakeLists.txt")
            self.lbl_status.setStyleSheet("color: #f59e0b; font-weight: 600; font-size: 13px;")
            self.txt_project.setText(str(self.project_path))
            return
        self.project_path = candidate
        self.txt_project.setText(str(candidate))
        self._rescan_project()
        self.lbl_status.setText("Status: Project targets refreshed")
        self.lbl_status.setStyleSheet("color: #38bdf8; font-weight: 600; font-size: 13px;")

    def _on_mode_changed(self, idx: int):
        is_adb = idx == 1
        self.cmb_device.setEnabled(is_adb)

    def _on_run_mode_changed(self, idx: int):
        if idx == 0:
            self.lbl_profile_mode.setText(
                "Timing + Profile runs a measured timing pass and one instrumented profile pass."
            )
        elif idx == 1:
            self.lbl_profile_mode.setText("Timing only skips function profiling.")
        else:
            self.lbl_profile_mode.setText(
                "Profile only uses one instrumented run; its timings are estimates."
            )

    def _on_adaptive_toggled(self, checked: bool):
        if checked:
            self.lbl_runs.setText("Min runs:")
            self.spin_runs.setMaximum(200)
            self.spin_runs.setToolTip(
                "Minimum valid measured runs before convergence checks. "
                "Adaptive sampling is capped at 200 runs or 30 seconds."
            )
            self.lbl_adaptive_limits.setText(
                "Adaptive limits: at least the chosen run count, at most 200 runs or 30 seconds; "
                "3% RSE and 6% median-drift thresholds."
            )
        else:
            self.lbl_runs.setText("Runs:")
            self.spin_runs.setMaximum(500)
            self.spin_runs.setToolTip("Exact number of valid measured runs")
            self.lbl_adaptive_limits.setText("Fixed mode: run exactly the selected number of valid measured samples.")

    def _set_controls_enabled(self, enabled: bool):
        """Enable or lock every control that can alter the active run."""
        self.cmb_target.setEnabled(enabled)
        self.cmb_compare.setEnabled(enabled)
        self.cmb_run_mode.setEnabled(enabled)
        self.spin_runs.setEnabled(enabled)
        self.spin_warmup.setEnabled(enabled)
        self.chk_adaptive.setEnabled(enabled)
        self.chk_syscalls.setEnabled(enabled)
        self.cmb_mode.setEnabled(enabled)
        self.cmb_device.setEnabled(enabled and self.cmb_mode.currentIndex() == 1)
        self.txt_project.setEnabled(enabled)
        self.btn_browse_target.setEnabled(enabled)
        self.btn_browse_compare.setEnabled(enabled)
        self.btn_browse.setEnabled(enabled)

    def _on_toggle_run(self):
        if self.worker and self.worker.isRunning():
            self.btn_run.setEnabled(False)
            self.lbl_status.setText("Status: Cancelling...")
            self.worker.cancel()
            return

        # Bug 14: guard against empty target
        if not self.cmb_target.currentText().strip():
            self.lbl_status.setText("Status: No target selected")
            self.lbl_status.setStyleSheet("color: #f59e0b; font-weight: 600; font-size: 13px;")
            return

        mode_idx = self.cmb_run_mode.currentIndex()
        run_mode = "combined" if mode_idx == 0 else "time" if mode_idx == 1 else "profile"

        cfg = BenchmarkConfig(
            project_dir=self.project_path,
            target=self.cmb_target.currentText(),
            run_mode=run_mode,
            compare_target=(
                None
                if self.cmb_compare.currentText() in ("(None)", "", self.cmb_target.currentText())
                else self.cmb_compare.currentText()
            ),
            profile_functions=run_mode != "time",
            trace_syscalls=self.chk_syscalls.isChecked(),
            adb=self.cmb_mode.currentIndex() == 1,
            device=(
                self.cmb_device.currentText()
                if self.cmb_device.isEnabled()
                else None
            ),
            runs=None if self.chk_adaptive.isChecked() else self.spin_runs.value(),
            min_runs=self.spin_runs.value(),
            warmup=self.spin_warmup.value(),
        )

        self.btn_run.setText("⬛ Cancel")
        self.btn_run.setStyleSheet(
            "QPushButton { background-color: #dc2626; border: 1px solid #ef4444; "
            "color: #ffffff; font-size: 14px; font-weight: 700; "
            "padding: 7px 22px; border-radius: 6px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )

        self.txt_log.clear()
        self.current_report = {}
        self._set_export_enabled(False)
        self.progress_bar.setValue(0)
        self.timeline_chart.set_samples([])
        self.lbl_status.setText("Status: Configuring...")
        self.lbl_status.setStyleSheet("color: #38bdf8; font-weight: 600; font-size: 13px;")
        self._set_controls_enabled(False)  # Bug 12: lock controls during run

        self.worker = BenchmarkWorker(cfg)
        self.worker.sig_log.connect(self._on_worker_log)
        self.worker.sig_status.connect(self._on_worker_status)
        self.worker.sig_stage.connect(self._on_worker_stage)
        self.worker.sig_samples.connect(self._on_worker_samples)
        self.worker.sig_stability.connect(self._on_worker_stability)
        self.worker.sig_finished.connect(self._on_worker_finished)
        self.worker.sig_error.connect(self._on_worker_error)
        self.worker.sig_cancelled.connect(self._on_worker_cancelled)
        # Note: we deliberately DO NOT switch tabs to log, so user can stay on Overview or Functions
        self.worker.start()

    @pyqtSlot(str)
    def _on_worker_log(self, text: str):
        self.txt_log.append(text)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    @pyqtSlot(str)
    def _on_worker_status(self, text: str):
        self.lbl_status.setText(f"Status: {text}")

    @pyqtSlot(str, int, int)
    def _on_worker_stage(self, name: str, current: int, total: int):
        self.lbl_status.setText(f"Stage {current}/{total}: {name}")
        pct = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)

    @pyqtSlot(dict)
    def _on_worker_sample(self, s: dict):
        # Update timeline in real time
        curr = self.timeline_chart.samples.copy()
        curr.append(s)
        self.timeline_chart.set_samples(curr, s.get("target", ""))

    @pyqtSlot(list)
    def _on_worker_samples(self, samples: list):
        # Coalesced worker updates avoid repainting between every benchmarked
        # process, while still keeping the visible timeline current.
        if not samples:
            return
        curr = self.timeline_chart.samples.copy()
        curr.extend(samples)
        self.timeline_chart.set_samples(curr, samples[-1].get("target", ""))

    @pyqtSlot(dict)
    def _on_worker_stability(self, stab: dict):
        rse = stab.get("relative_standard_error_percent")
        drift = stab.get("median_drift_percent")
        passed = stab.get("passed")
        if rse is not None:
            self.card_precision.set_data(
                f"{rse:.2f}% RSE",
                f"Drift: {drift:.1f}% • {'STABLE' if passed else 'Sampling'}",
            )

    @pyqtSlot(dict)
    def _on_worker_finished(self, report: dict):
        self.btn_run.setText("▶ Run Benchmark")
        self.btn_run.setStyleSheet("")
        self.btn_run.setEnabled(True)
        self._set_controls_enabled(True)  # Bug 12
        self.lbl_status.setText("Status: Complete")
        self.progress_bar.setValue(100)

        self.current_report = report
        self._populate_dashboard(report)
        self._set_export_enabled(True)

    @pyqtSlot(str)
    def _on_worker_error(self, err: str):
        self.btn_run.setText("▶ Run Benchmark")
        self.btn_run.setStyleSheet("")
        self.btn_run.setEnabled(True)
        self._set_controls_enabled(True)  # Bug 12
        self.current_report = {}
        self._set_export_enabled(False)
        self.lbl_status.setText(f"Status: Failed - {err[:50]}")
        self.lbl_status.setStyleSheet("color: #ef4444; font-weight: 600; font-size: 13px;")
        # Switch to settings & log tab so user can see compiler output in the terminal log
        self.tabs.setCurrentIndex(3)

    @pyqtSlot()
    def _on_worker_cancelled(self):
        self.btn_run.setText("▶ Run Benchmark")
        self.btn_run.setStyleSheet("")
        self.btn_run.setEnabled(True)
        self._set_controls_enabled(True)  # Bug 12
        self.current_report = {}
        self._set_export_enabled(False)
        self.lbl_status.setText("Status: Cancelled")

    def _populate_dashboard(self, report: dict):
        target = report.get("target", "")
        summary = report.get("summary", {})
        profile = report.get("profile", {})
        functions = profile.get("functions", [])
        tree = profile.get("tree", [])
        syscalls = report.get("syscalls", {})

        # Total profiled runtime for percentage calculations
        root_total_ns = profile.get("total_duration_ns", 0)
        if root_total_ns <= 0 and tree and len(tree) > 0:
            root_total_ns = tree[0].get("total_ns", 0)
        if root_total_ns <= 0:
            root_total_ns = summary.get("median_ns", 0)
        if root_total_ns <= 0 and functions:
            root_total_ns = max((fn.get("total_ns", 0) for fn in functions), default=1)

        # 1. Update Primary Metric Cards
        med_ns = summary.get("median_ns", 0)
        mean_ns = summary.get("mean_ns", 0)
        std_ns = summary.get("stddev_ns", 0)
        self.card_median.set_data(
            format_duration(med_ns),
            f"Mean: {format_duration(mean_ns)} ± {format_duration(std_ns)}",
        )

        rse = summary.get("relative_standard_error_percent")
        drift = summary.get("median_drift_percent")
        if rse is not None:
            self.card_precision.set_data(
                f"{rse:.2f}% RSE",
                f"Drift: {drift:.1f}% • {'Stable' if summary.get('passed') else 'Adaptive'}",
            )
        else:
            self.card_precision.set_data("Fixed Runs", "No drift limit")

        self.card_runs.set_data(
            f"{report.get('runs') or 0} valid runs",
            (
                f"{report.get('warmup_runs', 0) or 0} warmups discarded"
                if not report.get("invalid_runs")
                else f"{report.get('invalid_runs')} invalid of {report.get('attempted_runs', 0)} attempts"
            ),
        )

        # Comparison Card
        comp = report.get("comparison")
        if comp:
            speedup = comp.get("speedup", 1.0)
            fastest = comp.get("fastest", "")
            self.card_compare.set_data(
                f"{speedup:.2f}x", f"Fastest: {fastest}"
            )
        else:
            self.card_compare.set_data("Single Target", "No compare target")

        # Bottleneck Function Card
        if functions:
            top_fn = max(functions, key=lambda item: item.get("self_ns", 0))
            pct = (top_fn.get("self_ns", 0) / max(1, root_total_ns)) * 100
            self.card_bottleneck.set_data(
                f"{pct:.1f}% self", f"{top_fn.get('name', '')[:32]} of profiled runtime"
            )
        else:
            self.card_bottleneck.set_data("—", "Scope timing only")

        # Syscalls Card
        syscall_error = syscalls.get("error")
        if syscall_error:
            self.card_syscalls.set_data("Unavailable", syscall_error)
        else:
            tot_sc = syscalls.get("total_syscalls", 0)
            top_sc = list(syscalls.get("summary", {}).keys())[:3]
            self.card_syscalls.set_data(
                f"{tot_sc:,} calls", ", ".join(top_sc) if top_sc else "0 recorded"
            )

        timing_basis = profile.get("timing_basis")
        if timing_basis == "normalized_profile_estimate":
            self.lbl_profile_note.setText(
                "Function timings are normalized estimates from one instrumented run; "
                "the overview latency is the direct measurement."
            )
        elif timing_basis == "instrumented_profile_run":
            self.lbl_profile_note.setText(
                "Function timings come from one instrumented run and are not a precision latency measurement."
            )
        else:
            self.lbl_profile_note.setText("No function profile was collected for this run.")

        # 2. Populate Comparison Table
        self._populate_comparison_table(report)

        # 3. Populate Functions Table
        self._populate_functions_table(functions, root_total_ns)

        # 4. Populate Flame Graph
        sc_map = syscalls.get("by_function", {})
        self.flame_widget.set_tree_data(tree, sc_map)
        self.lbl_breadcrumbs.setText("Path: Root")

        # 5. Populate Syscalls Sub-Tab
        self._populate_syscalls_table(syscalls)

        # 6. Populate Timeline Tab
        samples = report.get("targets", {}).get(target, {}).get("samples", [])
        warmups = report.get("targets", {}).get(target, {}).get("warmups", [])
        all_samples = [
            {**s, "phase": "warmup"} for s in warmups
        ] + [{**s, "phase": "sample"} for s in samples]
        self.timeline_chart.set_samples(all_samples, target)

        if summary:
            rse_str = f"{rse:.2f}%" if rse is not None else "N/A"
            var = summary.get("variability_percent")
            var_str = f"{var:.2f}%" if var is not None else "N/A"
            self.txt_timeline_stats.setHtml(
                f"""
                <b>Sample Statistics:</b><br>
                • <b>Median:</b> {format_duration(summary.get('median_ns', 0))} | 
                <b>Mean:</b> {format_duration(summary.get('mean_ns', 0))} | 
                <b>Min:</b> {format_duration(summary.get('min_ns', 0))} | 
                <b>Max:</b> {format_duration(summary.get('max_ns', 0))}<br>
                • <b>Std Dev:</b> {format_duration(std_ns if std_ns is not None else 0)} | 
                <b>Variability:</b> {var_str} | 
                <b>RSE:</b> {rse_str}
            """
            )

    def _set_export_enabled(self, enabled: bool):
        for button in (self.btn_export_json, self.btn_export_csv, self.btn_export_html):
            button.setEnabled(enabled)

    def _populate_comparison_table(self, report: dict):
        comp = report.get("comparison")
        if not comp:
            # Reset headers to neutral
            self.tbl_comparison.setHorizontalHeaderLabels(
                ["Metric", "—", "—", "Difference", "Speedup"]
            )
            self.tbl_comparison.setRowCount(1)
            self.tbl_comparison.clearSpans()
            self.tbl_comparison.setSpan(0, 0, 1, 5)
            self.tbl_comparison.setItem(
                0,
                0,
                QtWidgets.QTableWidgetItem(
                    "Single target benchmark. Select a 'Compare' target in the top bar to see side-by-side comparison."
                ),
            )
            return

        t_a = comp.get("target_a", "A")
        t_b = comp.get("target_b", "B")
        fastest = comp.get("fastest", "")
        speedup = comp.get("speedup", 1.0)

        # Bug 2: update headers with actual binary names
        self.tbl_comparison.setHorizontalHeaderLabels(
            ["Metric", t_a, t_b, "Difference", "Speedup"]
        )
        # Bug 7: Speedup col → Stretch so long target names don't squeeze Metric col
        self.tbl_comparison.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.Stretch
        )
        for c in range(1, 4):
            self.tbl_comparison.horizontalHeader().setSectionResizeMode(
                c, QtWidgets.QHeaderView.ResizeToContents
            )

        targets_dict = report.get("targets", {})
        sum_a = targets_dict.get(t_a, {}).get("summary", {})
        sum_b = targets_dict.get(t_b, {}).get("summary", {})

        metrics = [
            ("Median Latency", "median_ns", True),
            ("Mean Latency", "mean_ns", True),
            ("Min Latency", "min_ns", True),
            ("Max Latency", "max_ns", True),
            ("Std Dev", "stddev_ns", True),
        ]

        self.tbl_comparison.setRowCount(len(metrics))
        self.tbl_comparison.clearSpans()
        for row, (name, key, is_duration) in enumerate(metrics):
            val_a = sum_a.get(key, 0)
            val_b = sum_b.get(key, 0)

            delta = val_b - val_a
            diff_str = format_duration(abs(delta)) if is_duration else f"{abs(delta)}"
            if delta < 0:
                diff_str = f"-{diff_str}"
            else:
                diff_str = f"+{diff_str}"

            if row == 0:
                speedup_str = f"{speedup:.2f}x ({fastest} faster)"
            else:
                ratio = (max(val_a, val_b) / max(1, min(val_a, val_b))) if min(val_a, val_b) > 0 else 1.0
                speedup_str = f"{ratio:.2f}x"

            self.tbl_comparison.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.tbl_comparison.setItem(
                row, 1, QtWidgets.QTableWidgetItem(format_duration(val_a) if is_duration else str(val_a))
            )
            self.tbl_comparison.setItem(
                row, 2, QtWidgets.QTableWidgetItem(format_duration(val_b) if is_duration else str(val_b))
            )
            self.tbl_comparison.setItem(row, 3, QtWidgets.QTableWidgetItem(diff_str))
            self.tbl_comparison.setItem(row, 4, QtWidgets.QTableWidgetItem(speedup_str))

    def _populate_functions_table(self, functions: List[Dict[str, Any]], root_total_ns: int = 0):
        self.tbl_functions.setSortingEnabled(False)
        self.tbl_functions.setRowCount(len(functions))
        
        # Ensure non-zero denominator for percentage calculations
        if root_total_ns <= 0:
            root_total_ns = max(1, sum(item.get("self_ns", 0) for item in functions))

        self.lbl_func_count.setText(f"{len(functions)} functions recorded")

        sorted_funcs = sorted(
            functions, key=lambda item: item.get("self_ns", 0), reverse=True
        )
        for row, fn in enumerate(sorted_funcs):
            name = fn.get("name", "unknown")
            calls = fn.get("calls", 1)
            tot_ns = fn.get("total_ns", 0)
            self_ns = fn.get("self_ns", 0)
            avg_ns = (tot_ns / calls) if calls > 0 else 0
            sc = fn.get("syscalls", {})
            sc_count = sum(sc.values()) if sc else 0

            # Percentage relative to root execution time, clamped to 100.0%
            pct_tot = min(100.0, (tot_ns / root_total_ns) * 100.0)
            pct_self = min(100.0, (self_ns / root_total_ns) * 100.0)

            it_name = QtWidgets.QTableWidgetItem(name)
            it_name.setData(Qt.UserRole, fn)
            it_calls = NumericTableWidgetItem(f"{calls:,}", calls)
            it_tot = NumericTableWidgetItem(format_duration(tot_ns), tot_ns)
            it_pct_tot = NumericTableWidgetItem(f"{pct_tot:.1f}%", pct_tot)
            it_self = NumericTableWidgetItem(format_duration(self_ns), self_ns)
            it_pct_self = NumericTableWidgetItem(f"{pct_self:.1f}%", pct_self)
            it_avg = NumericTableWidgetItem(format_duration(avg_ns), avg_ns)
            it_sc = NumericTableWidgetItem(f"{sc_count}" if sc_count > 0 else "—", sc_count)

            self.tbl_functions.setItem(row, 0, it_name)
            self.tbl_functions.setItem(row, 1, it_calls)
            self.tbl_functions.setItem(row, 2, it_tot)
            self.tbl_functions.setItem(row, 3, it_pct_tot)
            self.tbl_functions.setItem(row, 4, it_self)
            self.tbl_functions.setItem(row, 5, it_pct_self)
            self.tbl_functions.setItem(row, 6, it_avg)
            self.tbl_functions.setItem(row, 7, it_sc)

        self.tbl_functions.setSortingEnabled(True)
        self.tbl_functions.horizontalScrollBar().setValue(0)

    def _filter_functions_table(self, query: str):
        query = query.strip().lower()
        visible = 0
        total = self.tbl_functions.rowCount()
        for row in range(total):
            item = self.tbl_functions.item(row, 0)
            if not item:
                continue
            is_match = query in item.text().lower()
            self.tbl_functions.setRowHidden(row, not is_match)
            if is_match:
                visible += 1
        if query:
            self.lbl_func_count.setText(f"Showing {visible} of {total} functions")
        else:
            self.lbl_func_count.setText(f"{total} functions recorded")

    def _on_function_selected(self):
        items = self.tbl_functions.selectedItems()
        if not items:
            return
        row = items[0].row()
        item = self.tbl_functions.item(row, 0)
        if not item:
            return
        fn_data = item.data(Qt.UserRole)
        if not fn_data:
            return

        self.lbl_detail_title.setText(fn_data.get("name", "Function Details"))
        sc = fn_data.get("syscalls", {})
        sc_html = (
            "".join(f"<li><b>{k}:</b> {v} calls</li>" for k, v in sc.items())
            if sc
            else "<li>None directly attributed</li>"
        )
        estimated = self.current_report.get("profile", {}).get("timing_basis") in {
            "normalized_profile_estimate",
            "instrumented_profile_run",
        }
        prefix = "Estimated " if estimated else ""

        html = f"""
        <b>Address:</b> <span style="font-family: monospace;">{fn_data.get('address', 'N/A')}</span><br>
        <b>Call Count:</b> {fn_data.get('calls', 1):,}<br>
        <b>{prefix}Inclusive Time:</b> {format_duration(fn_data.get('total_ns', 0))}<br>
        <b>{prefix}Exclusive Self Time:</b> {format_duration(fn_data.get('self_ns', 0))}<br>
        <b>{prefix}Min Call Duration:</b> {format_duration(fn_data.get('min_ns', 0))}<br>
        <b>{prefix}Max Call Duration:</b> {format_duration(fn_data.get('max_ns', 0))}<br><br>
        <b>Attributed System Calls:</b>
        <ul>{sc_html}</ul>
        """
        self.txt_detail_body.setHtml(html)

    def _populate_syscalls_table(self, syscalls: dict):
        summary = syscalls.get("summary", {})
        by_func = syscalls.get("by_function", {})
        if syscalls.get("error"):
            self.tbl_syscalls.setSortingEnabled(False)
            self.tbl_syscalls.clearContents()
            self.tbl_syscalls.clearSpans()
            self.tbl_syscalls.setRowCount(1)
            self.tbl_syscalls.setSpan(0, 0, 1, 4)
            self.tbl_syscalls.setItem(
                0, 0, QtWidgets.QTableWidgetItem(f"Unavailable: {syscalls['error']}")
            )
            return

        tot_calls = max(1, syscalls.get("total_syscalls", 0))

        self.tbl_syscalls.setSortingEnabled(False)
        self.tbl_syscalls.clearContents()
        self.tbl_syscalls.clearSpans()
        self.tbl_syscalls.setRowCount(len(summary))
        for row, (sc_name, count) in enumerate(summary.items()):
            attr_funcs = [
                fname for fname, sc_map in by_func.items() if sc_name in sc_map
            ]
            attr_str = (
                ", ".join(attr_funcs) if attr_funcs else "Host Runtime / Dynamic Linker"
            )
            pct = (count / tot_calls) * 100.0

            self.tbl_syscalls.setItem(row, 0, QtWidgets.QTableWidgetItem(sc_name))
            self.tbl_syscalls.setItem(
                row, 1, NumericTableWidgetItem(f"{count:,}", count)  # Bug 10: numeric sort
            )
            self.tbl_syscalls.setItem(
                row, 2, QtWidgets.QTableWidgetItem(attr_str)
            )
            self.tbl_syscalls.setItem(
                row, 3, NumericTableWidgetItem(f"{pct:.1f}%", pct)  # Bug 10: numeric sort
            )
        self.tbl_syscalls.setSortingEnabled(True)

    # Flame Graph Event Handlers
    def _on_reset_flame_zoom(self):
        self.flame_widget.reset_zoom()
        self.lbl_breadcrumbs.setText("Path: Root")

    def _on_flame_search_changed(self, query: str):
        self.flame_widget.set_search_filter(query)

    def _on_flame_dir_changed(self, idx: int):
        self.flame_widget.set_top_down(idx == 0)

    def _on_flame_color_changed(self, idx: int):
        modes = ["heat", "hash", "depth"]
        self.flame_widget.set_color_mode(modes[idx])

    def _on_flame_node_clicked_from_widget(self, node_info: dict):
        crumbs = self.flame_widget.get_node_path(self.flame_widget.selected_node)
        crumb_str = " › ".join(c[0] for c in crumbs) if crumbs else "Root"
        self.lbl_breadcrumbs.setText(f"Path: {crumb_str}")

        # Synchronize selection with Functions table & details
        target_name = node_info.get("name", "")
        for row in range(self.tbl_functions.rowCount()):
            item = self.tbl_functions.item(row, 0)
            if item and item.text() == target_name:
                self.tbl_functions.selectRow(row)
                break

    def _on_flame_zoom_changed(self, node: Any):
        crumbs = self.flame_widget.get_breadcrumbs()
        crumb_str = " › ".join(c[0] for c in crumbs) if crumbs else "Root"
        self.lbl_breadcrumbs.setText(f"Path: {crumb_str}")

    def _on_function_double_clicked(self, index: QtCore.QModelIndex):
        row = index.row()
        item = self.tbl_functions.item(row, 0)
        if not item:
            return
        fn_name = item.text()
        # Switch to Flame Graph tab (index 2)
        self.tabs.setCurrentIndex(2)
        node = self.flame_widget.find_node_by_name(fn_name)
        if node:
            self.flame_widget.selected_node = node
            self.flame_widget.zoom_to_node(node)
            self._on_flame_zoom_changed(node)

    # Exports
    def _on_export_json(self):
        if not self.current_report:
            QtWidgets.QMessageBox.warning(self, "No Data", "Run a benchmark first before exporting.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export JSON Report", str(self.project_path / "benchmark_report.json"), "JSON Files (*.json)"
        )
        if path:
            export_json(self.current_report, path)
            self.lbl_status.setText(f"Exported: {Path(path).name}")  # Bug 15: no blocking modal

    def _on_export_csv(self):
        if not self.current_report:
            QtWidgets.QMessageBox.warning(self, "No Data", "Run a benchmark first before exporting.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export CSV Breakdown", str(self.project_path / "functions_breakdown.csv"), "CSV Files (*.csv)"
        )
        if path:
            export_csv(self.current_report, path)
            self.lbl_status.setText(f"Exported: {Path(path).name}")  # Bug 15: no blocking modal

    def _on_export_html(self):
        if not self.current_report:
            QtWidgets.QMessageBox.warning(self, "No Data", "Run a benchmark first before exporting.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Interactive HTML Report", str(self.project_path / "benchmark_report.html"), "HTML Files (*.html)"
        )
        if path:
            export_html_report(self.current_report, path)
            self.lbl_status.setText(f"Exported: {Path(path).name}")  # Bug 15: no blocking modal


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    proj_dir = sys.argv[1] if len(sys.argv) > 1 else None
    win = BenchmarkStudioWindow(proj_dir)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
