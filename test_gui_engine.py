#!/usr/bin/env python3
"""Tests for GUI, profiling engine, and export utilities."""

import os
import tempfile
import unittest
from pathlib import Path

from benchmark_engine import scan_cmake_targets, BenchmarkConfig, SymbolResolver
from flamegraph_widget import FlameGraphWidget, FlameNode, format_duration
from syscall_tracer import SyscallTracer
from export_utils import export_json, export_csv, export_html_report


class TestBenchmarkStudio(unittest.TestCase):

    def test_scan_cmake_targets(self):
        proj = Path(__file__).parent.parent
        targets = scan_cmake_targets(proj)
        self.assertIn("read_file_on_device_cpp", targets)

    def test_flamegraph_widget(self):
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        widget = FlameGraphWidget()

        tree_data = [
            {"id": 0, "parent": 0, "name": "root", "total_ns": 1000, "self_ns": 0, "calls": 1},
            {"id": 1, "parent": 0, "name": "main()", "total_ns": 1000, "self_ns": 200, "calls": 1},
            {"id": 2, "parent": 1, "name": "worker()", "total_ns": 800, "self_ns": 800, "calls": 2},
        ]
        widget.set_tree_data(tree_data)
        self.assertIsNotNone(widget.root_node)
        self.assertEqual(len(widget.root_node.children), 1)
        self.assertEqual(widget.root_node.children[0].name, "main()")

        # Test search filter
        widget.set_search_filter("worker")
        self.assertEqual(widget.search_query, "worker")

        # Test zoom
        worker_node = widget.root_node.children[0].children[0]
        widget.zoom_to_node(worker_node)
        self.assertEqual(widget.current_zoom_node, worker_node)

        # Test breadcrumbs
        crumbs = widget.get_breadcrumbs()
        self.assertEqual(len(crumbs), 3)

        widget.reset_zoom()
        self.assertEqual(widget.current_zoom_node, widget.root_node)

    def test_syscall_parsing(self):
        tracer = SyscallTracer("/bin/ls", "ls")
        raw_output = """
openat(AT_FDCWD, "/dev/null", O_RDONLY) = 3
 > /path/to/my_app(read_note()+0x20) [0x1234]
 > /path/to/my_app(main+0x10) [0x1200]
read(3, "abc", 3) = 3
 > /path/to/my_app(read_note()+0x40) [0x1254]
close(3) = 0
 > /path/to/my_app(read_note()+0x60) [0x1274]
"""
        parsed = tracer.parse_strace_k_output(raw_output)
        self.assertEqual(parsed["total_syscalls"], 3)
        self.assertIn("openat", parsed["summary"])
        self.assertIn("read", parsed["summary"])
        self.assertIn("close", parsed["summary"])
        self.assertIn("read_note()", parsed["by_function"])
        self.assertEqual(parsed["by_function"]["read_note()"]["openat"], 1)
        self.assertEqual(parsed["by_function"]["read_note()"]["read"], 1)
        self.assertEqual(parsed["by_function"]["read_note()"]["close"], 1)

    def test_exports(self):
        data = {
            "target": "demo_target",
            "runs": 10,
            "summary": {"median_ns": 5000, "mean_ns": 5100},
            "profile": {
                "functions": [
                    {"name": "test_func()", "calls": 2, "total_ns": 5000, "self_ns": 5000, "syscalls": {"read": 1}}
                ],
                "tree": [
                    {"id": 0, "parent": 0, "name": "root", "total_ns": 5000, "self_ns": 0, "calls": 1, "children": [1]},
                    {"id": 1, "parent": 0, "name": "test_func()", "total_ns": 5000, "self_ns": 5000, "calls": 2, "children": []}
                ]
            },
            "syscalls": {"total_syscalls": 1, "summary": {"read": 1}, "by_function": {"test_func()": {"read": 1}}}
        }
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "test.json"
            cp = Path(td) / "test.csv"
            hp = Path(td) / "test.html"

            export_json(data, jp)
            export_csv(data, cp)
            export_html_report(data, hp)

            self.assertTrue(jp.is_file() and jp.stat().st_size > 0)
            self.assertTrue(cp.is_file() and cp.stat().st_size > 0)
            self.assertTrue(hp.is_file() and hp.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
