#!/usr/bin/env python3
"""Interactive Flame Graph widget in PyQt5 using QPainter.

Provides a clean, hardware-accelerated, zoomable call tree flame graph
with breadcrumb navigation, search filtering, and rich hover tooltips.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Any, Tuple
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal


def format_duration(ns: int | float) -> str:
    """Formats nanoseconds to human readable string."""
    if ns is None or math.isnan(ns) or ns < 0:
        return "0 ns"
    if ns < 1_000:
        return f"{ns:.0f} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    return f"{ns / 1_000_000_000:.3f} s"


class FlameNode:
    """A node in the flame graph tree."""

    def __init__(
        self,
        node_id: int,
        name: str,
        total_ns: int,
        self_ns: int,
        calls: int = 1,
        syscalls: Optional[Dict[str, int]] = None,
        address: str = "",
        category: str = "user",
    ):
        self.id = node_id
        self.name = name
        self.total_ns = total_ns
        self.self_ns = self_ns
        self.calls = calls
        self.syscalls = syscalls or {}
        self.address = address
        self.category = category
        self.children: List[FlameNode] = []
        self.parent: Optional[FlameNode] = None
        self.depth: int = 0

        # Layout properties (computed on layout)
        self.x_ratio: float = 0.0  # [0.0, 1.0] relative to current view root
        self.w_ratio: float = 1.0
        self.rect: QRectF = QRectF()


class FlameGraphWidget(QtWidgets.QWidget):
    """Custom interactive Flame Graph widget."""

    node_selected = pyqtSignal(dict)
    zoom_changed = pyqtSignal(object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(240)

        self.root_node: Optional[FlameNode] = None
        self.current_zoom_node: Optional[FlameNode] = None
        self.hovered_node: Optional[FlameNode] = None
        self.selected_node: Optional[FlameNode] = None

        self.row_height: int = 28
        self.max_depth: int = 0
        self.top_down: bool = True  # Icicle style (default) vs flame bottom-up
        self.color_mode: str = "heat"  # "heat", "hash", "depth"
        self.search_query: str = ""

        self.tooltip_widget: Optional[QtWidgets.QLabel] = None

    def set_tree_data(
        self,
        tree_list: List[Dict[str, Any]],
        syscall_map: Optional[Dict[str, Dict[str, int]]] = None,
    ):
        """Builds the tree from serialized profiler output."""
        if not tree_list:
            self.root_node = None
            self.current_zoom_node = None
            self.update()
            return

        syscall_map = syscall_map or {}

        # Create nodes
        nodes: Dict[int, FlameNode] = {}
        for item in tree_list:
            nid = item.get("id", 0)
            name = item.get("name", "unknown")
            total = item.get("total_ns", 0)
            self_ns = item.get("self_ns", 0)
            calls = item.get("calls", 1)
            addr = item.get("address", "")
            sc = syscall_map.get(name, {})
            cat = item.get("category", "user")
            nodes[nid] = FlameNode(
                nid, name, total, self_ns, calls, sc, address=addr, category=cat
            )

        # Link parent/child relationships
        for item in tree_list:
            nid = item.get("id", 0)
            parent_id = item.get("parent", 0)
            node = nodes.get(nid)
            if not node:
                continue

            if nid != 0 and parent_id in nodes and parent_id != nid:
                parent_node = nodes[parent_id]
                node.parent = parent_node
                parent_node.children.append(node)

        # Determine root
        self.root_node = nodes.get(0)
        if not self.root_node and nodes:
            self.root_node = next(iter(nodes.values()))

        # If root has 0 total duration, compute sum of children
        if self.root_node and self.root_node.total_ns <= 0:
            self.root_node.total_ns = sum(
                c.total_ns for c in self.root_node.children
            )
            if self.root_node.total_ns == 0:
                self.root_node.total_ns = 1

        self.current_zoom_node = self.root_node
        self.hovered_node = None
        self.selected_node = None
        self._compute_depths()
        self.update()

    def set_search_filter(self, query: str):
        self.search_query = query.strip().lower()
        self.update()

    def set_top_down(self, top_down: bool):
        self.top_down = top_down
        self.update()

    def set_color_mode(self, mode: str):
        self.color_mode = mode
        self.update()

    def reset_zoom(self):
        self.current_zoom_node = self.root_node
        self.selected_node = None  # Bug 18: clear ghost selection highlight
        self.zoom_changed.emit(self.current_zoom_node)
        self.update()

    def zoom_to_node(self, node: FlameNode):
        self.current_zoom_node = node
        self.zoom_changed.emit(self.current_zoom_node)
        self.update()

    def find_node_by_name(self, name: str) -> Optional[FlameNode]:
        """Finds the first node matching name in the tree."""
        if not self.root_node:
            return None
        queue = [self.root_node]
        while queue:
            cur = queue.pop(0)
            if cur.name == name or name in cur.name:
                return cur
            queue.extend(cur.children)
        return None

    def get_breadcrumbs(self) -> List[Tuple[str, FlameNode]]:
        """Returns the ancestor path for breadcrumb navigation."""
        return self.get_node_path(self.current_zoom_node)

    def get_node_path(self, node: Optional[FlameNode]) -> List[Tuple[str, FlameNode]]:
        """Returns the ancestor path for any given node."""
        crumbs: List[Tuple[str, FlameNode]] = []
        cur = node
        while cur:
            crumbs.append((cur.name or "root", cur))
            cur = cur.parent
        crumbs.reverse()
        return crumbs

    def _compute_depths(self):
        if not self.root_node:
            self.max_depth = 0
            return

        def _walk(node: FlameNode, depth: int):
            node.depth = depth
            m = depth
            for c in node.children:
                m = max(m, _walk(c, depth + 1))
            return m

        self.max_depth = _walk(self.root_node, 0)
        # Update widget minimum height dynamically
        needed_height = max(260, (self.max_depth + 2) * self.row_height + 20)
        self.setMinimumHeight(needed_height)

    def _compute_layout(self, width: float, height: float):
        """Calculates screen rectangles for nodes within current zoom scope."""
        if not self.current_zoom_node:
            return

        zoom_root = self.current_zoom_node
        total_time = max(1, zoom_root.total_ns)
        base_depth = zoom_root.depth

        def _layout_node(node: FlameNode, x_ratio: float, w_ratio: float):
            node.x_ratio = x_ratio
            node.w_ratio = w_ratio

            rel_depth = node.depth - base_depth
            if self.top_down:
                y = rel_depth * self.row_height + 4
            else:
                y = height - (rel_depth + 1) * self.row_height - 4

            node.rect = QRectF(
                x_ratio * width, y, max(1.0, w_ratio * width), self.row_height - 1
            )

            # Layout children
            cur_x = x_ratio
            for child in node.children:
                child_w = child.total_ns / total_time
                if child_w <= 0:
                    continue
                # Cap so children never render outside the parent bounds
                child_w = min(child_w, (x_ratio + w_ratio) - cur_x)
                if child_w <= 0:
                    break
                _layout_node(child, cur_x, child_w)
                cur_x += child_w

        _layout_node(zoom_root, 0.0, 1.0)

    def paintEvent(self, event: QtGui.QPaintEvent):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

        rect = self.rect()
        w = float(rect.width())
        h = float(rect.height())

        # Clean modern slate background
        painter.fillRect(rect, QtGui.QColor("#181b20"))

        if not self.root_node or not self.current_zoom_node:
            painter.setPen(QtGui.QColor("#6c7280"))
            painter.setFont(QtGui.QFont("-apple-system", 13))
            painter.drawText(
                rect,
                Qt.AlignCenter,
                "No flame graph data available. Run benchmark with Function Profiling.",
            )
            painter.end()
            return

        self._compute_layout(w, h)

        # Draw all visible nodes under zoom_root
        def _draw_node(node: FlameNode):
            # Culling: skip nodes outside view or narrower than 0.5px
            if node.rect.width() >= 0.5 and node.rect.bottom() >= 0 and node.rect.top() <= h:
                self._draw_box(painter, node)

            for child in node.children:
                _draw_node(child)

        _draw_node(self.current_zoom_node)
        painter.end()

    def _draw_box(self, painter: QtGui.QPainter, node: FlameNode):
        r = node.rect
        is_hovered = node == self.hovered_node
        is_selected = node == self.selected_node

        # Base color
        base_color = self._get_node_color(node)

        # Search highlight
        matches_search = bool(
            self.search_query and self.search_query in node.name.lower()
        )
        if self.search_query:
            if matches_search:
                base_color = QtGui.QColor("#ffd166")  # Vibrant amber highlight
            else:
                base_color = QtGui.QColor(
                    int(base_color.red() * 0.4),
                    int(base_color.green() * 0.4),
                    int(base_color.blue() * 0.4),
                    180,
                )

        if is_hovered:
            base_color = base_color.lighter(130)

        # Draw box
        painter.setPen(
            QtGui.QColor("#ffffff")
            if is_selected
            else QtGui.QColor(30, 34, 42, 180)
        )
        painter.setBrush(QtGui.QBrush(base_color))
        painter.drawRoundedRect(r, 2.5, 2.5)

        # Draw text if box is wide enough
        if r.width() > 36:
            painter.save()
            painter.setClipRect(r.adjusted(3, 1, -3, -1))
            painter.setPen(
                QtGui.QColor("#111827")
                if matches_search
                else QtGui.QColor("#f3f4f6")
            )

            font = QtGui.QFont("-apple-system", 10, QtGui.QFont.Medium)
            font.setStyleHint(QtGui.QFont.SansSerif)
            painter.setFont(font)

            label = node.name
            if r.width() > 140:
                duration_str = format_duration(node.total_ns)
                label = f"{node.name}  ({duration_str})"

            painter.drawText(
                r.adjusted(4, 0, -4, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )
            painter.restore()

    def _get_node_color(self, node: FlameNode) -> QtGui.QColor:
        """Determines frame color based on color mode and timing."""
        if node.id == 0:
            return QtGui.QColor("#374151")

        if self.color_mode == "category":
            if node.category == "std_direct":
                return QtGui.QColor("#2563eb")
            elif node.category == "std_internal":
                return QtGui.QColor("#475569")
            elif node.category == "runtime":
                return QtGui.QColor("#9d174d")
            else:
                ratio = (node.self_ns / node.total_ns) if node.total_ns > 0 else 0.0
                r = int(210 + 45 * ratio)
                g = int(90 + 60 * (1.0 - ratio))
                b = int(40 + 30 * (1.0 - ratio))
                return QtGui.QColor(min(255, r), min(255, g), min(255, b))

        if self.color_mode == "heat":
            # Intensity based on self-time percentage relative to total
            ratio = (
                (node.self_ns / node.total_ns) if node.total_ns > 0 else 0.0
            )
            # Interpolate from cool warm slate to intense red-orange
            r = int(220 + 35 * ratio)
            g = int(80 + 90 * (1.0 - ratio))
            b = int(50 + 40 * (1.0 - ratio))
            return QtGui.QColor(min(255, r), min(255, g), min(255, b))

        if self.color_mode == "hash":
            # Deterministic color by function name
            h = abs(hash(node.name)) % 360
            return QtGui.QColor.fromHsv(h, 170, 210)

        # By depth
        h = (node.depth * 38) % 360
        return QtGui.QColor.fromHsv(h, 160, 200)

    def _find_node_at(
        self, pos: QPointF, node: Optional[FlameNode]
    ) -> Optional[FlameNode]:
        if not node:
            return None
        if node.rect.contains(pos):
            return node
        for child in node.children:
            found = self._find_node_at(pos, child)
            if found:
                return found
        return None

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        pos = QPointF(event.pos())
        node = self._find_node_at(pos, self.current_zoom_node)
        if node != self.hovered_node:
            self.hovered_node = node
            if node:
                self.setCursor(Qt.PointingHandCursor)
                self._show_tooltip(event.globalPos(), node)
            else:
                self.setCursor(Qt.ArrowCursor)
                QtWidgets.QToolTip.hideText()
            self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == Qt.LeftButton:
            pos = QPointF(event.pos())
            node = self._find_node_at(pos, self.current_zoom_node)
            if node:
                self.selected_node = node
                self.node_selected.emit(
                    {
                        "id": node.id,
                        "name": node.name,
                        "total_ns": node.total_ns,
                        "self_ns": node.self_ns,
                        "calls": node.calls,
                        "syscalls": node.syscalls,
                        "address": node.address,
                    }
                )
                self.update()
        elif event.button() == Qt.RightButton:
            # Right-click zooms out to parent
            if (
                self.current_zoom_node
                and self.current_zoom_node.parent
                and self.current_zoom_node != self.root_node
            ):
                self.zoom_to_node(self.current_zoom_node.parent)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent):
        """Double-click zooms into the clicked node (only if it has children)."""
        if event.button() == Qt.LeftButton:
            pos = QPointF(event.pos())
            node = self._find_node_at(pos, self.current_zoom_node)
            if node and node.children:  # Bug 16: don't zoom into leaf nodes
                self.zoom_to_node(node)

    def _show_tooltip(self, global_pos: QtCore.QPoint, node: FlameNode):
        root_time = (
            self.root_node.total_ns
            if self.root_node and self.root_node.total_ns > 0
            else 1
        )
        parent_time = (
            node.parent.total_ns
            if node.parent and node.parent.total_ns > 0
            else root_time
        )

        total_pct = (node.total_ns / root_time) * 100.0
        parent_pct = (node.total_ns / parent_time) * 100.0
        self_pct = (
            (node.self_ns / node.total_ns) * 100.0 if node.total_ns > 0 else 0.0
        )
        avg_ns = (node.total_ns / node.calls) if node.calls > 0 else 0

        sc_html = ""
        if node.syscalls:
            sc_items = ", ".join(
                f"{k}: {v}" for k, v in list(node.syscalls.items())[:4]
            )
            sc_html = f"<b>Syscalls:</b> {sc_items}<br>"

        cat_badge = {
            "user": "<span style='background: #065f46; color: #34d399; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;'>USER CODE</span>",
            "std_direct": "<span style='background: #1e3a8a; color: #60a5fa; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;'>DIRECT STD</span>",
            "std_internal": "<span style='background: #374151; color: #9ca3af; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;'>INTERNAL STD</span>",
            "runtime": "<span style='background: #831843; color: #f472b6; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;'>RUNTIME</span>",
        }.get(node.category, "")

        text = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; font-size: 13px; color: #f3f4f6; background-color: #1f242d; padding: 8px; border: 1px solid #374151; border-radius: 6px;">
          <b style="font-size: 14px; color: #60a5fa;">{node.name}</b> {cat_badge}<br>
          <b>Inclusive:</b> {format_duration(node.total_ns)} ({total_pct:.1f}% total, {parent_pct:.1f}% parent)<br>
          <b>Self:</b> {format_duration(node.self_ns)} ({self_pct:.1f}% self)<br>
          <b>Calls:</b> {node.calls:,} (avg {format_duration(avg_ns)})<br>
          {sc_html}
          <div style="font-size: 11px; color: #9ca3af; margin-top: 6px;">Click to select • {'Double-click to zoom in' if node.children else 'Leaf node (no children)'} • Right-click to zoom out</div>
        </div>
        """
        QtWidgets.QToolTip.showText(global_pos, text, self)
