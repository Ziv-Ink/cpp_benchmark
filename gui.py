#!/usr/bin/env python3
"""Entry point for the C++ Benchmark & Profiling Studio GUI."""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from dashboard_gui import main

if __name__ == "__main__":
    main()
