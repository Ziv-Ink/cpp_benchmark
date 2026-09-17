#ifndef MAIN_BENCHMARK_TIMER_H
#define MAIN_BENCHMARK_TIMER_H
#include <chrono>
#include <cstdio>
#include <cstdlib>

namespace bench {
class MainTimer {
    using Clock = std::chrono::steady_clock;
    Clock::time_point start_ = Clock::now();
public:
    MainTimer() = default;
    MainTimer(const MainTimer&) = delete;
    MainTimer& operator=(const MainTimer&) = delete;
    ~MainTimer() noexcept {
        const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
            Clock::now() - start_).count();
        // File operations occur after the timing boundary.
        const char* path = std::getenv("MAIN_BENCH_RESULT");
        if (!path) return;
        std::FILE* output = std::fopen(path, "w");
        if (!output) return;
        std::fprintf(output, "%lld\n", static_cast<long long>(elapsed));
        std::fclose(output);
    }
};
}
#endif
