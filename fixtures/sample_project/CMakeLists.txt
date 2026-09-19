cmake_minimum_required(VERSION 3.15)
project(SampleBenchApp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(compute_lib STATIC compute.cpp compute.h)

add_executable(fast_app main_fast.cpp)
target_link_libraries(fast_app PRIVATE compute_lib)

add_executable(slow_app main_slow.cpp)
target_link_libraries(slow_app PRIVATE compute_lib)
