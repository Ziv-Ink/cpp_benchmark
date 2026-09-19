#include "compute.h"
#include <iostream>

int main() {
    int res = 0;
    for (int i = 0; i < 50; ++i) {
        res += fast_compute(10000);
        worker_task();
    }
    if (res == 12345678) std::cout << res << std::endl;
    return 0;
}
