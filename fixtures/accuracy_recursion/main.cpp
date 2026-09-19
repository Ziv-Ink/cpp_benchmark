#include <chrono>

void spin_wait(int ms) {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(ms)) {}
}

int recursive_func(int depth) {
    if (depth <= 0) {
        spin_wait(10);
        return 0;
    }
    return 1 + recursive_func(depth - 1);
}

int main() {
    recursive_func(100);
    return 0;
}
