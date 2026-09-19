cmake_minimum_required(VERSION 3.15)
project(AccuracyThread CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)

add_executable(test_thread main.cpp)
target_link_libraries(test_thread PRIVATE Threads::Threads)
