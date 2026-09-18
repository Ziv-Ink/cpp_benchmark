#!/usr/bin/env python3
"""Benchmark and profiling execution engine.

Coordinates CMake configuration, builds, instrumentation, multi-sample
benchmark timing, function profiling (-finstrument-functions), and
syscall tracing. Provides a QThread worker for responsive GUI execution.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Any, Callable, Tuple

from PyQt5 import QtCore

# Import existing benchmark functions
sys.path.insert(0, str(Path(__file__).parent))
import benchmark as base_bench
from syscall_tracer import SyscallTracer


def format_ns(ns: int | float) -> str:
    if ns is None or math.isnan(ns) or ns < 0:
        return "0 ns"
    if ns < 1_000:
        return f"{ns:.0f} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    return f"{ns / 1_000_000_000:.3f} s"


def clean_cpp_name(name: str) -> str:
    """Simplifies verbose C++ demangled names for better readability."""
    if not name:
        return name
    # Strip return types from the beginning (e.g. "char8_t* ", "void ")
    # Actually, just strip everything before the first namespace or class if it ends in space/pointer
    name = re.sub(r'^[\w\s\*&_]+?\s+([A-Za-z_])', r'\1', name)
    
    # Remove ABI tags
    name = re.sub(r'\[abi:[^\]]+\]', '', name)
    # Simplify NDK / C++11 std:: namespaces (handle it globally)
    name = re.sub(r'std::(?:__ndk1|__cxx11|__1)::', 'std::', name)
    # Simplify common STL types
    name = re.sub(r'std::basic_string<char,\s*std::char_traits<char>,\s*std::allocator<char>\s*>', 'std::string', name)
    name = re.sub(r'std::basic_string_view<char,\s*std::char_traits<char>\s*>', 'std::string_view', name)
    # Remove default allocators and deleters
    name = re.sub(r',\s*std::allocator<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::default_delete<[^>]+>\s*', '', name)
    return name.strip()


@dataclasses.dataclass
class BenchmarkConfig:
    project_dir: Path
    target: str
    run_mode: str = "combined"  # 'time', 'profile', 'combined'
    compare_target: Optional[str] = None
    profile_functions: bool = True
    trace_syscalls: bool = True
    adb: bool = False
    device: Optional[str] = None
    ndk: Optional[str] = None
    android_api: int = 23
    runs: Optional[int] = 20  # None for adaptive
    min_runs: int = 20
    max_runs: int = 200
    max_time: float = 30.0
    precision: float = 3.0
    warmup: int = 2
    timeout: float = 60.0
    program_args: List[str] = dataclasses.field(default_factory=list)
    cmake_args: List[str] = dataclasses.field(default_factory=list)


class SymbolResolver:
    """Uses host nm / addr2line to symbolize un-demangled addresses."""

    def __init__(self, binary_path: Path):
        self.binary_path = binary_path
        self._symbols: Dict[int, str] = {}
        self._load_nm()

    def _load_nm(self):
        if not self.binary_path.is_file():
            return
        nm_bin = shutil.which("nm")
        if not nm_bin:
            return
        try:
            res = subprocess.run(
                [nm_bin, "-C", "-n", str(self.binary_path)],
                capture_output=True,
                text=True,
                errors="replace",
            )
            for line in res.stdout.splitlines():
                parts = line.strip().split(maxsplit=2)
                if len(parts) == 3:
                    addr_str, _, sym_name = parts
                    try:
                        addr = int(addr_str, 16)
                        self._symbols[addr] = sym_name
                    except ValueError:
                        pass
        except Exception:
            pass

    def resolve(self, addr_str: str) -> Optional[str]:
        try:
            addr = int(addr_str, 16)
        except ValueError:
            return None
        if addr in self._symbols:
            return self._symbols[addr]
        # Check nearest symbol if offset
        candidates = [a for a in self._symbols.keys() if a <= addr]
        if candidates:
            nearest = max(candidates)
            if addr - nearest < 4096:
                return self._symbols[nearest]
        return None


class BenchmarkWorker(QtCore.QThread):
    """Asynchronous background worker for benchmark execution."""

    sig_log = QtCore.pyqtSignal(str)
    sig_status = QtCore.pyqtSignal(str)
    sig_stage = QtCore.pyqtSignal(str, int, int)
    sig_sample = QtCore.pyqtSignal(dict)
    sig_stability = QtCore.pyqtSignal(dict)
    sig_finished = QtCore.pyqtSignal(dict)
    sig_error = QtCore.pyqtSignal(str)
    sig_cancelled = QtCore.pyqtSignal()

    def __init__(
        self, config: BenchmarkConfig, parent: Optional[QtCore.QObject] = None
    ):
        super().__init__(parent)
        self.config = config
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def run(self):
        try:
            import copy
            if self.config.run_mode == "combined":
                self.sig_log.emit("=== COMBINED MODE: PASS 1 (ACCURATE TIMING) ===")
                cfg_time = copy.deepcopy(self.config)
                cfg_time.profile_functions = False
                report_time = self._execute_benchmark(cfg_time, stage_offset=0, total_stages=8)
                if self._is_cancelled:
                    self.sig_status.emit("Benchmark cancelled")
                    self.sig_cancelled.emit()
                    return

                self.sig_log.emit("\n=== COMBINED MODE: PASS 2 (PROFILE BREAKDOWN) ===")
                cfg_prof = copy.deepcopy(self.config)
                cfg_prof.profile_functions = True
                cfg_prof.runs = 1
                cfg_prof.warmup = 0
                report_prof = self._execute_benchmark(cfg_prof, stage_offset=4, total_stages=8)
                if self._is_cancelled:
                    self.sig_status.emit("Benchmark cancelled")
                    self.sig_cancelled.emit()
                    return

                report_time["profile"] = report_prof.get("profile", {})
                report_time["syscalls"] = report_prof.get("syscalls", {})
                self.sig_finished.emit(report_time)

            elif self.config.run_mode == "time":
                cfg_time = copy.deepcopy(self.config)
                cfg_time.profile_functions = False
                report = self._execute_benchmark(cfg_time, stage_offset=0, total_stages=4)
                if self._is_cancelled:
                    self.sig_status.emit("Benchmark cancelled")
                    self.sig_cancelled.emit()
                    return
                self.sig_finished.emit(report)

            else: # profile
                cfg_prof = copy.deepcopy(self.config)
                cfg_prof.profile_functions = True
                cfg_prof.runs = 1
                cfg_prof.warmup = 0
                report = self._execute_benchmark(cfg_prof, stage_offset=0, total_stages=4)
                if self._is_cancelled:
                    self.sig_status.emit("Benchmark cancelled")
                    self.sig_cancelled.emit()
                    return
                self.sig_finished.emit(report)

        except Exception as exc:
            self.sig_log.emit(f"[ERROR] {exc}")
            self.sig_error.emit(str(exc))

    def _execute_benchmark(self, cfg: Optional[BenchmarkConfig] = None, stage_offset: int = 0, total_stages: int = 4) -> Dict[str, Any]:
        cfg = cfg or self.config
        bench_dir = Path(__file__).parent.resolve()
        profiler_h = bench_dir / "bench_profiler.h"
        profiler_cpp = bench_dir / "bench_profiler.cpp"
        main_h = bench_dir / "benchmark.h"

        run_start = time.monotonic()
        timings: Dict[str, float] = {}

        self.sig_log.emit("=" * 60)
        self.sig_log.emit(f"C++ Benchmark & Profiler: target={cfg.target}")
        self.sig_log.emit(f"Project: {cfg.project_dir}")
        self.sig_log.emit(
            f"Mode: {'ADB Device' if cfg.adb else 'Local Host'} | Function Profiling: {cfg.profile_functions}"
        )
        self.sig_log.emit("=" * 60)

        # Stage 1: Setup & Configure
        self.sig_stage.emit("Configure Release Build", stage_offset + 1, total_stages)
        self.sig_status.emit("Configuring CMake Release build...")
        t_stage = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="main-benchmark-gui-") as temp:
            root = Path(temp)
            source, build = root / "source", root / "build"
            base_bench.copy_project(cfg.project_dir, source)

            platform_args: List[str] = []
            serial = cfg.device
            adb = shutil.which("adb") or "adb"

            if cfg.adb:
                if not serial:
                    serial = base_bench.select_adb_device(
                        adb, base_bench.Console()
                    )
                ndk_root, toolchain = base_bench.find_ndk(
                    cfg.ndk or os.environ.get("ANDROID_NDK_HOME")
                )
                abi = base_bench.adb_property(adb, serial, "ro.product.cpu.abi")
                platform_args = [
                    f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
                    f"-DANDROID_ABI={abi}",
                    f"-DANDROID_PLATFORM=android-{cfg.android_api}",
                    "-DANDROID_STL=c++_static",
                    "-DBUILD_TESTING=OFF",
                ]

            # Append profiler to CMakeLists.txt if function profiling is enabled
            if cfg.profile_functions:
                shutil.copyfile(profiler_h, source / "bench_profiler.h")
                shutil.copyfile(profiler_cpp, source / "bench_profiler.cpp")

                # Insert add_compile_options(-finstrument-functions) after project(...)
                # so all static libraries (e.g. SNfiletools) are instrumented
                cm_path = source / "CMakeLists.txt"
                cm_txt = cm_path.read_text(encoding="utf-8")
                proj_match = re.search(r"project\s*\([^)]*\)", cm_txt, re.IGNORECASE)
                if proj_match:
                    pos = proj_match.end()
                    cm_txt = cm_txt[:pos] + "\nadd_compile_options(-finstrument-functions)\n" + cm_txt[pos:]
                else:
                    cm_txt = "add_compile_options(-finstrument-functions)\n" + cm_txt

                cmake_additions = f"""
# Added by cpp_benchmark profiler
if (TARGET {cfg.target})
    target_sources({cfg.target} PRIVATE "${{CMAKE_CURRENT_LIST_DIR}}/bench_profiler.cpp")
    target_link_options({cfg.target} PRIVATE -rdynamic)
    target_link_libraries({cfg.target} PRIVATE dl)
    set_source_files_properties("${{CMAKE_CURRENT_LIST_DIR}}/bench_profiler.cpp" PROPERTIES COMPILE_OPTIONS "$<$<CXX_COMPILER_ID:GNU>:-fno-instrument-functions>")
endif()
"""
                if cfg.compare_target and cfg.compare_target != cfg.target:
                    cmake_additions += f"""
if (TARGET {cfg.compare_target})
    target_sources({cfg.compare_target} PRIVATE "${{CMAKE_CURRENT_LIST_DIR}}/bench_profiler.cpp")
    target_link_options({cfg.compare_target} PRIVATE -rdynamic)
    target_link_libraries({cfg.compare_target} PRIVATE dl)
    set_source_files_properties("${{CMAKE_CURRENT_LIST_DIR}}/bench_profiler.cpp" PROPERTIES COMPILE_OPTIONS "$<$<CXX_COMPILER_ID:GNU>:-fno-instrument-functions>")
endif()
"""
                cm_path.write_text(cm_txt + "\n" + cmake_additions, encoding="utf-8")

            targets, configure_args, toolchains = base_bench.configure(
                source, build, platform_args + cfg.cmake_args
            )
            timings["configure_seconds"] = time.monotonic() - t_stage
            self.sig_log.emit(
                f"[OK] CMake configured in {timings['configure_seconds']:.2f}s"
            )

            if cfg.target not in targets:
                raise ValueError(
                    f"Target '{cfg.target}' not found in project! Available: {', '.join(targets.keys())}"
                )

            names = [cfg.target] + (
                [cfg.compare_target] if cfg.compare_target else []
            )

            # Instrument main()
            shutil.copyfile(main_h, root / "benchmark.h")
            if cfg.profile_functions:
                shutil.copyfile(profiler_h, root / "bench_profiler.h")

            changed = {}
            for name in names:
                base_bench.instrument(
                    targets[name],
                    source,
                    root / "benchmark.h",
                    cfg.project_dir,
                    changed,
                )

            # Stage 2: Build
            self.sig_stage.emit(f"Build Targets: {', '.join(names)}", stage_offset + 2, total_stages)
            self.sig_status.emit(f"Building targets: {', '.join(names)}...")
            t_stage = time.monotonic()
            build_args = [
                "cmake",
                "--build",
                str(build),
                "--config",
                "Release",
                "--target",
            ] + names
            build_output = base_bench.command(build_args)
            if build_output.strip():
                self.sig_log.emit(build_output.strip())
            timings["build_seconds"] = time.monotonic() - t_stage
            self.sig_log.emit(
                f"[OK] Build completed in {timings['build_seconds']:.2f}s"
            )

            executables: Dict[str, Path] = {}
            for name in names:
                artifacts = targets[name].get("artifacts", [])
                paths = [
                    (
                        Path(a["path"])
                        if Path(a["path"]).is_absolute()
                        else build / a["path"]
                    )
                    for a in artifacts
                ]
                paths = [
                    p
                    for p in paths
                    if p.is_file()
                    and p.suffix.lower() not in (".pdb", ".lib", ".exp")
                ]
                if not paths:
                    raise ValueError(f"No executable found for target {name}")
                executables[name] = paths[0]

            # Stage 3: Deploy & Measure
            self.sig_stage.emit("Run Benchmark & Samples", stage_offset + 3, total_stages)
            self.sig_status.emit("Executing benchmark runs...")
            t_stage = time.monotonic()

            report_targets: Dict[str, Any] = {
                name: {"samples": [], "warmups": []} for name in names
            }

            # Function profile result — captured independently per target
            profile_data: Dict[str, Any] = {}
            profile_json_paths = {n: root / f"profile_{n}.json" for n in names}
            profile_captured = {n: False for n in names}

            # Execute runs — deploy to ADB device if in ADB mode
            remote_root = None
            remote_executables: Dict[str, str] = {}
            if cfg.adb:
                remote_root = f"/data/local/tmp/main-bench-{secrets.token_hex(8)}"
                base_bench.adb_command(adb, serial, "shell", "mkdir", "-p", remote_root)
                self.sig_log.emit(f"[INFO] Deployed to ADB device ({serial}) at {remote_root}")
                for name in names:
                    remote_exe = f"{remote_root}/{name}"
                    base_bench.adb_command(adb, serial, "push", str(executables[name]), remote_exe)
                    base_bench.adb_command(adb, serial, "shell", "chmod", "755", remote_exe)
                    remote_executables[name] = remote_exe

            cwd = cfg.project_dir
            try:
                for name in names:
                    exe = executables[name]

                    # 1. Warmups
                    for w in range(cfg.warmup):
                        if self._is_cancelled:
                            return {}
                        self.sig_status.emit(
                            f"Warmup {w+1}/{cfg.warmup} for {name}..."
                        )
                        if cfg.adb:
                            remote_rec = f"{remote_root}/warmup-{name}-{w}.txt"
                            res = base_bench.measure_adb(
                                adb, serial, remote_executables[name], cfg.program_args, remote_rec, cfg.timeout
                            )
                        else:
                            record = root / f"{name}-warmup-{w}.txt"
                            res = base_bench.measure(
                                exe, cfg.program_args, cwd, record, cfg.timeout
                            )
                        report_targets[name]["warmups"].append(res)
                        self.sig_sample.emit(
                            {
                                "target": name,
                                "phase": "warmup",
                                "iteration": w + 1,
                                "elapsed_ns": res.get("elapsed_ns", 0),
                            }
                        )

                    # 2. Measured runs
                    consecutive_passes = 0
                    sample_idx = 0
                    while sample_idx < (cfg.runs or cfg.max_runs):
                        if self._is_cancelled:
                            return {}
                        sample_idx += 1
                        if cfg.runs is None:
                            self.sig_status.emit(
                                f"Run {sample_idx} (adaptive, min {cfg.min_runs}) for {name}..."
                            )
                        else:
                            self.sig_status.emit(
                                f"Run {sample_idx}/{cfg.runs} for {name}..."
                            )

                        want_profile = (
                            cfg.profile_functions
                            and sample_idx == 1
                            and not profile_captured[name]
                        )

                        if cfg.adb:
                            remote_rec = f"{remote_root}/sample-{name}-{sample_idx}.txt"
                            extra_env = {}
                            if want_profile:
                                remote_prof = f"{remote_root}/profile_{name}.json"
                                extra_env["MAIN_BENCH_PROFILE_RESULT"] = remote_prof
                                profile_captured[name] = True

                            res = base_bench.measure_adb(
                                adb, serial, remote_executables[name], cfg.program_args,
                                remote_rec, cfg.timeout, extra_env=extra_env
                            )
                            if want_profile:
                                base_bench.adb_command(
                                    adb, serial, "pull", remote_prof, str(profile_json_paths[name]), check=False
                                )
                        else:
                            record = root / f"{name}-sample-{sample_idx}.txt"
                            env_override = os.environ.copy()
                            if want_profile:
                                env_override["MAIN_BENCH_PROFILE_RESULT"] = str(
                                    profile_json_paths[name]
                                )
                                profile_captured[name] = True

                            res = base_bench.measure(
                                exe,
                                cfg.program_args,
                                cwd,
                                record,
                                cfg.timeout,
                                extra_env=env_override,
                            )

                        report_targets[name]["samples"].append(res)

                        elapsed_ns = res.get("elapsed_ns", 0)
                        self.sig_sample.emit(
                            {
                                "target": name,
                                "phase": "sample",
                                "iteration": sample_idx,
                                "elapsed_ns": elapsed_ns,
                            }
                        )

                        # Check adaptive stability if adaptive mode
                        if cfg.runs is None and sample_idx >= cfg.min_runs:
                            valid_vals = [
                                s["elapsed_ns"]
                                for s in report_targets[name]["samples"]
                                if "error" not in s
                            ]
                            stab = base_bench.stability(valid_vals, cfg.precision)
                            self.sig_stability.emit(stab)
                            if stab["passed"]:
                                consecutive_passes += 1
                                if consecutive_passes >= 3:
                                    self.sig_log.emit(
                                        f"[OK] Target {name} reached stability after {sample_idx} runs."
                                    )
                                    break
                            else:
                                consecutive_passes = 0

                            if (
                                time.monotonic() - t_stage
                            ) > cfg.max_time:
                                self.sig_log.emit(
                                    f"[INFO] Target {name} reached max time budget ({cfg.max_time}s)."
                                )
                                break
            finally:
                if cfg.adb and remote_root:
                    base_bench.adb_command(adb, serial, "shell", "rm", "-rf", remote_root, check=False)

            timings["collection_seconds"] = time.monotonic() - t_stage

            # Stage 4: Analysis & Profiling
            self.sig_stage.emit("Analyze Functions & Syscalls", stage_offset + 4, total_stages)
            self.sig_status.emit("Analyzing function breakdown & syscalls...")

            # Load profile JSON for primary target
            primary_profile_path = profile_json_paths[cfg.target]
            if primary_profile_path.is_file():
                try:
                    profile_data = json.loads(
                        primary_profile_path.read_text(encoding="utf-8")
                    )
                    self.sig_log.emit(
                        f"[OK] Loaded function profile: {len(profile_data.get('functions', []))} functions, {len(profile_data.get('tree', []))} call-tree nodes"
                    )
                except Exception as e:
                    self.sig_log.emit(
                        f"[WARN] Failed to parse profile JSON: {e}"
                    )

            # Symbolize unknown addresses using host resolver
            resolver = SymbolResolver(executables[cfg.target])
            for fn in profile_data.get("functions", []):
                if fn.get("name", "").startswith("0x"):
                    resolved = resolver.resolve(fn["name"])
                    if resolved:
                        fn["name"] = resolved
                fn["name"] = clean_cpp_name(fn.get("name", ""))
            for node in profile_data.get("tree", []):
                if node.get("name", "").startswith("0x"):
                    resolved = resolver.resolve(node["name"])
                    if resolved:
                        node["name"] = resolved
                node["name"] = clean_cpp_name(node.get("name", ""))

            # Filter out standard library and internal noise from the functions table
            def is_noise(name: str) -> bool:
                if not name: return True
                if name.startswith("__"): return True
                # Match std:: at start or preceded by non-identifier char (like &, *, space, <)
                if re.search(r'(^|[^A-Za-z0-9_])std::', name): return True
                if re.search(r'(^|[^A-Za-z0-9_])__gnu_cxx::', name): return True
                return False

            profile_data["functions"] = [
                fn for fn in profile_data.get("functions", [])
                if not is_noise(fn.get("name", ""))
            ]
            
            # Optional: Collapse noise nodes in the tree to clean up the flame graph a bit
            for node in profile_data.get("tree", []):
                if is_noise(node.get("name", "")):
                    node["name"] = "[STL Internal]"

            # Syscall tracing
            syscall_data: Dict[str, Any] = {
                "available": False,
                "summary": {},
                "by_function": {},
                "total_syscalls": 0,
            }
            if cfg.trace_syscalls and not cfg.adb:
                tracer = SyscallTracer(executables[cfg.target], cfg.target)
                if tracer.is_available():
                    self.sig_status.emit("Tracing system calls with strace...")
                    syscall_data = tracer.trace_local(
                        cfg.program_args, cwd=cfg.project_dir, timeout=15.0
                    )
                    self.sig_log.emit(
                        f"[OK] Syscall trace complete: {syscall_data.get('total_syscalls', 0)} calls captured"
                    )

            # Link syscalls to profile functions
            by_func = syscall_data.get("by_function", {})
            for fn in profile_data.get("functions", []):
                fname = fn.get("name", "")
                for sym, sc in by_func.items():
                    if sym in fname or fname in sym:
                        fn["syscalls"] = sc
                        break

            # Compute summary stats
            for name, data in report_targets.items():
                vals = [
                    s["elapsed_ns"]
                    for s in data["samples"]
                    if "error" not in s
                ]
                data["summary"] = base_bench.summarize(vals)
                if len(vals) >= 2:
                    stab = base_bench.stability(vals, cfg.precision)
                    data["summary"][
                        "relative_standard_error_percent"
                    ] = stab.get("relative_standard_error_percent")
                    data["summary"]["median_drift_percent"] = stab.get(
                        "median_drift_percent"
                    )
                    data["summary"]["passed"] = stab.get("passed")

            # Comparison calculation
            comparison = None
            if (
                cfg.compare_target
                and cfg.compare_target in report_targets
                and report_targets[cfg.target].get("summary")
                and report_targets[cfg.compare_target].get("summary")
            ):
                m1 = report_targets[cfg.target]["summary"]["median_ns"]
                m2 = report_targets[cfg.compare_target]["summary"]["median_ns"]
                fastest = cfg.target if m1 <= m2 else cfg.compare_target
                ratio = (m2 / m1) if m1 > 0 else 1.0
                comparison = {
                    "target_a": cfg.target,
                    "target_b": cfg.compare_target,
                    "median_a_ns": m1,
                    "median_b_ns": m2,
                    "fastest": fastest,
                    "speedup": ratio,
                }

            timings["total_seconds"] = time.monotonic() - run_start

            primary_summary = (
                report_targets.get(cfg.target, {}).get("summary") or {}
            )

            full_report = {
                "project": str(cfg.project_dir),
                "target": cfg.target,
                "compare": cfg.compare_target,
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "runs": len(report_targets[cfg.target]["samples"]),
                "warmup_runs": len(report_targets[cfg.target]["warmups"]),
                "summary": primary_summary,
                "timings": timings,
                "targets": report_targets,
                "profile": profile_data,
                "syscalls": syscall_data,
                "comparison": comparison,
            }

            self.sig_status.emit("Benchmark complete!")
            self.sig_log.emit(
                f"[DONE] Benchmark finished in {timings['total_seconds']:.2f}s total"
            )
            return full_report


def scan_cmake_targets(project_dir: Path | str) -> List[str]:
    """Inspects a CMake project to list available executable targets."""
    proj = Path(project_dir)
    cm_file = proj / "CMakeLists.txt"
    if not cm_file.is_file():
        return []

    executables: List[str] = []
    try:
        # Fast regex scan of CMakeLists.txt files in project
        pattern = re.compile(
            r"add_executable\s*\(\s*([A-Za-z0-9_.-]+)", re.IGNORECASE
        )
        for p in [cm_file] + list(proj.glob("**/CMakeLists.txt"))[:10]:
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
                for match in pattern.finditer(txt):
                    name = match.group(1)
                    if (
                        not name.startswith("$")
                        and name not in executables
                    ):
                        executables.append(name)
            except Exception:
                pass
    except Exception:
        pass

    return executables or ["read_file_on_device_cpp"]


def scan_adb_devices() -> List[str]:
    """Lists connected and authorized ADB device serials."""
    adb = shutil.which("adb")
    if not adb:
        return []
    try:
        res = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=3.0
        )
        devices = []
        for line in res.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices
    except Exception:
        return []
