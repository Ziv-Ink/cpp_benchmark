#include "compute.h"
#include <vector>
#include <numeric>
#include <thread>
#include <chrono>

int fast_compute(int n) {
    int sum = 0;
    for (int i = 0; i < n; ++i) {
        sum += (i * 3) ^ (i >> 1);
    }
    return sum;
}

int slow_compute(int n) {
    int sum = 0;
    for (int i = 0; i < n * 3; ++i) {
        sum += (i * 7) ^ (i >> 1);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
    return sum;
}

void worker_task() {
    std::vector<int> v(10000);
    std::iota(v.begin(), v.end(), 1);
    int s = 0;
    for (int x : v) s += x;
    (void)s;
}
