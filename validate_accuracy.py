#!/usr/bin/env python3
import sys
import json
import time
from pathlib import Path
from PyQt5 import QtCore

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from benchmark_engine import BenchmarkWorker, BenchmarkConfig

FIXTURES = [
    {
        "dir": "fixtures/accuracy_test",
        "target": "split_30_70",
        "expected": {"main": 200_000_000, "func_a": 60_000_000, "func_b": 140_000_000},
        "tolerance": 0.1
    },
    {
        "dir": "fixtures/accuracy_thread",
        "target": "test_thread",
        "expected": {"main": 50_000_000}, # Roughly 50ms wall-clock, wait no, they sleep 20 and 30ms so 30ms total if parallel? worker_a is 20, worker_b is 30, so 50ms total per thread
        "tolerance": 0.2
    },
    {
        "dir": "fixtures/accuracy_exceptions",
        "target": "test_exceptions",
        "expected": {"main": 70_000_000, "catch_exception": 70_000_000},
        "tolerance": 0.1
    },
    {
        "dir": "fixtures/accuracy_recursion",
        "target": "test_recursion",
        "expected": {"main": 10_000_000}, # 100 deep, wait, spin_wait is 10ms only once when depth=0. So 10ms total.
        "tolerance": 0.1
    },
    {
        "dir": "fixtures/accuracy_io",
        "target": "test_io",
        "expected": {"main": 40_000_000}, # spin_wait(10) + sleep(30) + IO overhead
        "tolerance": 0.5 # High tolerance due to IO
    },
    {
        "dir": "fixtures/accuracy_templates",
        "target": "test_templates",
        "expected": {"main": 30_000_000}, # 3 * 10ms
        "tolerance": 0.1
    }
]

def run_fixtures():
    app = QtCore.QCoreApplication(sys.argv)
    results = {}
    current = 0

    def run_next():
        nonlocal current
        if current >= len(FIXTURES):
            analyze_results()
            app.quit()
            return
            
        fix = FIXTURES[current]
        print(f"Running {fix['target']} in {fix['dir']}...")
        cfg = BenchmarkConfig(
            project_dir=Path(fix["dir"]).resolve(),
            target=fix["target"],
            run_mode="combined",
            runs=3,
            warmup=1,
            profile_functions=True,
            trace_syscalls=False,
        )
        worker = BenchmarkWorker(cfg)
        
        def on_finished(report):
            nonlocal current
            results[fix["target"]] = report
            current += 1
            run_next()
            
        def on_error(err):
            nonlocal current
            print(f"Error on {fix['target']}: {err}")
            results[fix["target"]] = {"error": err}
            current += 1
            run_next()
            
        worker.sig_finished.connect(on_finished)
        worker.sig_error.connect(on_error)
        # Using a QTimer to run the worker in the event loop instead of starting its thread?
        # Actually BenchmarkWorker inherits from QThread and calling run() directly blocks.
        # So we can just start() it. Wait, the old scratch script called worker.run() synchronously?
        # Let's call start()
        worker.start()
        # Keep a reference to prevent garbage collection
        app.worker_ref = worker

    def analyze_results():
        for fix in FIXTURES:
            target = fix["target"]
            report = results.get(target)
            if not report:
                continue
            if "error" in report:
                print(f"[{target}] FAILED with error: {report['error']}")
                continue
            
            print(f"\n[{target}] Analysis:")
            profile = report.get("profile", {})
            funcs = profile.get("functions", [])
            print(f"Available functions: {[f['name'] for f in funcs]}")
            
            for func_name, expected_ns in fix["expected"].items():
                found = False
                for f in funcs:
                    if func_name in f["name"]:
                        found = True
                        measured = f["total_ns"]
                        err = abs(measured - expected_ns) / expected_ns
                        status = "OK" if err <= fix["tolerance"] else "FAIL"
                        print(f"  {func_name}: Expected {expected_ns/1e6:.1f}ms, Got {measured/1e6:.1f}ms (Error: {err*100:.1f}%) [{status}]")
                        break
                if not found:
                    print(f"  {func_name}: FAIL (Not found in profile)")
            
            # Additional checks e.g., missing nodes, garbage totals
            tot = profile.get("total_duration_ns", 0)
            if tot > 0:
                print(f"  Total profile duration: {tot/1e6:.1f}ms")

    run_next()
    app.exec_()

if __name__ == "__main__":
    run_fixtures()
