#include <chrono>
#include <map>
#include <string>
#include <vector>

template <typename T>
void template_func(T value) {
    auto start = std::chrono::steady_clock::now();
    while (std::chrono::steady_clock::now() - start < std::chrono::milliseconds(10)) {}
}

void complex_templates() {
    std::map<std::string, std::vector<int>> my_map;
    for (int i = 0; i < 100; ++i) {
        my_map["key" + std::to_string(i)].push_back(i);
    }
}

int main() {
    template_func(1);
    template_func(1.5);
    template_func(std::string("test"));
    complex_templates();
    return 0;
}
