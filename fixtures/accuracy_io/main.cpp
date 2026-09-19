#include <chrono>
#include <thread>
#include <fstream>

void spin_wait(int ms) {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(ms)) {}
}

void io_work() {
    std::ofstream out("test_io.txt");
    for (int i = 0; i < 10000; ++i) {
        out << "Writing some data to disk " << i << "\n";
    }
    out.close();
}

void sleep_work() {
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
}

int main() {
    spin_wait(10);
    io_work();
    sleep_work();
    return 0;
}
