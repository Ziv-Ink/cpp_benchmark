# Comprehensive Measurement Inaccuracies & Profiler Bug Report

This document compiles the full catalog of measurement inaccuracies, compiler configuration flaws, runtime bugs, and architectural limitations identified in the `cpp_benchmark` suite.

Each issue is documented with its severity, affected files, technical root cause, empirical evidence, and recommended remediation.

---

## Summary Catalog

| ID | Issue Title | Severity | Affected Subsystem | Status |
| :--- | :--- | :--- | :--- | :--- |
| **BUG-01** | Whole-`main()` Scope Timing Wraps Warmup & Process Setup | **Critical** | `benchmark.py`, `benchmark.h` | Open |
| **BUG-02** | Exclusive Self-Time Attribution Collapse on Function Delegation | **Critical** | `bench_profiler.cpp` | Open |
| **BUG-03** | Standard Library Blanket Suppression & Time Stripping | **High** | `benchmark_engine.py` | Mitigated (UI View Filter) |
| **BUG-04** | Duration Normalization Skew via Partial Sibling Denominator | **High** | `benchmark_engine.py` | Partially Mitigated |
| **BUG-05** | CMake Generator Expression Fails on Clang & Android NDK | **High** | `benchmark_engine.py` | Open |
| **BUG-06** | Fixed Probe Overhead (~150ns) & Inlining Suppression | **High** | `bench_profiler.cpp` | Open |
| **BUG-07** | Multithreading Data Races & Crash Under Concurrency | **Critical** | `bench_profiler.cpp` | Open |
| **BUG-08** | Exception Unwinding Desynchronizes Profiler Stack Tracker | **High** | `bench_profiler.cpp`, `benchmark.h` | Open |
| **BUG-09** | Android ADB Big.LITTLE Core Migration & Thermal Drift | **Medium** | `benchmark.py` | Open |
| **BUG-10** | Syscall Attribution to Linker & macOS/Android Incompatibility | **Medium** | `syscall_tracer.py` | Open |
| **BUG-11** | Fixed Call Stack Depth Buffer (512 Frames) Saturated by Recursion | **Medium** | `bench_profiler.cpp` | Open |
| **BUG-12** | ADB Batch Shell Timeout Resolution Floor (1 Second) | **Low** | `benchmark.py` | Open |

---

## Detailed Bug Catalog

### BUG-01: Whole-`main()` Scope Timing Wraps Warmup & Process Setup
* **Severity**: Critical
* **Affected Files**: `benchmark.py#L329-L355`, `benchmark.h#L12-L37`
* **Root Cause**:
  `instrument()` injects `::bench::MainTimer main_bench_scope_timer;` immediately at the opening brace of `int main() {`. Timing begins in `MainTimer()` and concludes in `~MainTimer()` when `main()` exits.
* **Empirical Impact**:
  In `fixtures/accuracy_test/main_split.cpp`, the program performs an internal warmup loop before its timed section:
  ```cpp
  int main() {
      run_iteration(); // Warm up clocks
      auto t0 = clk::now();
      run_iteration(); // Timed iteration (100 ms)
      auto t1 = clk::now();
      ...
  ```
  `MainTimer` measures both iterations, reporting **200.01 ms** instead of the ground-truth **100.00 ms** (+100.01% error). Any program with test data initialization, CLI argument parsing, verification loops, or console output has non-benchmark operations included in its primary latency metric.
* **Recommended Fix**:
  Provide an explicit macro API (e.g. `BENCH_SCOPE_START()` / `BENCH_SCOPE_STOP()`) allowing user code to delineate the timed boundary. If omitted, fall back to whole-`main()`.

---

### BUG-02: Exclusive Self-Time Attribution Collapse on Function Delegation
* **Severity**: Critical
* **Affected Files**: `bench_profiler.cpp#L246-L265`
* **Root Cause**:
  In `__cyg_profile_func_exit`:
  ```cpp
  int64_t elapsed = hook_start - frame.start_ns;
  int64_t self = elapsed - frame.child_ns;
  ```
  When a function calls a helper function, the entire duration of the child is subtracted from `elapsed` to determine `self_ns`.
* **Empirical Impact**:
  In `fixtures/accuracy_test/main_split.cpp`, `func_a()` (30 ms) and `func_b()` (70 ms) delegate their workload to `busy_spin_ms(ms)`. Because `busy_spin_ms` is a separate function, its entire duration is subtracted from `func_a` and `func_b`, leaving them with only **~200 ns** of self-time (**0.0001%** instead of the expected 30% and 70%).
* **Recommended Fix**:
  In addition to strict exclusive self-time, compute **Containment Time** and **Delegated Self-Time** (attributing helper time to the sole caller when a helper is 100% strictly contained within that caller).

---

### BUG-03: Standard Library Blanket Suppression & Time Stripping
* **Severity**: High
* **Affected Files**: `benchmark_engine.py#L656-L674`
* **Root Cause**:
  Legacy engine code applied `is_noise()` regex matching `std::`, `__gnu_cxx`, and `__format` across all function profiles, completely deleting them from `profile_data["functions"]` and renaming all tree nodes to `[STL Internal]`.
* **Empirical Impact**:
  In `main_split.cpp`, `busy_spin_ms()` spent ~195.8 ms in `std::chrono` calls. When stripped, only 4.2 ms remained in the functions table out of a 200 ms run—**97.88% of execution time vanished**, causing `busy_spin_ms` to be displayed as consuming 99.98% self-time. Furthermore, standard library algorithms like `std::sort()` or `std::vector::emplace_back()` were completely hidden.
* **Current Status**:
  **Mitigated**. The engine now classifies functions into `[user]`, `[std_direct]`, `[std_internal]`, and `[runtime]`. Standard library calls are preserved in the data and controlled via a 3-way UI filter (`Direct Standard Calls`, `User Functions Only`, `Show All`).

---

### BUG-04: Duration Normalization Skew via Partial Sibling Denominator
* **Severity**: High
* **Affected Files**: `benchmark_engine.py#L57-L83`
* **Root Cause**:
  `normalize_profile_durations()` calculated:
  ```python
  profiled_ns = max((fn.get("total_ns", 0) for fn in functions), default=0)
  factor = float(measured_ns) / float(profiled_ns)
  ```
  Because `main()` is not instrumented, if `main()` called multiple sibling functions (e.g. `step1(); step2();`), `max(total_ns)` represented only the largest single sibling, NOT the total runtime. Furthermore, if the largest sibling was an STL function that was filtered out, `profiled_ns` picked an arbitrary smaller function, resulting in scaling factors $> 1.0$.
* **Empirical Impact**:
  Scaled sibling functions summed to 150%–200%+ of total measured runtime.
* **Current Status**:
  **Partially Mitigated**. Added fallback to sum root call-tree nodes (`t_roots`). Full resolution requires instrumenting an explicit top-level benchmark root or tracking root exit timestamps.

---

### BUG-05: CMake Generator Expression Fails on Clang & Android NDK
* **Severity**: High
* **Affected Files**: `benchmark_engine.py#L396`, `benchmark_engine.py#L405`
* **Root Cause**:
  The profiler is linked into the build using:
  ```cmake
  set_source_files_properties("${CMAKE_CURRENT_LIST_DIR}/bench_profiler.cpp" PROPERTIES COMPILE_OPTIONS "$<$<CXX_COMPILER_ID:GNU>:-fno-instrument-functions>")
  ```
  On macOS, the compiler ID is `AppleClang` or `Clang`. On Android NDK, the compiler ID is `Clang`. On both platforms, the condition evaluates to an empty string.
* **Empirical Impact**:
  `bench_profiler.cpp` is itself compiled with `-finstrument-functions`. While its top-level C hooks have `__attribute__((no_instrument_function))`, its C++ standard library calls (`std::vector::push_back`, `std::unordered_map::find`, `std::string`, `snprintf`) trigger compiler instrumentation hooks, inducing recursive re-entrancy checks on every single function call.
* **Recommended Fix**:
  Update the generator expression to match Clang as well:
  ```cmake
  COMPILE_OPTIONS "$<$<OR:$<CXX_COMPILER_ID:GNU>,$<CXX_COMPILER_ID:Clang>,$<CXX_COMPILER_ID:AppleClang>>:-fno-instrument-functions>"
  ```
  Or apply `-fno-instrument-functions` unconditionally for all non-MSVC compilers.

---

### BUG-06: Fixed Probe Overhead (~150ns) & Inlining Suppression
* **Severity**: High
* **Affected Files**: `bench_profiler.cpp#L210-L275`, `benchmark_engine.py#L60-L64`
* **Root Cause**:
  `__cyg_profile_func_enter` and `__cyg_profile_func_exit` read the monotonic clock twice each, perform hash table lookups in `g_edge_map`, and push/pop `g_stack`. This incurs a fixed cost of **~100–250 ns per function call**.
  Additionally, passing `-finstrument-functions` causes GCC and Clang to **disable function inlining and vectorization** across all translation units.
* **Empirical Impact**:
  - A coarse function called 1 time taking 100 ms incurs ~200 ns overhead (+0.0002%).
  - A tight utility called 1,000,000 times taking 100 ns per call incurs ~200 ms probe overhead (+200%), completely altering the application's performance characteristics and cache behavior.
* **Recommended Fix**:
  Implement lightweight sampling (e.g. Linux `perf` or macOS `dtrace`/`sample`) as an alternate profiling mode, or allow user code to annotate performance-critical hot loops with `no_instrument_function`.

---

### BUG-07: Multithreading Data Races & Crash Under Concurrency
* **Severity**: Critical
* **Affected Files**: `bench_profiler.cpp#L63-L65`
* **Root Cause**:
  While the call stack buffer is thread-local (`thread_local StackFrame g_stack[MAX_STACK_DEPTH]`), the storage structures are global and completely unguarded:
  ```cpp
  static std::vector<ProfileTreeNode> g_tree;
  static std::unordered_map<void*, FuncStat> g_stats;
  static std::unordered_map<EdgeKey, uint32_t, EdgeKeyHash> g_edge_map;
  ```
  `__cyg_profile_func_enter` and `__cyg_profile_func_exit` mutate `g_tree` and `g_edge_map` concurrently without mutexes, atomic operations, or spinlocks.
* **Empirical Impact**:
  Running `fixtures/accuracy_thread` (4 worker threads) consistently causes segmentation faults, heap corruption in `std::vector::push_back`, or produces corrupted JSON with exit code 1:
  `"Nonzero exit or missing/invalid main timing record"`.
* **Recommended Fix**:
  Collect per-thread call trees into thread-local buffers, and merge them during `bench_profile_finish()` using a mutex or single-threaded post-processing step.

---

### BUG-08: Exception Unwinding Desynchronizes Profiler Stack Tracker
* **Severity**: High
* **Affected Files**: `bench_profiler.cpp#L210-L270`, `benchmark.h#L23-L37`
* **Root Cause**:
  `-finstrument-functions` injects `__cyg_profile_func_exit` into normal function epilogues. When a C++ exception is thrown (`throw std::runtime_error(...)`), stack unwinding bypasses the normal return path.
* **Empirical Impact**:
  In `fixtures/accuracy_exceptions`, `throw_exception()` enters but never exits via the hook. When `catch_exception()` returns, `g_sp` still points to the unpopped frame, popping the wrong frame and attributing subsequent time incorrectly (expected 70 ms, measured 50 ms).
  Furthermore, if `std::exit()` is called, RAII destructors (`~MainTimer()`) are skipped entirely, and no timing record is written.
* **Recommended Fix**:
  Use RAII scope guards within instrumented code, or hook into `__cxa_throw` / `std::set_terminate` / `std::atexit` to flush profiler records upon early exits.

---

### BUG-09: Android ADB Big.LITTLE Core Migration & Thermal Drift
* **Severity**: Medium
* **Affected Files**: `benchmark.py#L619-L731`
* **Root Cause**:
  Android benchmark binaries pushed via ADB execute without CPU affinity binding (`taskset`).
* **Empirical Impact**:
  1. The Linux scheduler migrates threads dynamically between low-power LITTLE cores and high-performance big cores, producing 2x–3x timing variance across samples.
  2. Successive iterations on fanless mobile/e-ink devices trigger thermal throttling, causing clocks to drop over time.
  3. The adaptive sampling loop detects high drift (`median_drift_percent > 6%`), preventing convergence and forcing runs to hit the `max_runs` (200) or `max_time` (30s) limit.
* **Recommended Fix**:
  Prefix remote ADB invocations with `taskset -c <core_id>` (binding to a specific big core) and introduce cooldown sleeps between iterations on battery/thermal-constrained devices.

---

### BUG-10: Syscall Attribution to Linker & macOS/Android Incompatibility
* **Severity**: Medium
* **Affected Files**: `syscall_tracer.py#L143-L162`, `benchmark_engine.py#L748-L764`
* **Root Cause**:
  `SyscallTracer` relies on `strace -k`, which is only supported on Linux. On macOS and Android ADB runs, syscall tracing is disabled outright. On Linux, `strace` traces the entire process lifecycle from dynamic linker startup (`ld.so`) to `_exit`.
* **Empirical Impact**:
  Pre-`main()` loader syscalls (`mmap`, `openat`, `prlimit64`, `brk`) are attributed to application functions. Syscall tables remain empty on macOS and Android.
* **Recommended Fix**:
  Isolate tracing to the benchmark execution phase, and explore `strace` on rooted Android devices or `dtrace`/`dtruss` on macOS.

---

### BUG-11: Fixed Call Stack Depth Buffer (512 Frames) Saturated by Recursion
* **Severity**: Medium
* **Affected Files**: `bench_profiler.cpp#L44`
* **Root Cause**:
  `MAX_STACK_DEPTH` is hardcoded to `512`:
  ```cpp
  constexpr size_t MAX_STACK_DEPTH = 512;
  thread_local StackFrame g_stack[MAX_STACK_DEPTH];
  ```
* **Empirical Impact**:
  In `fixtures/accuracy_recursion` or deeply nested template code (such as AST visitors or recursive parsers), call depths exceeding 512 frames silently stop pushing frames (`if (g_sp < MAX_STACK_DEPTH)`), causing all subsequent child time to be completely lost.
* **Recommended Fix**:
  Use dynamic resizing or log a warning flag in the profiler JSON when `g_sp >= MAX_STACK_DEPTH`.

---

### BUG-12: ADB Batch Shell Timeout Resolution Floor (1 Second)
* **Severity**: Low
* **Affected Files**: `benchmark.py#L690-L731`
* **Root Cause**:
  `measure_adb_batch()` executes multiple runs inside an ADB shell loop using the shell's built-in `$SECONDS` variable:
  ```sh
  start=$SECONDS
  ...
  elapsed=$(( SECONDS - start ))
  ```
* **Empirical Impact**:
  `$SECONDS` updates only once per second. For micro-benchmarks or sub-second timeouts, time-budget checks have integer-second resolution, allowing runs to exceed time limits by up to 999 ms before terminating.
* **Recommended Fix**:
  Use `date +%s%N` or `uptime` in Android shells for millisecond/nanosecond budget tracking.
