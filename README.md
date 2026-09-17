# C++ main benchmark

Benchmarks an executable target from an existing CMake project without editing the original source. The tool copies the project to a temporary directory, instruments `main()`, builds the selected target in Release mode through CMake, and reports repeated `std::chrono::steady_clock` measurements.

## Local

```sh
python3 benchmark.py /path/to/project --target app
```

Compare two executable targets:

```sh
python3 benchmark.py /path/to/project \
  --target implementation_a \
  --compare implementation_b
```

## Android device

```sh
python3 benchmark.py /path/to/project \
  --adb \
  --target app \
  --device DEVICE_SERIAL
```

Android mode uses the connected device ABI and an installed Android NDK, cross-compiles through the project's CMake configuration, pushes each executable once, and collects timing from inside `main()` on the device. With exactly one ready device, `--device` can be omitted.

ADB mode collects up to five complete rounds per call, starting a fresh executable
process for every sample. Shell built-ins handle timing records; stdout, stderr,
exit status and timing files are returned in one archive per batch. This avoids
separate ADB fetch/delete calls for every run. The device must provide Android's
`sh` and Toybox `timeout`/`tar`. Each sample retains its own timeout. Archives are
parsed in memory, and missing, malformed or incomplete records cannot count as
successful measurements.

Useful options:

```text
--runs N                 use exactly N measured runs instead of adaptive sampling
--min-runs N             adaptive minimum per target; default 20
--max-runs N             adaptive maximum per target; default 200
--max-time SECONDS       adaptive measurement budget; default 30
--precision PERCENT      adaptive precision threshold; default 3 (use 1 for stricter runs)
--warmup N               discarded warm-up runs; default 2
--json FILE              save raw samples and build metadata
--color                  always use ANSI colors
--no-color               disable ANSI colors
--cmake-arg=-DNAME=value pass a CMake cache setting
--timeout SECONDS        per-run timeout; default 60
--ndk PATH               select an Android NDK
--android-api N          Android API level; default 23
-- ARGS...               arguments passed to the executable
```

Sampling is adaptive by default, locally and over ADB. After at least 20 samples per
target, every fifth round checks whether the relative standard error of the mean is
at most 3% and the medians of the first and second halves differ by at most 6%.
All targets must pass three consecutive checks, so the earliest default stop is
30 samples per target. Zero-duration measurements cannot establish stability.
This is a practical stopping heuristic, not a confidence interval or an accuracy guarantee.

Adaptive sampling stops at 200 runs per target or a 30-second measurement budget,
even if stability has not been established. The budget includes process/ADB overhead,
excludes build/deployment/warmups, and is checked between complete rounds. The host
checks before each batch; the Android shell also checks between rounds using its
one-second-resolution elapsed clock. A batch can overrun the budget by up to roughly
one second plus the current round and transport time. Each target still has its own
`--timeout` (a killed process is reported as exit 137). A short budget can
stop sampling before the minimum. Failed runs stop adaptive sampling after the current
round and return a failure exit code; reaching a limit alone is not an execution failure.
Comparisons alternate target order and collect equal sample counts.

The report states why sampling stopped. JSON includes actual `runs` and a `sampling`
object with the mode, requested count, limits, elapsed time, last stability check,
the sample count used by that check, and stopping reason. `--runs N` bypasses adaptive
stopping and the adaptive time budget.
The CLion ADB configuration uses adaptive sampling automatically because it omits `--runs`.

The terminal report uses bordered statistics tables, side-by-side target comparisons, and
an updating progress bar. Redirected output uses short progress lines instead. Displayed
timings are rounded for readability; `--json` retains the raw nanosecond samples.
Progress shows completed runs, elapsed collection time, and the error/drift values
against their thresholds. A time breakdown separates setup/configure/build/deploy,
warmup, and measured-sample collection. Timed `main()` and the remaining collection
time are components of collection, not extra stages. These values are also stored
in JSON under `timings`. Batched ADB samples omit `process_elapsed_ns`, since the
host measures batch wall time rather than the wall time of individual processes.

Color is enabled automatically in a terminal, unless `NO_COLOR` is set or `TERM=dumb`.
Use `--color` to force colors or `--no-color` to disable them. Table borders fall back to
ASCII when the output encoding does not support Unicode.

Run `python3 benchmark.py --help` for the complete interface. `benchmark.h` must remain beside `benchmark.py`.
