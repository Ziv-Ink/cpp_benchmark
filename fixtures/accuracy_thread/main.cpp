#include <thread>
#include <vector>
#include <chrono>

void worker_a() {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(20)) {}
}

void worker_b() {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(30)) {}
}

int main() {
    // 4 threads * 50ms = 200ms of work, but wall-clock time is 50ms.
    // The profiler might double count or crash due to concurrent writes.
    std::vector<std::thread> threads;
    for (int i = 0; i < 4; ++i) {
        threads.emplace_back([](){
            worker_a();
            worker_b();
        });
    }
    for (auto& t : threads) {
        t.join();
    }
    return 0;
}
