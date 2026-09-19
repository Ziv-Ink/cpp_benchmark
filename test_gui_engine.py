#!/usr/bin/env python3
"""Tests for GUI, profiling engine, and export utilities."""

import os
import tempfile
import unittest
from pathlib import Path

from benchmark_engine import (
    scan_cmake_targets,
    BenchmarkConfig,
    SymbolResolver,
    normalize_profile_durations,
    classify_symbol,
    is_std_symbol,
    is_runtime_harness,
)
from flamegraph_widget import FlameGraphWidget, FlameNode, format_duration
from syscall_tracer import SyscallTracer
from export_utils import export_json, export_csv, export_html_report


class TestBenchmarkStudio(unittest.TestCase):

    def test_scan_cmake_targets(self):
        with tempfile.TemporaryDirectory() as td:
            cm = Path(td) / "CMakeLists.txt"
            cm.write_text("cmake_minimum_required(VERSION 3.15)\nproject(demo)\nadd_executable(my_target main.cpp)\n")
            targets = scan_cmake_targets(td)
            self.assertIn("my_target", targets)

    def test_top_bar_fits_minimum_window(self):
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        from dashboard_gui import BenchmarkStudioWindow

        window = BenchmarkStudioWindow(
            str(Path(__file__).parent / "fixtures" / "sample_project")
        )
        window.resize(980, 680)
        window.show()
        app.processEvents()

        self.assertLessEqual(
            window.btn_run.geometry().right(), window.centralWidget().rect().right()
        )
        self.assertGreaterEqual(
            window.btn_browse_target.geometry().left()
            - window.cmb_target.geometry().right(),
            0,
        )
        window.close()

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

    def test_profile_normalization_scales_every_displayed_duration(self):
        profile = {
            "functions": [
                {
                    "name": "work()",
                    "total_ns": 10_000,
                    "self_ns": 2_000,
                    "min_ns": 100,
                    "max_ns": 1_000,
                }
            ],
            "tree": [
                {"id": 0, "total_ns": 10_000, "self_ns": 2_000},
            ],
        }

        self.assertTrue(normalize_profile_durations(profile, 100_000))
        fn = profile["functions"][0]
        self.assertEqual(fn["total_ns"], 100_000)
        self.assertEqual(fn["self_ns"], 20_000)
        self.assertEqual(fn["min_ns"], 1_000)
        self.assertEqual(fn["max_ns"], 10_000)
        self.assertEqual(profile["tree"][0]["total_ns"], 100_000)
        self.assertEqual(profile["total_duration_ns"], 100_000)
        self.assertEqual(profile["timing_basis"], "normalized_profile_estimate")

    def test_none_exports(self):
        data = {
            "target": "none_test",
            "runs": 1,
            "summary": {
                "median_ns": 5000,
                "mean_ns": 5000,
                "relative_standard_error_percent": None,
                "median_drift_percent": None
            },
            "profile": {},
            "syscalls": {}
        }
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "test_none.html"
            export_html_report(data, hp)
            self.assertTrue(hp.is_file() and hp.stat().st_size > 0)

    def test_timeline_none_safety(self):
        from PyQt5.QtWidgets import QApplication
        from dashboard_gui import TimelineChartWidget
        app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        widget = TimelineChartWidget()
        # Samples with None elapsed_ns (e.g. timeout or error) must not crash
        widget.set_samples([
            {"phase": "warmup", "elapsed_ns": None, "error": "timeout"},
            {"phase": "sample", "elapsed_ns": 1000},
            {"phase": "sample", "elapsed_ns": None}
        ])
        self.assertEqual(len(widget.samples), 3)

    def test_comparison_direction(self):
        # When target B is faster than target A
        m1 = 100_000_000  # 100ms
        m2 = 10_000_000   # 10ms (10x faster)
        fastest = "b" if m2 <= m1 else "a"
        slowest = "a" if m2 <= m1 else "b"
        speedup = max(m1, m2) / min(m1, m2)
        self.assertEqual(fastest, "b")
        self.assertAlmostEqual(speedup, 10.0)

    def test_symbol_classification(self):
        self.assertEqual(classify_symbol("my_custom_task()", "user"), "user")
        self.assertEqual(classify_symbol("std::sort<int*>", "user"), "std_direct")
        self.assertEqual(classify_symbol("std::vector<int>::push_back(int const&)", "user"), "std_direct")
        self.assertEqual(classify_symbol("std::__1::__introsort_loop", "std_direct"), "std_internal")
        self.assertEqual(classify_symbol("std::__split_buffer<int>::push_back", "std_internal"), "std_internal")
        self.assertEqual(classify_symbol("bench_profile_finish", "user"), "runtime")
        self.assertEqual(classify_symbol("bench::MainTimer::~MainTimer", "user"), "runtime")

    def test_stl_filter_modes_and_containment_inspector(self):
        from PyQt5.QtWidgets import QApplication
        from dashboard_gui import BenchmarkStudioWindow

        app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        window = BenchmarkStudioWindow(
            str(Path(__file__).parent / "fixtures" / "sample_project")
        )

        test_report = {
            "target": "demo_target",
            "summary": {"median_ns": 50_000_000},
            "profile": {
                "total_duration_ns": 50_000_000,
                "timing_basis": "normalized_profile_estimate",
                "functions": [
                    {
                        "name": "user_worker()",
                        "category": "user",
                        "calls": 1,
                        "total_ns": 50_000_000,
                        "self_ns": 10_000_000,
                        "containment": {
                            "is_strictly_contained": False,
                            "sole_caller": None,
                            "callers_list": [{"name": "Root / Entry", "calls": 1, "total_ns": 50_000_000, "pct_of_callee": 100.0}],
                            "callees_list": [
                                {"name": "std::sort()", "calls": 1, "total_ns": 30_000_000, "pct_of_parent": 60.0},
                                {"name": "busy_helper()", "calls": 1, "total_ns": 10_000_000, "pct_of_parent": 20.0},
                            ],
                        }
                    },
                    {
                        "name": "std::sort()",
                        "category": "std_direct",
                        "calls": 1,
                        "total_ns": 30_000_000,
                        "self_ns": 5_000_000,
                        "containment": {
                            "is_strictly_contained": True,
                            "sole_caller": "user_worker()",
                            "callers_list": [{"name": "user_worker()", "calls": 1, "total_ns": 30_000_000, "pct_of_callee": 100.0}],
                            "callees_list": [{"name": "std::__introsort_loop()", "calls": 1, "total_ns": 25_000_000, "pct_of_parent": 83.3}],
                        }
                    },
                    {
                        "name": "std::__introsort_loop()",
                        "category": "std_internal",
                        "calls": 1,
                        "total_ns": 25_000_000,
                        "self_ns": 25_000_000,
                        "containment": {
                            "is_strictly_contained": True,
                            "sole_caller": "std::sort()",
                            "callers_list": [{"name": "std::sort()", "calls": 1, "total_ns": 25_000_000, "pct_of_callee": 100.0}],
                            "callees_list": [],
                        }
                    },
                ],
                "tree": [
                    {"id": 0, "parent": 0, "name": "root", "total_ns": 50_000_000, "self_ns": 0, "calls": 1, "category": "runtime"},
                    {"id": 1, "parent": 0, "name": "user_worker()", "total_ns": 50_000_000, "self_ns": 10_000_000, "calls": 1, "category": "user"},
                    {"id": 2, "parent": 1, "name": "std::sort()", "total_ns": 30_000_000, "self_ns": 5_000_000, "calls": 1, "category": "std_direct"},
                    {"id": 3, "parent": 2, "name": "std::__introsort_loop()", "total_ns": 25_000_000, "self_ns": 25_000_000, "calls": 1, "category": "std_internal"},
                ],
            },
            "syscalls": {},
        }

        window.current_report = test_report
        window._populate_functions_table(test_report["profile"]["functions"], 50_000_000)
        window._populate_functions_tree(test_report["profile"]["tree"], 50_000_000)

        # Mode 0: "Direct Standard Calls" -> user_worker + std::sort (2 items)
        window.cmb_stl_filter.setCurrentIndex(0)
        self.assertEqual(window.tbl_functions.rowCount(), 2)

        # Mode 1: "User Functions Only" -> user_worker only (1 item)
        window.cmb_stl_filter.setCurrentIndex(1)
        self.assertEqual(window.tbl_functions.rowCount(), 1)
        self.assertEqual(window.tbl_functions.item(0, 0).text(), "user_worker()")

        # Mode 2: "Show All (Including Internals)" -> all 3 items
        window.cmb_stl_filter.setCurrentIndex(2)
        self.assertEqual(window.tbl_functions.rowCount(), 3)

        # Test containment details display for std::sort (index 1)
        fn_sort = test_report["profile"]["functions"][1]
        window._display_function_details(fn_sort)
        html_details = window.txt_detail_body.toHtml()
        self.assertIn("100% Strictly Contained", html_details)
        self.assertIn("user_worker()", html_details)
        self.assertIn("DIRECT STD CALL", html_details)

        # Test Tree Widget Hierarchy
        self.assertEqual(window.tree_functions.topLevelItemCount(), 1)
        root_item = window.tree_functions.topLevelItem(0)
        self.assertEqual(root_item.text(0), "user_worker()")
        self.assertEqual(root_item.childCount(), 1)
        child_sort = root_item.child(0)
        self.assertEqual(child_sort.text(0), "std::sort()")

        window.close()


if __name__ == "__main__":
    unittest.main()
