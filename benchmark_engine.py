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


# Updating a visible Qt widget during a fresh-process sweep lets the GUI event
# loop compete with the next benchmark process.  One full adaptive sweep is at
# most 200 samples, so defer those updates until the sweep completes.
SAMPLE_UPDATE_BATCH = 200


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


PROFILE_DURATION_FIELDS = ("total_ns", "self_ns", "min_ns", "max_ns")


def normalize_profile_durations(profile: Dict[str, Any], measured_ns: int | float) -> bool:
    """Scale every displayed profiler duration to the measured median.

    Function profiling is collected in a separate, instrumented process.  It
    preserves call proportions, but its absolute timings include instrumentation
    overhead.  Keeping every duration field on the same scale avoids presenting
    an impossible combination such as a max call longer than its total time.
    """
    functions = profile.get("functions", [])
    profiled_ns = max(
        (fn.get("total_ns", 0) for fn in functions), default=0
    )
    if profile.get("tree"):
        t_roots = [n for n in profile["tree"] if n.get("parent") in (-1, 0) and n.get("id") != 0]
        if t_roots:
            profiled_ns = max(profiled_ns, sum(n.get("total_ns", 0) for n in t_roots))
        elif profile["tree"]:
            profiled_ns = max(profiled_ns, profile["tree"][0].get("total_ns", 0))

    if profiled_ns <= 0 or measured_ns <= 0:
        return False

    factor = float(measured_ns) / float(profiled_ns)
    for collection in (functions, profile.get("tree", [])):
        for entry in collection:
            for field in PROFILE_DURATION_FIELDS:
                value = entry.get(field)
                if isinstance(value, (int, float)) and value >= 0:
                    entry[field] = int(round(value * factor))

    profile["total_duration_ns"] = int(round(measured_ns))
    profile["timing_basis"] = "normalized_profile_estimate"
    return True


def clean_cpp_name(name: str) -> str:
    """Simplifies verbose C++ demangled names for better readability."""
    if not name:
        return name
    # Strip return types from the beginning (e.g. "char8_t* ", "void ")
    # Keep 'operator ...' intact
    if not name.startswith("operator"):
        name = re.sub(r'^[\w\s\*&_]+?\s+([A-Za-z_])', r'\1', name)
    
    # Remove ABI tags
    name = re.sub(r'\[abi:[^\]]+\]', '', name)
    # Simplify NDK / C++11 std:: namespaces (handle it globally)
    name = re.sub(r'std::(?:__ndk1|__cxx11|__1)::', 'std::', name)
    # Simplify common STL types
    name = re.sub(r'std::basic_string<char,\s*std::char_traits<char>,\s*std::allocator<char>\s*>', 'std::string', name)
    name = re.sub(r'std::basic_string_view<char,\s*std::char_traits<char>\s*>', 'std::string_view', name)
    # Remove default allocators, deleters, and comparators
    name = re.sub(r',\s*std::allocator<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::default_delete<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::less<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::equal_to<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::hash<[^>]+>\s*', '', name)
    return name.strip()


def is_std_symbol(name: str) -> bool:
    """Checks whether a demangled symbol belongs to the standard library or compiler support."""
    if not name:
        return False
    return bool(re.search(r'\b(?:std|__gnu_cxx|__detail|__cxxabiv1)\b', name))


def is_runtime_harness(name: str) -> bool:
    """Checks whether a symbol is part of the benchmark profiling harness itself."""
    if not name:
        return False
    return (
        "bench_profile" in name
        or "bench::MainTimer" in name
        or name.startswith("__cyg_profile")
        or "main_bench_scope_timer" in name
    )


def classify_symbol(name: str, parent_category: str = "user") -> str:
    """Classifies a symbol into: 'user', 'std_direct', 'std_internal', or 'runtime'.

    - 'user': Application code written by the developer.
    - 'std_direct': Standard library function called directly from user code (e.g. std::sort, std::vector::push_back).
    - 'std_internal': Implementation details called from within standard functions (e.g. __introsort_loop, __split_buffer).
    - 'runtime': Benchmark runner / profiler harness routines.
    """
    if is_runtime_harness(name):
        return "runtime"
    if not is_std_symbol(name):
        if name.startswith("__") and not name.startswith("__gnu_cxx"):
            return "std_internal" if parent_category in ("std_direct", "std_internal") else "user"
        return "user"

    # Standard library symbol
    if parent_category == "user":
        return "std_direct"
    return "std_internal"


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
    sig_samples = QtCore.pyqtSignal(list)
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
                combined_start = time.monotonic()
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

                # Keep the profile's entire duration vocabulary on the same
                # scale as the independently measured median.
                try:
                    true_time_ns = report_time["targets"][self.config.target]["summary"].get("median_ns", 0)
                    prof_data = report_prof.get("profile", {})
                    if normalize_profile_durations(prof_data, true_time_ns):
                        self.sig_log.emit(f"[OK] Normalized profile times to match true median ({true_time_ns / 1000.0:.2f} µs)")
                    else:
                        self.sig_log.emit("[WARN] Profile did not contain a positive duration to normalize.")
                except Exception as e:
                    self.sig_log.emit(f"[WARN] Could not normalize profile times: {e}")

                report_time["profile"] = report_prof.get("profile", {})
                report_time["syscalls"] = report_prof.get("syscalls", {})
                report_time["timings"]["profile_pass_seconds"] = report_prof.get("timings", {}).get("total_seconds", 0.0)
                report_time["timings"]["total_seconds"] = time.monotonic() - combined_start
                report_time["profile_timing_basis"] = "normalized estimate from one instrumented profile pass"
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
                        adb, None
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
            pending_samples: List[Dict[str, Any]] = []
            pending_status: Optional[str] = None
            pending_stability: Optional[Dict[str, Any]] = None

            def flush_live_updates() -> None:
                """Send one queued UI update for a group of completed processes."""
                nonlocal pending_samples, pending_status, pending_stability
                if pending_status is not None:
                    self.sig_status.emit(pending_status)
                    pending_status = None
                if pending_samples:
                    self.sig_samples.emit(pending_samples)
                    pending_samples = []
                if pending_stability is not None:
                    self.sig_stability.emit(pending_stability)
                    pending_stability = None

            def queue_live_update(status: str, sample: Dict[str, Any]) -> None:
                """Keep UI work out of the per-process measurement cadence."""
                nonlocal pending_status
                pending_status = status
                pending_samples.append(sample)
                if len(pending_samples) >= SAMPLE_UPDATE_BATCH:
                    flush_live_updates()

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
                # 1. Warmups (alternating order when comparing multiple targets)
                for w in range(cfg.warmup):
                    if self._is_cancelled:
                        return {}
                    active_names = names if w % 2 == 0 else list(reversed(names))
                    for name in active_names:
                        exe = executables[name]
                        status = f"Warmup {w+1}/{cfg.warmup} for {name}..."
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
                        queue_live_update(status,
                            {
                                "target": name,
                                "phase": "warmup",
                                "iteration": w + 1,
                                "elapsed_ns": res.get("elapsed_ns") or 0,
                            }
                        )
                        if "error" in res:
                            raise RuntimeError(f"Warmup failed for {name}: {res['error']}")

                # 2. Measured runs (alternating order each round for fair sampling)
                max_limit = cfg.runs or cfg.max_runs
                round_idx = 0
                consecutive_passes = 0
                while round_idx < max_limit:
                    if self._is_cancelled:
                        return {}
                    round_idx += 1
                    active_names = names if (round_idx - 1) % 2 == 0 else list(reversed(names))
                    for name in active_names:
                        exe = executables[name]
                        if cfg.runs is None:
                            status = f"Run {round_idx} (adaptive, min {cfg.min_runs}) for {name}..."
                        else:
                            status = f"Run {round_idx}/{cfg.runs} for {name}..."

                        want_profile = (
                            cfg.profile_functions
                            and round_idx == 1
                            and not profile_captured[name]
                        )

                        if cfg.adb:
                            remote_rec = f"{remote_root}/sample-{name}-{round_idx}.txt"
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
                            record = root / f"{name}-sample-{round_idx}.txt"
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
                        elapsed_ns = res.get("elapsed_ns") or 0
                        queue_live_update(status,
                            {
                                "target": name,
                                "phase": "sample",
                                "iteration": round_idx,
                                "elapsed_ns": elapsed_ns,
                            }
                        )
                        if "error" in res:
                            raise RuntimeError(f"Measured run failed for {name}: {res['error']}")

                    # Check adaptive stability if adaptive mode (every 5 rounds, matching benchmark.py)
                    if cfg.runs is None and round_idx >= cfg.min_runs and round_idx % 5 == 0:
                        all_passed = True
                        for name in names:
                            valid_vals = [
                                s["elapsed_ns"]
                                for s in report_targets[name]["samples"]
                                if "error" not in s and s.get("elapsed_ns") is not None
                            ]
                            stab = base_bench.stability(valid_vals, cfg.precision)
                            if name == cfg.target:
                                pending_stability = stab
                            if not stab.get("passed"):
                                all_passed = False

                        if all_passed:
                            consecutive_passes += 1
                            if consecutive_passes >= 3:
                                self.sig_log.emit(
                                    f"[OK] Targets reached stability after {round_idx} rounds."
                                )
                                break
                        else:
                            consecutive_passes = 0

                        if (time.monotonic() - t_stage) > cfg.max_time:
                            self.sig_log.emit(
                                f"[INFO] Reached max time budget ({cfg.max_time}s)."
                            )
                            break
            finally:
                flush_live_updates()
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

            # Merge ABI-duplicate constructor/destructor entries.
            # The Itanium C++ ABI emits two physical symbols per ctor/dtor:
            #   C1 (complete object ctor) and C2 (base object ctor) — same demangled name.
            # -finstrument-functions instruments both, so C2 appears nested inside C1,
            # making it look like the constructor ran twice. Fix: keep the entry with the
            # largest total_ns (C1, the outer wrapper) and drop the rest.
            seen: dict = {}  # name → best entry so far
            for fn in profile_data.get("functions", []):
                name = fn.get("name", "")
                if name not in seen:
                    seen[name] = fn
                else:
                    existing = seen[name]
                    # Keep the outer (larger total_ns) variant
                    if fn.get("total_ns", 0) > existing.get("total_ns", 0):
                        # Merge syscall data from the smaller entry if any
                        if fn.get("syscalls") is None and existing.get("syscalls"):
                            fn["syscalls"] = existing["syscalls"]
                        seen[name] = fn
                    # else: existing is already the larger; merge syscalls if needed
                    elif existing.get("syscalls") is None and fn.get("syscalls"):
                        existing["syscalls"] = fn["syscalls"]
            profile_data["functions"] = list(seen.values())
            
            # Filter zero-call harness nodes from the flame graph tree
            if profile_data.get("tree"):
                profile_data["tree"] = [
                    node for node in profile_data["tree"]
                    if not (node.get("calls", 0) == 0 and node.get("total_ns", 0) == 0)
                ]

            # Merge ABI-duplicate sibling nodes in the call tree (same parent, same name).
            # Group tree nodes by (parent_id, name); keep the one with the largest total_ns.
            if profile_data.get("tree"):
                best: dict = {}  # (parent, name) → node dict
                keep_ids: set = set()
                remap: dict = {}  # dropped_id → surviving_id
                for node in profile_data["tree"]:
                    key = (node.get("parent", -1), node.get("name", ""))
                    if key not in best:
                        best[key] = node
                        keep_ids.add(node["id"])
                    else:
                        existing = best[key]
                        if node.get("total_ns", 0) > existing.get("total_ns", 0):
                            remap[existing["id"]] = node["id"]
                            keep_ids.discard(existing["id"])
                            keep_ids.add(node["id"])
                            best[key] = node
                        else:
                            remap[node["id"]] = existing["id"]
                # Apply remap to parent references and filter dropped nodes
                surviving = [n for n in profile_data["tree"] if n["id"] in keep_ids]
                for node in surviving:
                    p = node.get("parent")
                    if p in remap:
                        node["parent"] = remap[p]
                profile_data["tree"] = surviving

            # Build tree node map for top-down traversal and classification
            tree_nodes = profile_data.get("tree", [])
            node_map: Dict[int, Dict[str, Any]] = {n["id"]: n for n in tree_nodes}

            # Top-down tree category tagging
            visited_nodes: set = set()
            def _tag_node_category(node_id: int, parent_cat: str):
                if node_id in visited_nodes:
                    return
                visited_nodes.add(node_id)
                n = node_map.get(node_id)
                if not n:
                    return
                if node_id == 0:
                    n["category"] = "runtime"
                    child_pcat = "user"
                else:
                    cat = classify_symbol(n.get("name", ""), parent_cat)
                    n["category"] = cat
                    child_pcat = cat
                for cid in n.get("children", []):
                    _tag_node_category(cid, child_pcat)

            if 0 in node_map:
                _tag_node_category(0, "user")
            for nid in list(node_map.keys()):
                if nid not in visited_nodes:
                    _tag_node_category(nid, "user")

            # Map function names to best category seen across call sites
            name_to_cats: Dict[str, set] = collections.defaultdict(set)
            for n in tree_nodes:
                name_to_cats[n.get("name", "")].add(n.get("category", "user"))

            for fn in profile_data.get("functions", []):
                fname = fn.get("name", "")
                cats = name_to_cats.get(fname, set())
                if "user" in cats:
                    fn["category"] = "user"
                elif "std_direct" in cats:
                    fn["category"] = "std_direct"
                elif "std_internal" in cats:
                    fn["category"] = "std_internal"
                elif "runtime" in cats:
                    fn["category"] = "runtime"
                else:
                    fn["category"] = classify_symbol(fname, "user")

            # Compute containment and caller/callee aggregation
            containment: Dict[str, Any] = {}
            for n in tree_nodes:
                nid = n.get("id", 0)
                if nid == 0:
                    continue
                name = n.get("name", "")
                pid = n.get("parent", 0)
                parent_node = node_map.get(pid)
                parent_name = parent_node.get("name", "Root / Entry") if parent_node and pid != 0 else "Root / Entry"
                parent_dur = parent_node.get("total_ns", 0) if parent_node else n.get("total_ns", 0)

                if name not in containment:
                    containment[name] = {
                        "total_ns": 0,
                        "calls": 0,
                        "callers": {},
                        "callees": {},
                    }
                c_entry = containment[name]
                c_entry["total_ns"] += n.get("total_ns", 0)
                c_entry["calls"] += n.get("calls", 0)

                if parent_name not in c_entry["callers"]:
                    c_entry["callers"][parent_name] = {"calls": 0, "total_ns": 0, "parent_total_ns": parent_dur}
                c_entry["callers"][parent_name]["calls"] += n.get("calls", 0)
                c_entry["callers"][parent_name]["total_ns"] += n.get("total_ns", 0)

                # Record callee in parent's callees
                if parent_name != "Root / Entry":
                    if parent_name not in containment:
                        containment[parent_name] = {"total_ns": 0, "calls": 0, "callers": {}, "callees": {}}
                    p_entry = containment[parent_name]
                    if name not in p_entry["callees"]:
                        p_entry["callees"][name] = {"calls": 0, "total_ns": 0}
                    p_entry["callees"][name]["calls"] += n.get("calls", 0)
                    p_entry["callees"][name]["total_ns"] += n.get("total_ns", 0)

            # Format callers and callees into sorted lists with percentages
            for name, entry in containment.items():
                tot = max(1, entry["total_ns"])
                callers_list = []
                for cname, cdata in entry["callers"].items():
                    callers_list.append({
                        "name": cname,
                        "calls": cdata["calls"],
                        "total_ns": cdata["total_ns"],
                        "pct_of_callee": min(100.0, (cdata["total_ns"] / tot) * 100.0),
                        "pct_of_caller": min(100.0, (cdata["total_ns"] / max(1, cdata["parent_total_ns"])) * 100.0) if cdata["parent_total_ns"] else 0.0,
                    })
                callers_list.sort(key=lambda x: x["total_ns"], reverse=True)
                entry["callers_list"] = callers_list

                callees_list = []
                for cname, cdata in entry["callees"].items():
                    callees_list.append({
                        "name": cname,
                        "calls": cdata["calls"],
                        "total_ns": cdata["total_ns"],
                        "pct_of_parent": min(100.0, (cdata["total_ns"] / tot) * 100.0),
                    })
                callees_list.sort(key=lambda x: x["total_ns"], reverse=True)
                entry["callees_list"] = callees_list

                non_root_callers = [c for c in callers_list if c["name"] != "Root / Entry"]
                if len(non_root_callers) == 1 and non_root_callers[0]["pct_of_callee"] >= 99.0:
                    entry["is_strictly_contained"] = True
                    entry["sole_caller"] = non_root_callers[0]["name"]
                else:
                    entry["is_strictly_contained"] = False
                    entry["sole_caller"] = None

            profile_data["containment"] = containment
            for fn in profile_data.get("functions", []):
                fn["containment"] = containment.get(fn.get("name", ""), {})


            # Syscall tracing
            syscall_data: Dict[str, Any] = {
                "available": False,
                "error": "Syscall tracing was disabled.",
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
                else:
                    syscall_data["error"] = "strace is not available on this host."
                    self.sig_log.emit("[SKIP] Syscall tracing is unavailable: strace was not found on this host.")
            elif cfg.trace_syscalls:
                syscall_data["error"] = "Syscall tracing is not available for Android runs."
                self.sig_log.emit("[SKIP] Syscall tracing is unavailable for Android runs.")

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
                    if "error" not in s and s.get("elapsed_ns") is not None
                ]
                data["summary"] = base_bench.summarize(vals)
                if len(vals) >= 2 and data["summary"]:
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
                slowest = cfg.compare_target if m1 <= m2 else cfg.target
                min_m = min(m1, m2)
                max_m = max(m1, m2)
                ratio = (max_m / min_m) if min_m and min_m > 0 else 1.0
                comparison = {
                    "target_a": cfg.target,
                    "target_b": cfg.compare_target,
                    "median_a_ns": m1,
                    "median_b_ns": m2,
                    "fastest": fastest,
                    "slowest": slowest,
                    "speedup": ratio,
                }

            timings["total_seconds"] = time.monotonic() - run_start

            primary_summary = (
                report_targets.get(cfg.target, {}).get("summary") or {}
            )
            if profile_data and "total_duration_ns" not in profile_data:
                profile_data["total_duration_ns"] = int(primary_summary.get("median_ns", 0) or 0)
                profile_data["timing_basis"] = "instrumented_profile_run"

            primary_samples = report_targets[cfg.target]["samples"]
            valid_samples = [
                sample for sample in primary_samples
                if "error" not in sample and sample.get("elapsed_ns") is not None
            ]

            full_report = {
                "project": str(cfg.project_dir),
                "target": cfg.target,
                "compare": cfg.compare_target,
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "runs": len(valid_samples),
                "attempted_runs": len(primary_samples),
                "invalid_runs": len(primary_samples) - len(valid_samples),
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
