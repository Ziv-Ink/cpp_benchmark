/**
 * Profiler Accuracy Test: Known 30%/70% time split
 *
 * Ground truth (per iteration):
 *   func_a()  busy-spins for ~30ms  → expect ~30% self time
 *   func_b()  busy-spins for ~70ms  → expect ~70% self time
 *   total     ~100ms per iteration
 *
 * Uses CPU busy-wait (not sleep) so the time is spent entirely inside
 * user-code, making self_ns attribution meaningful.
 *
 * __attribute__((noinline)) prevents the compiler from merging or inlining
 * these functions, so the profiler sees clean call graph nodes.
 */

#include <chrono>
#include <cstdio>
#include <cstdint>

using clk = std::chrono::steady_clock;

// Prevent optimizer from removing the spin loop
__attribute__((noinline))
static void busy_spin_ms(int target_ms) {
    auto start = clk::now();
    volatile uint64_t sink = 0;
    while (std::chrono::duration_cast<std::chrono::milliseconds>(
               clk::now() - start).count() < target_ms) {
        sink += sink * 6364136223846793005ULL + 1;
    }
    (void)sink;
}

// Named functions with known time budgets
__attribute__((noinline))
void func_a() {
    busy_spin_ms(30);   // spend 30ms of CPU time here
}

__attribute__((noinline))
void func_b() {
    busy_spin_ms(70);   // spend 70ms of CPU time here
}

__attribute__((noinline))
void run_iteration() {
    func_a();
    func_b();
}

int main() {
    // Warm up clocks
    run_iteration();

    // Time one clean iteration
    auto t0 = clk::now();
    run_iteration();
    auto t1 = clk::now();
    double actual_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    std::printf("[GROUND_TRUTH] total_ms=%.3f  func_a_target_pct=30.0  func_b_target_pct=70.0\n",
                actual_ms);
    return 0;
}
