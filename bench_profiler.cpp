#include "bench_profiler.h"

#include <chrono>
#include <vector>
#include <unordered_map>
#include <string>
#include <fstream>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <cxxabi.h>

namespace {

static bool g_active = false;
static thread_local bool g_in_hook = false;

struct StackFrame {
    void* fn;
    int64_t start_ns;
    int64_t child_ns;
    uint32_t tree_index;
};

static constexpr size_t MAX_STACK_DEPTH = 4096;
static thread_local StackFrame g_stack[MAX_STACK_DEPTH];
static thread_local int g_sp = 0;

struct ProfileTreeNode {
    void* fn;
    uint32_t parent;
    uint64_t calls;
    int64_t total_ns;
    int64_t self_ns;
    std::vector<uint32_t> children;
};

struct FuncStat {
    void* fn;
    uint64_t calls = 0;
    int64_t total_ns = 0;
    int64_t self_ns = 0;
    int64_t min_ns = -1;
    int64_t max_ns = 0;
};

struct EdgeKey {
    uint32_t parent;
    void* fn;
    bool operator==(const EdgeKey& o) const noexcept {
        return parent == o.parent && fn == o.fn;
    }
};

struct EdgeKeyHash {
    std::size_t operator()(const EdgeKey& k) const noexcept {
        return std::hash<uint32_t>()(k.parent) ^ (reinterpret_cast<uintptr_t>(k.fn) >> 3);
    }
};

static std::vector<ProfileTreeNode> g_tree;
static std::unordered_map<void*, FuncStat> g_stats;
static std::unordered_map<EdgeKey, uint32_t, EdgeKeyHash> g_edge_map;

inline __attribute__((no_instrument_function)) int64_t now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

__attribute__((no_instrument_function)) std::string resolve_sym(void* addr) {
    Dl_info info;
    std::memset(&info, 0, sizeof(info));
    if (dladdr(addr, &info)) {
        if (info.dli_sname) {
            int status = 0;
            char* demangled = abi::__cxa_demangle(info.dli_sname, nullptr, nullptr, &status);
            std::string res = (status == 0 && demangled) ? demangled : info.dli_sname;
            std::free(demangled);
            return res;
        } else if (info.dli_fbase) {
            char buf[64];
            uintptr_t offset = reinterpret_cast<uintptr_t>(addr) - reinterpret_cast<uintptr_t>(info.dli_fbase);
            std::snprintf(buf, sizeof(buf), "0x%lx", offset);
            return buf;
        }
    }
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%p", addr);
    return buf;
}

__attribute__((no_instrument_function)) std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 16);
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c));
                    out += buf;
                } else {
                    out += c;
                }
                break;
        }
    }
    return out;
}

} // namespace

extern "C" {

__attribute__((no_instrument_function)) void bench_profile_init() noexcept {
    g_in_hook = true;
    g_tree.clear();
    g_stats.clear();
    g_edge_map.clear();
    g_sp = 0;
    // Add root node at index 0
    g_tree.push_back({nullptr, 0, 1, 0, 0, {}});
    g_active = true;
    g_in_hook = false;
}

__attribute__((no_instrument_function)) void bench_profile_finish() noexcept {
    g_active = false;
    g_in_hook = true;

    const char* path = std::getenv("MAIN_BENCH_PROFILE_RESULT");
    if (!path || !path[0]) {
        path = "/tmp/main_bench_profile.json";
    }

    std::ofstream out(path);
    if (!out.is_open()) {
        g_in_hook = false;
        return;
    }

    out << "{\n";
    out << "  \"version\": 1,\n";
    out << "  \"functions\": [\n";
    bool first = true;
    for (const auto& pair : g_stats) {
        if (!first) out << ",\n";
        first = false;
        const auto& s = pair.second;
        std::string name = resolve_sym(s.fn);
        char addr_buf[64];
        std::snprintf(addr_buf, sizeof(addr_buf), "%p", s.fn);

        out << "    {\n";
        out << "      \"address\": \"" << addr_buf << "\",\n";
        out << "      \"name\": \"" << json_escape(name) << "\",\n";
        out << "      \"calls\": " << s.calls << ",\n";
        out << "      \"total_ns\": " << s.total_ns << ",\n";
        out << "      \"self_ns\": " << s.self_ns << ",\n";
        out << "      \"min_ns\": " << (s.min_ns < 0 ? 0 : s.min_ns) << ",\n";
        out << "      \"max_ns\": " << s.max_ns << "\n";
        out << "    }";
    }
    out << "\n  ],\n";

    out << "  \"tree\": [\n";
    for (size_t i = 0; i < g_tree.size(); ++i) {
        if (i > 0) out << ",\n";
        const auto& node = g_tree[i];
        std::string name = node.fn ? resolve_sym(node.fn) : "root";
        char addr_buf[64];
        std::snprintf(addr_buf, sizeof(addr_buf), "%p", node.fn);

        out << "    {\n";
        out << "      \"id\": " << i << ",\n";
        out << "      \"parent\": " << node.parent << ",\n";
        out << "      \"address\": \"" << (node.fn ? addr_buf : "0x0") << "\",\n";
        out << "      \"name\": \"" << json_escape(name) << "\",\n";
        out << "      \"calls\": " << node.calls << ",\n";
        out << "      \"total_ns\": " << node.total_ns << ",\n";
        out << "      \"self_ns\": " << node.self_ns << ",\n";
        out << "      \"children\": [";
        for (size_t c = 0; c < node.children.size(); ++c) {
            if (c > 0) out << ", ";
            out << node.children[c];
        }
        out << "]\n";
        out << "    }";
    }
    out << "\n  ]\n";
    out << "}\n";
    out.close();

    g_in_hook = false;
}

__attribute__((no_instrument_function)) bool bench_profile_is_active() noexcept {
    return g_active;
}

__attribute__((no_instrument_function)) void __cyg_profile_func_enter(void *this_fn, void *call_site) noexcept {
    if (!g_active || g_in_hook) return;
    g_in_hook = true;
    int64_t hook_start = now_ns();
    (void)call_site;

    uint32_t parent_idx = (g_sp > 0) ? g_stack[g_sp - 1].tree_index : 0;
    EdgeKey edge_key{parent_idx, this_fn};
    auto it = g_edge_map.find(edge_key);
    uint32_t node_idx = 0;
    if (it != g_edge_map.end()) {
        node_idx = it->second;
    } else {
        node_idx = static_cast<uint32_t>(g_tree.size());
        g_tree.push_back({this_fn, parent_idx, 0, 0, 0, {}});
        g_tree[parent_idx].children.push_back(node_idx);
        g_edge_map[edge_key] = node_idx;
    }

    int64_t hook_end = now_ns();
    if (g_sp < static_cast<int>(MAX_STACK_DEPTH)) {
        g_stack[g_sp++] = {this_fn, hook_end, 0, node_idx};
    }
    if (g_sp > 1) {
        g_stack[g_sp - 2].child_ns += (hook_end - hook_start);
    }
    g_in_hook = false;
}

__attribute__((no_instrument_function)) void __cyg_profile_func_exit(void *this_fn, void *call_site) noexcept {
    if (!g_active || g_in_hook) return;
    g_in_hook = true;
    int64_t hook_start = now_ns();
    (void)call_site;

    if (g_sp > 0) {
        StackFrame frame = g_stack[--g_sp];
        int64_t elapsed = hook_start - frame.start_ns;
        if (elapsed < 0) elapsed = 0;
        int64_t self = elapsed - frame.child_ns;
        if (self < 0) self = 0;

        if (g_sp > 0) {
            g_stack[g_sp - 1].child_ns += elapsed;
        }

        auto& node = g_tree[frame.tree_index];
        node.calls++;
        node.total_ns += elapsed;
        node.self_ns += self;

        auto& stat = g_stats[this_fn];
        stat.fn = this_fn;
        stat.calls++;
        stat.total_ns += elapsed;
        stat.self_ns += self;
        if (stat.min_ns < 0 || elapsed < stat.min_ns) stat.min_ns = elapsed;
        if (elapsed > stat.max_ns) stat.max_ns = elapsed;

        int64_t hook_end = now_ns();
        if (g_sp > 0) {
            g_stack[g_sp - 1].child_ns += (hook_end - hook_start);
        }
    }
    g_in_hook = false;
}

} // extern "C"
