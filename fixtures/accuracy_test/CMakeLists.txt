cmake_minimum_required(VERSION 3.15)
project(AccuracyTest CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# split_30_70: func_a sleeps 30ms (~30%), func_b sleeps 70ms (~70%), total ~100ms per iteration
add_executable(split_30_70 main_split.cpp)
