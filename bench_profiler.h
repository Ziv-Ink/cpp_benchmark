#ifndef BENCH_PROFILER_H
#define BENCH_PROFILER_H

#include <cstdint>
#include <cstddef>

#ifdef __cplusplus
extern "C" {
#endif

// Compiler instrumentation hooks for -finstrument-functions
void __attribute__((no_instrument_function)) __cyg_profile_func_enter(void *this_fn, void *call_site) noexcept;
void __attribute__((no_instrument_function)) __cyg_profile_func_exit(void *this_fn, void *call_site) noexcept;

// Explicit lifecycle controls
void __attribute__((no_instrument_function)) bench_profile_init() noexcept;
void __attribute__((no_instrument_function)) bench_profile_finish() noexcept;
bool __attribute__((no_instrument_function)) bench_profile_is_active() noexcept;

#ifdef __cplusplus
}
#endif

#ifdef __cplusplus
namespace bench {
class ProfileScope {
public:
    ProfileScope() noexcept {
        bench_profile_init();
    }
    ~ProfileScope() noexcept {
        bench_profile_finish();
    }
    ProfileScope(const ProfileScope&) = delete;
    ProfileScope& operator=(const ProfileScope&) = delete;
};
} // namespace bench
#endif

#endif // BENCH_PROFILER_H
