#!/usr/bin/env python3
"""Syscall tracer and function attribution module.

Captures system calls made by native binaries and attributes them
to specific C++ functions using call-stack frames.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


def clean_cpp_name(name: str) -> str:
    """Simplifies verbose C++ demangled names for better readability."""
    if not name:
        return name
    name = re.sub(r'\[abi:[^\]]+\]', '', name)
    name = re.sub(r'std::(__ndk1|__cxx11|__1)::', 'std::', name)
    name = name.replace('std::basic_string<char, std::char_traits<char>, std::allocator<char> >', 'std::string')
    name = name.replace('std::basic_string<char, std::char_traits<char>, std::allocator<char>>', 'std::string')
    name = name.replace('std::basic_string_view<char, std::char_traits<char> >', 'std::string_view')
    name = name.replace('std::basic_string_view<char, std::char_traits<char>>', 'std::string_view')
    name = re.sub(r',\s*std::allocator<[^>]+>\s*', '', name)
    name = re.sub(r',\s*std::default_delete<[^>]+>\s*', '', name)
    return name


class SyscallTracer:
    """Runs a target binary under strace (or parses strace output)

    and attributes syscalls to functions in the target.
    """

    def __init__(self, binary_path: Path | str, target_name: Optional[str] = None):
        self.binary_path = Path(binary_path)
        self.target_name = target_name or self.binary_path.name
        self.strace_path = shutil.which("strace")

    def is_available(self) -> bool:
        return self.strace_path is not None

    def trace_local(
        self,
        args: List[str],
        cwd: Optional[Path | str] = None,
        timeout: float = 30.0,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Runs the binary with strace -k and returns attributed syscalls."""
        if not self.is_available():
            return {
                "available": False,
                "error": "strace binary not found on host",
                "by_function": {},
                "summary": {},
                "total_syscalls": 0,
            }

        cmd_env = os.environ.copy()
        if env:
            cmd_env.update(env)

        # strace -k gives stack backtraces on every syscall
        # -f follows children if any
        cmd = [
            self.strace_path,
            "-k",
            "-f",
            str(self.binary_path),
            *args,
        ]

        try:
            res = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                env=cmd_env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            return self.parse_strace_k_output(res.stderr)
        except subprocess.TimeoutExpired:
            return {
                "available": True,
                "error": f"strace timed out after {timeout}s",
                "by_function": {},
                "summary": {},
                "total_syscalls": 0,
            }
        except Exception as exc:
            return {
                "available": True,
                "error": str(exc),
                "by_function": {},
                "summary": {},
                "total_syscalls": 0,
            }

    def parse_strace_k_output(self, output: str) -> Dict[str, Any]:
        """Parses `strace -k` output and attributes each syscall to the nearest

        recovered user function frame.
        """
        # Lines matching syscall entry/exit:
        # e.g.: [pid 12345] openat(AT_FDCWD, "/path", O_RDONLY) = 3
        # or: openat(AT_FDCWD, "/path", O_RDONLY) = 3
        # or: nanosleep({tv_sec=0, tv_nsec=1000}, NULL) = 0
        syscall_re = re.compile(
            r"^(?:\[pid\s+\d+\]\s+)?([a-zA-Z0-9_]+)\(.*\)\s*=\s*(-?[0-9]+|\?)"
        )
        # Call frame lines:
        # e.g.:  > /path/to/binary(function_name()+0x34) [0x11dd]
        # or:    > /usr/lib/libc.so.6(__open64+0x55) [0x11b445]
        frame_re = re.compile(r"^\s*>\s*([^(]+)\((.*)\)\s*(?:\[.*\])?$")

        by_function: Dict[str, Dict[str, int]] = {}
        summary: Dict[str, int] = {}
        total_syscalls = 0

        current_syscall: Optional[str] = None
        attributed = False

        # Filter out obvious dynamic linker startup syscalls if desired
        target_token = self.target_name

        for line in output.splitlines():
            sm = syscall_re.match(line)
            if sm:
                current_syscall = sm.group(1)
                total_syscalls += 1
                summary[current_syscall] = summary.get(current_syscall, 0) + 1
                attributed = False
                continue

            if current_syscall and not attributed:
                fm = frame_re.match(line)
                if fm:
                    binary_or_lib = fm.group(1).strip()
                    sym = fm.group(2).strip()
                    # Check if frame belongs to user binary or related project dso
                    if target_token in binary_or_lib or (
                        "libc.so" not in binary_or_lib
                        and "ld-linux" not in binary_or_lib
                    ):
                        func_name = re.sub(r"\+0x[0-9a-fA-F]+$", "", sym).strip()
                        if not func_name:
                            func_name = "unknown_user_code"

                        func_name = re.sub(r"\s+", " ", func_name)
                        func_name = clean_cpp_name(func_name)

                        by_func = by_function.setdefault(func_name, {})
                        by_func[current_syscall] = by_func.get(current_syscall, 0) + 1
                        attributed = True

        return {
            "available": True,
            "total_syscalls": total_syscalls,
            "summary": dict(
                sorted(summary.items(), key=lambda item: item[1], reverse=True)
            ),
            "by_function": by_function,
        }
