#include <chrono>
#include <stdexcept>

void do_work() {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(20)) {}
}

void throw_exception() {
    do_work();
    throw std::runtime_error("Early exit");
}

void catch_exception() {
    auto start = std::chrono::steady_clock::now();
    try {
        throw_exception();
    } catch (const std::exception&) {
        // Ignored
    }
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(50)) {}
}

int main() {
    catch_exception();
    return 0;
}
