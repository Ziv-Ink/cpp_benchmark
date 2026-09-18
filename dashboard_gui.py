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
/* Modern Dark Dashboard Theme */
QMainWindow, QDialog, QWidget {
    background-color: #12151b;
    color: #f1f5f9;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    font-size: 13px;
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
    padding: 8px 16px;
    min-width: 130px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
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
    padding: 6px 10px;
    color: #f8fafc;
    selection-background-color: #3b82f6;
}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #3b82f6;
    background-color: #222938;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QComboBox QAbstractItemView {
    background-color: #1e2430;
    border: 1px solid #333d52;
    selection-background-color: #2563eb;
    color: #f8fafc;
    padding: 4px;
}

QPushButton {
    background-color: #242c3b;
    border: 1px solid #38445a;
    border-radius: 6px;
    padding: 7px 16px;
    color: #f1f5f9;
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
    padding: 8px 24px;
    border-radius: 6px;
}

QPushButton#btn_run:hover {
    background-color: #3b82f6;
    border-color: #60a5fa;
}

QPushButton#btn_run:pressed {
    background-color: #1d4ed8;
}

QPushButton#btn_stop {
    background-color: #dc2626;
    border: 1px solid #ef4444;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    padding: 8px 24px;
}

QCheckBox {
    color: #cbd5e1;
    font-weight: 500;
    spacing: 6px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
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
}

QHeaderView::section {
    background-color: #212836;
    color: #cbd5e1;
    padding: 8px 10px;
    border: none;
    border-bottom: 2px solid #283040;
    border-right: 1px solid #202633;
    font-weight: 700;
    font-size: 12px;
}

QHeaderView::section:hover {
    background-color: #242b3a;
    color: #f1f5f9;
}

QProgressBar {
    border: 1px solid #283040;
    border-radius: 4px;
    background-color: #1a1e28;
    height: 8px;
    text-align: center;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #60a5fa);
    border-radius: 3px;
}

QScrollBar:vertical {
    border: none;
    background: #12151b;
    width: 10px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #2b3344;
    min-height: 24px;
    border-radius: 5px;
}

QScrollBar::handle:vertical:hover {
    background: #3f4a62;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
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
        self.setMinimumHeight(160)
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
            painter.drawText(
                rect,
                Qt.AlignCenter,
                "No sample data recorded yet. Run a benchmark to view iteration timeline.",
            )
            painter.end()
            return

        values = [s.get("elapsed_ns", 0) for s in self.samples]
        if not values or max(values) <= 0:
            painter.end()
            return

        max_v = max(values) * 1.15
        min_v = 0
        w = float(rect.width())
        h = float(rect.height())
        pad_left = 60
        pad_bottom = 30
        pad_top = 20
        pad_right = 20

        plot_w = w - pad_left - pad_right
        plot_h = h - pad_top - pad_bottom

        # Grid lines
        painter.setPen(QtGui.QColor("#242b38"))
        for step in range(4):
            y = pad_top + (plot_h / 3) * step
            painter.drawLine(int(pad_left), int(y), int(w - pad_right), int(y))
            val = max_v - (max_v / 3) * step
            painter.setPen(QtGui.QColor("#64748b"))
            painter.setFont(QtGui.QFont("Segoe UI", 8))
            painter.drawText(
                5,
                int(y + 4),
                pad_left - 10,
                15,
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
            val = s.get("elapsed_ns", 0)
            x = pad_left + i * step_x
            y = pad_top + plot_h - (val / max_v) * plot_h
            points.append((x, y, s.get("phase") == "warmup", val))

        # Draw line
        if len(points) > 1:
            painter.setPen(QtGui.QPen(QtGui.QColor("#3b82f6"), 1.8))
            for i in range(len(points) - 1):
                painter.drawLine(
                    QtCore.QPointF(points[i][0], points[i][1]),
                    QtCore.QPointF(points[i + 1][0], points[i + 1][1]),
                )

        # Draw sample points
        for x, y, is_warmup, val in points:
            if is_warmup:
                painter.setBrush(QtGui.QBrush(QtGui.QColor("#f59e0b")))
                painter.setPen(QtGui.QColor("#d97706"))
            else:
                painter.setBrush(QtGui.QBrush(QtGui.QColor("#60a5fa")))
                painter.setPen(QtGui.QColor("#2563eb"))
            painter.drawEllipse(QtCore.QPointF(x, y), 3.5, 3.5)

        # Draw axis label
        painter.setPen(QtGui.QColor("#94a3b8"))
        painter.setFont(QtGui.QFont("Segoe UI", 9))
        painter.drawText(
            int(pad_left),
            int(h - 8),
            int(plot_w),
            18,
            Qt.AlignCenter,
            f"Iterations (Total: {n} | Amber = Warmup, Blue = Measured)",
        )
        painter.end()


class MetricCard(QtWidgets.QFrame):
    """Clean stat card with prominent number and subtle sub-label."""

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
            f"""
            MetricCard {{
                background-color: #191d26;
                border: 1px solid #283040;
                border-radius: 8px;
                padding: 12px;
            }}
        """
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self.lbl_title = QtWidgets.QLabel(title.upper())
        self.lbl_title.setStyleSheet(
            "color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;"
        )

        self.lbl_value = QtWidgets.QLabel(value)
        self.lbl_value.setStyleSheet(
            f"color: {accent_color}; font-size: 22px; font-weight: 700; margin: 2px 0;"
        )

        self.lbl_subtitle = QtWidgets.QLabel(subtitle)
        self.lbl_subtitle.setStyleSheet("color: #64748b; font-size: 11px;")

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_value)
        layout.addWidget(self.lbl_subtitle)

    def set_data(self, value: str, subtitle: str = ""):
        self.lbl_value.setText(value)
        if subtitle:
            self.lbl_subtitle.setText(subtitle)


class BenchmarkStudioWindow(QtWidgets.QMainWindow):
    """Main dashboard window."""

    def __init__(
        self,
        initial_project: Optional[str] = "/home/ziv/Desktop/supernote/read_file_on_device_cpp",
    ):
        super().__init__()
        self.setWindowTitle("Supernote C++ Benchmark & Profiling Studio")
        self.resize(1240, 880)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(DARK_QSS)

        self.project_path: Path = Path(initial_project).resolve()
        self.worker: Optional[BenchmarkWorker] = None
        self.current_report: Dict[str, Any] = {}

        self._build_ui()
        self._rescan_project()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # 1. Top Bar (Target, Device, Run Button)
        main_layout.addWidget(self._create_top_bar())

        # 2. Metric Cards Row
        main_layout.addWidget(self._create_metric_cards_row())

        # 3. Stage Pipeline Progress Bar
        main_layout.addWidget(self._create_stage_pipeline_widget())

        # 4. Central Tabs
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._create_settings_tab(), "Run Settings")
        self.tabs.addTab(self._create_functions_tab(), "Functions Breakdown")
        self.tabs.addTab(self._create_flamegraph_tab(), "Flame Graph")
        self.tabs.addTab(self._create_syscalls_tab(), "Syscalls")
        self.tabs.addTab(self._create_comparison_tab(), "Comparison")
        self.tabs.addTab(self._create_timeline_tab(), "Sample Timeline")
        self.tabs.addTab(self._create_bottom_drawer(), "Terminal Log")
        self.tabs.addTab(self._create_export_tab(), "Raw Data & Export")
        main_layout.addWidget(self.tabs, stretch=1)

        # 5. Status Bar at the very bottom
        status_bar = QtWidgets.QHBoxLayout()
        self.lbl_status = QtWidgets.QLabel("Status: Ready")
        self.lbl_status.setStyleSheet("color: #38bdf8; font-weight: 600;")
        status_bar.addWidget(self.lbl_status)
        
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(300)
        status_bar.addWidget(self.progress_bar)
        
        main_layout.addLayout(status_bar)

    def _create_top_bar(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setStyleSheet(
            "background-color: #181c25; border: 1px solid #283040; border-radius: 8px;"
        )
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        layout.addWidget(QtWidgets.QLabel("Target:"))
        self.cmb_target = QtWidgets.QComboBox()
        self.cmb_target.setMinimumWidth(180)
        layout.addWidget(self.cmb_target)

        layout.addWidget(QtWidgets.QLabel("Execution:"))
        self.cmb_mode = QtWidgets.QComboBox()
        self.cmb_mode.addItems(["Local Host", "Android (ADB)"])
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.cmb_mode)

        layout.addWidget(QtWidgets.QLabel("Device:"))
        self.cmb_device = QtWidgets.QComboBox()
        self.cmb_device.setEnabled(False)
        self.cmb_device.setMinimumWidth(120)
        layout.addWidget(self.cmb_device)

        layout.addStretch(1)

        layout.addWidget(QtWidgets.QLabel("Mode:"))
        self.cmb_run_mode = QtWidgets.QComboBox()
        self.cmb_run_mode.addItems(["Combined (Default)", "Run for Time", "Run for Profile"])
        self.cmb_run_mode.setMinimumWidth(180)
        self.cmb_run_mode.setStyleSheet("background-color: #1e2430; border-radius: 4px; padding: 4px;")
        layout.addWidget(self.cmb_run_mode)

        self.btn_run = QtWidgets.QPushButton("▶ Run Benchmark")
        self.btn_run.setObjectName("btn_run")
        self.btn_run.setMinimumHeight(32)
        self.btn_run.clicked.connect(self._on_toggle_run)
        layout.addWidget(self.btn_run)

        return card

    def _create_settings_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Row 1: Project Path
        r1 = QtWidgets.QHBoxLayout()
        r1.addWidget(QtWidgets.QLabel("Project Path:"))
        self.txt_project = QtWidgets.QLineEdit(str(self.project_path))
        self.btn_browse = QtWidgets.QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._on_browse_project)
        r1.addWidget(self.txt_project, stretch=1)
        r1.addWidget(self.btn_browse)
        layout.addLayout(r1)

        # Row 2: Run settings
        r2 = QtWidgets.QHBoxLayout()
        self.chk_adaptive = QtWidgets.QCheckBox("Adaptive Mode (Run until stable)")
        self.chk_adaptive.setChecked(True)
        self.chk_adaptive.toggled.connect(self._on_adaptive_toggled)
        r2.addWidget(self.chk_adaptive)

        self.lbl_runs = QtWidgets.QLabel("Min Runs:")
        r2.addWidget(self.lbl_runs)
        self.spin_runs = QtWidgets.QSpinBox()
        self.spin_runs.setRange(2, 500)
        self.spin_runs.setValue(20)
        r2.addWidget(self.spin_runs)

        r2.addWidget(QtWidgets.QLabel("Warmup Runs:"))
        self.spin_warmup = QtWidgets.QSpinBox()
        self.spin_warmup.setRange(0, 20)
        self.spin_warmup.setValue(2)
        r2.addWidget(self.spin_warmup)
        
        r2.addStretch(1)
        layout.addLayout(r2)

        # Row 3: Profiling & Compare
        r3 = QtWidgets.QHBoxLayout()
        self.chk_profile = QtWidgets.QCheckBox("Enable Function Profiling (-finstrument-functions)")
        self.chk_profile.setChecked(False)
        r3.addWidget(self.chk_profile)

        self.chk_syscalls = QtWidgets.QCheckBox("Trace Syscalls (strace)")
        self.chk_syscalls.setChecked(True)
        r3.addWidget(self.chk_syscalls)

        r3.addWidget(QtWidgets.QLabel("Compare Target:"))
        self.cmb_compare = QtWidgets.QComboBox()
        self.cmb_compare.addItem("(None)")
        self.cmb_compare.setMinimumWidth(180)
        r3.addWidget(self.cmb_compare)

        r3.addStretch(1)
        layout.addLayout(r3)

        layout.addStretch(1)
        return tab

    def _create_metric_cards_row(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.card_median = MetricCard(
            "Median Latency", "—", "Mean: —", accent_color="#60a5fa"
        )
        self.card_precision = MetricCard(
            "Stability & Error", "—", "Drift: —", accent_color="#34d399"
        )
        self.card_runs = MetricCard(
            "Samples", "—", "Warmups: —", accent_color="#a78bfa"
        )
        self.card_bottleneck = MetricCard(
            "Bottleneck Function", "—", "Top self time", accent_color="#fb923c"
        )
        self.card_syscalls = MetricCard(
            "Total Syscalls", "—", "Attributed", accent_color="#38bdf8"
        )
        self.card_compare = MetricCard(
            "Comparison", "—", "Speedup delta", accent_color="#f43f5e"
        )

        layout.addWidget(self.card_median)
        layout.addWidget(self.card_precision)
        layout.addWidget(self.card_runs)
        layout.addWidget(self.card_bottleneck)
        layout.addWidget(self.card_syscalls)
        layout.addWidget(self.card_compare)
        self.card_compare.setVisible(False)  # shown only when comparison data exists

        return w

    def _create_stage_pipeline_widget(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setStyleSheet(
            "background-color: #161922; border: 1px solid #232a38; border-radius: 6px;"
        )
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(10, 6, 10, 6)

        self.lbl_stage_info = QtWidgets.QLabel("Pipeline: Idle")
        self.lbl_stage_info.setStyleSheet("color: #94a3b8; font-weight: 600;")
        layout.addWidget(self.lbl_stage_info)

        layout.addStretch()

        self.stage_badges: Dict[str, QtWidgets.QLabel] = {}
        stages = [
            ("setup", "1. Configure"),
            ("build", "2. Build"),
            ("measure", "3. Measure"),
            ("profile", "4. Profile & Syscalls"),
        ]
        for key, name in stages:
            badge = QtWidgets.QLabel(name)
            badge.setStyleSheet(
                "background-color: #1e2430; color: #64748b; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600;"
            )
            self.stage_badges[key] = badge
            layout.addWidget(badge)

        return card

    # Tab 1: Functions Breakdown Table
    def _create_functions_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Search bar
        top_bar = QtWidgets.QHBoxLayout()
        self.txt_func_search = QtWidgets.QLineEdit()
        self.txt_func_search.setPlaceholderText("🔍 Filter functions by name...")
        self.txt_func_search.textChanged.connect(self._filter_functions_table)
        top_bar.addWidget(self.txt_func_search, stretch=1)

        self.lbl_func_count = QtWidgets.QLabel("0 functions")
        self.lbl_func_count.setStyleSheet("color: #94a3b8;")
        top_bar.addWidget(self.lbl_func_count)
        layout.addLayout(top_bar)

        # Splitter: Table on left, Detail panel on right
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
        self.tbl_functions.itemSelectionChanged.connect(
            self._on_function_selected
        )
        self.tbl_functions.doubleClicked.connect(
            self._on_function_double_clicked
        )
        self.tbl_functions.setSortingEnabled(True)
        splitter.addWidget(self.tbl_functions)

        # Function detail drawer
        detail_card = QtWidgets.QFrame()
        detail_card.setStyleSheet(
            "background-color: #161922; border: 1px solid #283040; border-radius: 6px; padding: 10px;"
        )
        detail_layout = QtWidgets.QVBoxLayout(detail_card)
        detail_layout.setContentsMargins(8, 8, 8, 8)

        self.lbl_detail_title = QtWidgets.QLabel("Select a Function")
        self.lbl_detail_title.setStyleSheet(
            "color: #60a5fa; font-size: 14px; font-weight: 700;"
        )
        detail_layout.addWidget(self.lbl_detail_title)

        self.txt_detail_body = QtWidgets.QTextBrowser()
        self.txt_detail_body.setStyleSheet(
            "background-color: #12151b; border: 1px solid #232a38; border-radius: 4px; padding: 6px; color: #cbd5e1;"
        )
        detail_layout.addWidget(self.txt_detail_body)

        splitter.addWidget(detail_card)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)
        return w

    # Tab 2: Flame Graph Tab
    def _create_flamegraph_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Controls bar
        ctrl = QtWidgets.QHBoxLayout()
        self.btn_reset_zoom = QtWidgets.QPushButton("↺ Reset Zoom")
        self.btn_reset_zoom.clicked.connect(self._on_reset_flame_zoom)
        ctrl.addWidget(self.btn_reset_zoom)

        self.txt_flame_search = QtWidgets.QLineEdit()
        self.txt_flame_search.setPlaceholderText("🔍 Highlight function...")
        self.txt_flame_search.textChanged.connect(self._on_flame_search_changed)
        ctrl.addWidget(self.txt_flame_search)

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
            "background-color: #191d26; color: #94a3b8; padding: 6px 12px; border-radius: 4px; font-weight: 500;"
        )
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

    # Tab 3: Syscalls Tab
    def _create_syscalls_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

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
        layout.addWidget(self.tbl_syscalls)
        return w

    # Tab 4: Comparison Tab
    def _create_comparison_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

        self.tbl_comparison = QtWidgets.QTableWidget()
        self.tbl_comparison.setColumnCount(5)
        self.tbl_comparison.setHorizontalHeaderLabels(
            ["Metric / Function", "Target A", "Target B", "Delta", "Speedup"]
        )
        self.tbl_comparison.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch
        )
        for c in range(1, 5):
            self.tbl_comparison.horizontalHeader().setSectionResizeMode(
                c, QtWidgets.QHeaderView.ResizeToContents
            )
        layout.addWidget(self.tbl_comparison)
        return w

    # Tab 5: Sample Timeline Tab
    def _create_timeline_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

        self.timeline_chart = TimelineChartWidget()
        layout.addWidget(self.timeline_chart, stretch=1)

        # Stats summary box under timeline
        self.txt_timeline_stats = QtWidgets.QTextBrowser()
        self.txt_timeline_stats.setMaximumHeight(140)
        self.txt_timeline_stats.setStyleSheet(
            "background-color: #161922; border: 1px solid #283040; border-radius: 6px; padding: 8px; color: #cbd5e1;"
        )
        layout.addWidget(self.txt_timeline_stats)
        return w

    # Tab 6: Raw Data & Export Tab
    def _create_export_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_export_json = QtWidgets.QPushButton("📄 Export JSON")
        self.btn_export_json.clicked.connect(self._on_export_json)
        self.btn_export_csv = QtWidgets.QPushButton("📊 Export CSV")
        self.btn_export_csv.clicked.connect(self._on_export_csv)
        self.btn_export_html = QtWidgets.QPushButton("🌐 Export HTML Report")
        self.btn_export_html.clicked.connect(self._on_export_html)

        btn_row.addWidget(self.btn_export_json)
        btn_row.addWidget(self.btn_export_csv)
        btn_row.addWidget(self.btn_export_html)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.txt_raw_json = QtWidgets.QTextBrowser()
        self.txt_raw_json.setStyleSheet(
            "background-color: #10131a; border: 1px solid #232a38; border-radius: 6px; font-family: monospace; font-size: 12px; color: #93c5fd;"
        )
        layout.addWidget(self.txt_raw_json, stretch=1)
        return w

    def _create_bottom_drawer(self) -> QtWidgets.QWidget:
        self.txt_log = QtWidgets.QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(
            "background-color: #0d1016; border: 1px solid #1f2533; border-radius: 4px; font-family: monospace; font-size: 11px; color: #a5b4fc; padding: 10px;"
        )
        return self.txt_log

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

    def _on_mode_changed(self, idx: int):
        is_adb = idx == 1
        self.cmb_device.setEnabled(is_adb)

    def _on_adaptive_toggled(self, checked: bool):
        self.lbl_runs.setText("Min Runs:" if checked else "Exact Runs:")

    def _on_toggle_run(self):
        if self.worker and self.worker.isRunning():
            self.btn_run.setEnabled(False)
            self.lbl_status.setText("Cancelling...")
            self.worker.cancel()
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
            profile_functions=self.chk_profile.isChecked(),
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
        # Set red style directly — objectName-based QSS doesn't re-resolve after creation
        self.btn_run.setStyleSheet(
            "QPushButton { background-color: #dc2626; border: 1px solid #ef4444; "
            "color: #ffffff; font-size: 14px; font-weight: 700; "
            "padding: 8px 24px; border-radius: 6px; }"
            "QPushButton:hover { background-color: #ef4444; }"
        )

        self.txt_log.clear()
        self.progress_bar.setValue(0)
        self.timeline_chart.set_samples([])
        self._reset_stage_badges()

        self.worker = BenchmarkWorker(cfg)
        self.worker.sig_log.connect(self._on_worker_log)
        self.worker.sig_status.connect(self._on_worker_status)
        self.worker.sig_stage.connect(self._on_worker_stage)
        self.worker.sig_sample.connect(self._on_worker_sample)
        self.worker.sig_stability.connect(self._on_worker_stability)
        self.worker.sig_finished.connect(self._on_worker_finished)
        self.worker.sig_error.connect(self._on_worker_error)
        self.worker.sig_cancelled.connect(self._on_worker_cancelled)
        self.tabs.setCurrentIndex(6)  # Switch to Terminal Log
        self.worker.start()

    def _reset_stage_badges(self):
        for badge in self.stage_badges.values():
            badge.setStyleSheet(
                "background-color: #1e2430; color: #64748b; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600;"
            )
        self.lbl_stage_info.setText("Pipeline: Idle")

    def _mark_stages_complete(self):
        for badge in self.stage_badges.values():
            badge.setStyleSheet(
                "background-color: #10b981; color: #ffffff; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600;"
            )
        self.lbl_stage_info.setText("Pipeline: Complete")

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
        if current in (1, 5):
            for badge in self.stage_badges.values():
                badge.setStyleSheet("background-color: #1e2430; color: #64748b; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600;")
        
        self.lbl_stage_info.setText(f"Pipeline: [{current}/{total}] {name}")
        pct = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)

        normalized_current = ((current - 1) % 4) + 1
        keys = ["setup", "build", "measure", "profile"]
        if 1 <= normalized_current <= len(keys):
            active_key = keys[normalized_current - 1]
            for k, badge in self.stage_badges.items():
                if k == active_key:
                    badge.setStyleSheet(
                        "background-color: #2563eb; color: #ffffff; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 700;"
                    )
                elif keys.index(k) < keys.index(active_key):
                    badge.setStyleSheet(
                        "background-color: #10b981; color: #ffffff; padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600;"
                    )

    @pyqtSlot(dict)
    def _on_worker_sample(self, s: dict):
        # Update timeline in real time
        curr = self.timeline_chart.samples.copy()
        curr.append(s)
        self.timeline_chart.set_samples(curr, s.get("target", ""))

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
        self.btn_run.setStyleSheet("")  # clears override; parent #btn_run QSS takes effect
        self.btn_run.setEnabled(True)
        self._mark_stages_complete()

        self.current_report = report
        self._populate_dashboard(report)
        self.tabs.setCurrentIndex(1)  # Switch back to Functions Breakdown

    @pyqtSlot(str)
    def _on_worker_error(self, err: str):
        self.btn_run.setText("▶ Run Benchmark")
        self.btn_run.setStyleSheet("")
        self.btn_run.setEnabled(True)
        self._reset_stage_badges()
        QtWidgets.QMessageBox.critical(self, "Benchmark Failed", f"Execution failed:\n\n{err}")

    @pyqtSlot()
    def _on_worker_cancelled(self):
        self.btn_run.setText("▶ Run Benchmark")
        self.btn_run.setStyleSheet("")
        self.btn_run.setEnabled(True)
        self._reset_stage_badges()
        self.lbl_stage_info.setText("Pipeline: Cancelled")
        self.lbl_status.setText("Status: Cancelled")

    def _populate_dashboard(self, report: dict):
        target = report.get("target", "")
        summary = report.get("summary", {})
        timings = report.get("timings", {})
        profile = report.get("profile", {})
        functions = profile.get("functions", [])
        syscalls = report.get("syscalls", {})

        # 1. Update Cards
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
            f"{report.get('runs', 0)} runs",
            f"{report.get('warmup_runs', 0)} warmups discarded",
        )

        # Bottleneck function
        if functions:
            top_fn = max(functions, key=lambda item: item.get("self_ns", 0))
            tot_time = max(
                1, sum(item.get("self_ns", 0) for item in functions)
            )
            pct = (top_fn.get("self_ns", 0) / tot_time) * 100
            self.card_bottleneck.set_data(
                f"{pct:.1f}% self", top_fn.get("name", "")[:28]
            )
        else:
            self.card_bottleneck.set_data("—", "Scope timing only")

        # Syscalls
        tot_sc = syscalls.get("total_syscalls", 0)
        top_sc = list(syscalls.get("summary", {}).keys())[:3]
        self.card_syscalls.set_data(
            f"{tot_sc:,} calls", ", ".join(top_sc) if top_sc else "0 recorded"
        )

        # Comparison
        comp = report.get("comparison")
        if comp:
            speedup = comp.get("speedup", 1.0)
            self.card_compare.set_data(
                f"{speedup:.2f}x", f"Fastest: {comp.get('fastest')}"
            )
            self.card_compare.setVisible(True)
        else:
            self.card_compare.setVisible(False)

        # 2. Populate Functions Table
        self._populate_functions_table(functions)

        # 3. Populate Flame Graph
        tree = profile.get("tree", [])
        sc_map = syscalls.get("by_function", {})
        self.flame_widget.set_tree_data(tree, sc_map)
        self.lbl_breadcrumbs.setText("Path: Root")

        # 4. Populate Syscalls Tab
        self._populate_syscalls_table(syscalls)

        # 5. Populate Comparison Tab
        self._populate_comparison_tab(report)

        # 6. Populate Timeline Tab
        samples = report.get("targets", {}).get(target, {}).get("samples", [])
        warmups = report.get("targets", {}).get(target, {}).get("warmups", [])
        all_samples = [
            {**s, "phase": "warmup"} for s in warmups
        ] + [{**s, "phase": "sample"} for s in samples]
        self.timeline_chart.set_samples(all_samples, target)

        if summary:
            self.txt_timeline_stats.setHtml(
                f"""
                <b>Sample Statistics:</b><br>
                • <b>Median:</b> {format_duration(summary.get('median_ns', 0))} | 
                <b>Mean:</b> {format_duration(summary.get('mean_ns', 0))} | 
                <b>Min:</b> {format_duration(summary.get('min_ns', 0))} | 
                <b>Max:</b> {format_duration(summary.get('max_ns', 0))}<br>
                • <b>Std Dev:</b> {format_duration(summary.get('stddev_ns', 0))} | 
                <b>Variability:</b> {summary.get('variability_percent', 0):.2f}% | 
                <b>RSE:</b> {summary.get('relative_standard_error_percent', 0):.2f}%
            """
            )

        # 7. Raw JSON View
        self.txt_raw_json.setText(json.dumps(report, indent=2))

    def _populate_functions_table(self, functions: List[Dict[str, Any]]):
        self.tbl_functions.setSortingEnabled(False)
        self.tbl_functions.setRowCount(len(functions))
        tot_time = max(
            1, sum(item.get("self_ns", 0) for item in functions)
        )
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

            pct_tot = (tot_ns / tot_time) * 100.0
            pct_self = (self_ns / tot_time) * 100.0

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

        html = f"""
        <b>Address:</b> <span style="font-family: monospace;">{fn_data.get('address', 'N/A')}</span><br>
        <b>Call Count:</b> {fn_data.get('calls', 1):,}<br>
        <b>Inclusive Time:</b> {format_duration(fn_data.get('total_ns', 0))}<br>
        <b>Exclusive Self Time:</b> {format_duration(fn_data.get('self_ns', 0))}<br>
        <b>Min Call Duration:</b> {format_duration(fn_data.get('min_ns', 0))}<br>
        <b>Max Call Duration:</b> {format_duration(fn_data.get('max_ns', 0))}<br><br>
        <b>Attributed System Calls:</b>
        <ul>{sc_html}</ul>
        """
        self.txt_detail_body.setHtml(html)

    def _populate_syscalls_table(self, syscalls: dict):
        summary = syscalls.get("summary", {})
        by_func = syscalls.get("by_function", {})
        tot_calls = max(1, syscalls.get("total_syscalls", 0))

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
                row, 1, QtWidgets.QTableWidgetItem(f"{count:,}")
            )
            self.tbl_syscalls.setItem(
                row, 2, QtWidgets.QTableWidgetItem(attr_str)
            )
            self.tbl_syscalls.setItem(
                row, 3, QtWidgets.QTableWidgetItem(f"{pct:.1f}%")
            )

    def _populate_comparison_tab(self, report: dict):
        comp = report.get("comparison")
        if not comp:
            self.tbl_comparison.setRowCount(1)
            self.tbl_comparison.setItem(
                0,
                0,
                QtWidgets.QTableWidgetItem(
                    "No comparison target specified. Select a 'Compare' target and re-run."
                ),
            )
            return

        t_a = comp.get("target_a")
        t_b = comp.get("target_b")
        m_a = comp.get("median_a_ns", 0)
        m_b = comp.get("median_b_ns", 0)
        speedup = comp.get("speedup", 1.0)
        delta = m_b - m_a

        self.tbl_comparison.setRowCount(1)
        self.tbl_comparison.setItem(
            0, 0, QtWidgets.QTableWidgetItem("Median Latency")
        )
        self.tbl_comparison.setItem(
            0, 1, QtWidgets.QTableWidgetItem(format_duration(m_a))
        )
        self.tbl_comparison.setItem(
            0, 2, QtWidgets.QTableWidgetItem(format_duration(m_b))
        )
        self.tbl_comparison.setItem(
            0, 3, QtWidgets.QTableWidgetItem(format_duration(abs(delta)))
        )
        self.tbl_comparison.setItem(
            0,
            4,
            QtWidgets.QTableWidgetItem(
                f"{speedup:.2f}x ({comp.get('fastest')} faster)"
            ),
        )

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
        crumbs = self.flame_widget.get_breadcrumbs()
        crumb_str = " › ".join(c[0] for c in crumbs)
        self.lbl_breadcrumbs.setText(f"Path: {crumb_str}")

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
        # Switch to Flame Graph tab (index 1)
        self.tabs.setCurrentIndex(1)
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
            QtWidgets.QMessageBox.information(self, "Export Complete", f"Report saved to:\n{path}")

    def _on_export_csv(self):
        if not self.current_report:
            QtWidgets.QMessageBox.warning(self, "No Data", "Run a benchmark first before exporting.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export CSV Breakdown", str(self.project_path / "functions_breakdown.csv"), "CSV Files (*.csv)"
        )
        if path:
            export_csv(self.current_report, path)
            QtWidgets.QMessageBox.information(self, "Export Complete", f"CSV breakdown saved to:\n{path}")

    def _on_export_html(self):
        if not self.current_report:
            QtWidgets.QMessageBox.warning(self, "No Data", "Run a benchmark first before exporting.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Interactive HTML Report", str(self.project_path / "benchmark_report.html"), "HTML Files (*.html)"
        )
        if path:
            export_html_report(self.current_report, path)
            QtWidgets.QMessageBox.information(self, "Export Complete", f"Interactive HTML report saved to:\n{path}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    proj_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/ziv/Desktop/supernote/read_file_on_device_cpp"
    win = BenchmarkStudioWindow(proj_dir)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
