#!/usr/bin/env python3
"""Data exporter for benchmark, function profiling, and syscall traces.

Supports JSON, CSV, and self-contained interactive HTML reports.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Any, List


def format_duration(ns: int | float) -> str:
    if ns is None or ns < 0:
        return "0 ns"
    if ns < 1_000:
        return f"{ns:.0f} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    return f"{ns / 1_000_000_000:.3f} s"


def export_json(data: Dict[str, Any], filepath: Path | str) -> Path:
    """Exports full benchmark, profile, and syscall data to formatted JSON."""
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return p


def export_csv(data: Dict[str, Any], filepath: Path | str) -> Path:
    """Exports function-level breakdown and syscall attribution to CSV."""
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)

    functions = data.get("profile", {}).get("functions", [])
    total_time = max(
        1,
        data.get("profile", {}).get("total_duration_ns", 0)
        or sum(f.get("self_ns", 0) for f in functions),
    )

    fieldnames = [
        "Rank",
        "Function",
        "Calls",
        "Total Time (ns)",
        "Self Time (ns)",
        "% Total",
        "% Self",
        "Avg Time (ns)",
        "Min Time (ns)",
        "Max Time (ns)",
        "Syscall Count",
        "Syscalls Breakdown",
    ]

    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        # Sort functions by self_ns descending
        sorted_funcs = sorted(
            functions, key=lambda item: item.get("self_ns", 0), reverse=True
        )
        for rank, fn in enumerate(sorted_funcs, 1):
            tot = fn.get("total_ns", 0)
            self_ns = fn.get("self_ns", 0)
            calls = fn.get("calls", 1)
            sc = fn.get("syscalls", {})
            sc_count = sum(sc.values())
            sc_detail = "; ".join(f"{k}:{v}" for k, v in sc.items())

            writer.writerow(
                {
                    "Rank": rank,
                    "Function": fn.get("name", "unknown"),
                    "Calls": calls,
                    "Total Time (ns)": tot,
                    "Self Time (ns)": self_ns,
                    "% Total": f"{(tot / total_time * 100):.2f}%",
                    "% Self": f"{(self_ns / total_time * 100):.2f}%",
                    "Avg Time (ns)": (
                        f"{(tot / calls):.1f}" if calls > 0 else "0"
                    ),
                    "Min Time (ns)": fn.get("min_ns", 0),
                    "Max Time (ns)": fn.get("max_ns", 0),
                    "Syscall Count": sc_count,
                    "Syscalls Breakdown": sc_detail,
                }
            )

    return p


def export_html_report(data: Dict[str, Any], filepath: Path | str) -> Path:
    """Generates an all-in-one interactive HTML report with charts and flame graph."""
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(data)

    target_name = data.get("target", "Target")
    compare_name = data.get("compare")
    summary = data.get("summary", {})
    timings = data.get("timings", {})
    profile = data.get("profile", {})
    functions = profile.get("functions", [])
    tree = profile.get("tree", [])

    median_str = format_duration(summary.get("median_ns", 0))
    mean_str = format_duration(summary.get("mean_ns", 0))
    min_str = format_duration(summary.get("min_ns", 0))
    max_str = format_duration(summary.get("max_ns", 0))
    rse_str = f"{summary.get('relative_standard_error_percent', 0):.2f}%"
    drift_str = f"{summary.get('median_drift_percent', 0):.2f}%"
    runs_count = summary.get("runs", data.get("runs", 0))

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Benchmark & Profile Report - {target_name}</title>
<style>
  :root {{
    --bg-dark: #12151b;
    --bg-card: #1c202a;
    --border-color: #2e3546;
    --text-main: #f1f5f9;
    --text-muted: #94a3b8;
    --accent-blue: #3b82f6;
    --accent-emerald: #10b981;
    --accent-amber: #f59e0b;
    --accent-rose: #f43f5e;
  }}
  body {{
    margin: 0;
    padding: 24px;
    background: var(--bg-dark);
    color: var(--text-main);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .container {{ max-width: 1280px; margin: 0 auto; }}
  h1 {{ margin: 0 0 8px 0; font-size: 24px; color: #fff; }}
  .subtitle {{ color: var(--text-muted); font-size: 14px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 16px 20px;
  }}
  .card-label {{ font-size: 12px; font-weight: 600; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }}
  .card-val {{ font-size: 24px; font-weight: 700; margin: 8px 0 4px 0; color: #fff; }}
  .card-sub {{ font-size: 12px; color: var(--accent-emerald); }}
  
  .tabs {{ display: flex; gap: 8px; border-bottom: 1px solid var(--border-color); margin-bottom: 16px; }}
  .tab-btn {{
    background: none; border: none; padding: 10px 18px; color: var(--text-muted);
    font-size: 14px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent;
  }}
  .tab-btn.active {{ color: var(--accent-blue); border-bottom-color: var(--accent-blue); }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }}
  th {{ background: #161b24; color: var(--text-muted); padding: 10px 12px; border-bottom: 1px solid var(--border-color); font-weight: 600; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #232a38; }}
  tr:hover td {{ background: #222836; }}
  .bar-container {{ width: 100px; height: 8px; background: #232a38; border-radius: 4px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 8px; }}
  .bar-fill {{ height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); }}

  #flame-container {{ background: #181b20; border-radius: 8px; padding: 12px; border: 1px solid var(--border-color); overflow-x: auto; }}
  .flame-row {{ display: flex; height: 26px; margin-bottom: 2px; }}
  .flame-box {{
    height: 100%; background: #e06c75; border-radius: 3px; font-size: 11px;
    display: flex; align-items: center; padding: 0 6px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; cursor: pointer; color: #fff; border: 1px solid rgba(0,0,0,0.2);
    box-sizing: border-box; transition: transform 0.1s;
  }}
  .flame-box:hover {{ filter: brightness(1.25); transform: translateY(-1px); }}
</style>
</head>
<body>
<div class="container">
  <h1>Benchmark & Function Profiling Report</h1>
  <div class="subtitle">Target: <b>{target_name}</b> {f"vs <b>{compare_name}</b>" if compare_name else ""} • Project: {data.get("project", "")}</div>

  <div class="grid">
    <div class="card">
      <div class="card-label">Median Latency</div>
      <div class="card-val">{median_str}</div>
      <div class="card-sub">Mean: {mean_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Range [Min - Max]</div>
      <div class="card-val">{min_str}</div>
      <div class="card-sub">Max: {max_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Precision & Drift</div>
      <div class="card-val">{rse_str} RSE</div>
      <div class="card-sub">{drift_str} median drift</div>
    </div>
    <div class="card">
      <div class="card-label">Measured Runs</div>
      <div class="card-val">{runs_count}</div>
      <div class="card-sub">Warmups: {data.get("warmup_runs", 0)}</div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 24px;">
    <div class="card-label" style="margin-bottom: 12px;">Stage Breakdown</div>
    <div style="display: flex; height: 24px; border-radius: 6px; overflow: hidden; background: #232a38; font-size: 11px; font-weight: 600; text-align: center; line-height: 24px;">
      <div style="background: #3b82f6; flex: {max(1, int(timings.get('configure_seconds', 1)*1000))};" title="Configure: {timings.get('configure_seconds', 0):.2f}s">Configure</div>
      <div style="background: #8b5cf6; flex: {max(1, int(timings.get('build_seconds', 1)*1000))};" title="Build: {timings.get('build_seconds', 0):.2f}s">Build</div>
      <div style="background: #10b981; flex: {max(1, int(timings.get('collection_seconds', 1)*1000))};" title="Collection: {timings.get('collection_seconds', 0):.2f}s">Measure</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-functions')">Function Breakdown</button>
    <button class="tab-btn" onclick="switchTab('tab-flame')">Flame Graph</button>
    <button class="tab-btn" onclick="switchTab('tab-syscalls')">Syscalls</button>
    <button class="tab-btn" onclick="switchTab('tab-raw')">Raw JSON</button>
  </div>

  <div id="tab-functions" class="card">
    <table>
      <thead>
        <tr>
          <th>Function</th>
          <th>Calls</th>
          <th>Total Time</th>
          <th>Self Time</th>
          <th>% Self</th>
          <th>Syscalls</th>
        </tr>
      </thead>
      <tbody>
"""

    tot_dur = max(
        1,
        profile.get("total_duration_ns", 0)
        or sum(f.get("self_ns", 0) for f in functions),
    )
    for fn in sorted(
        functions, key=lambda item: item.get("self_ns", 0), reverse=True
    ):
        name = fn.get("name", "unknown")
        calls = fn.get("calls", 1)
        tot = format_duration(fn.get("total_ns", 0))
        self_ns = fn.get("self_ns", 0)
        self_str = format_duration(self_ns)
        pct = (self_ns / tot_dur) * 100.0
        sc = fn.get("syscalls", {})
        sc_str = ", ".join(f"{k}:{v}" for k, v in sc.items()) if sc else "—"

        html_content += f"""
        <tr>
          <td style="font-family: monospace; color: #60a5fa;">{name}</td>
          <td>{calls:,}</td>
          <td>{tot}</td>
          <td><b>{self_str}</b></td>
          <td>
            <div class="bar-container"><div class="bar-fill" style="width: {min(100, pct):.1f}%;"></div></div>
            {pct:.1f}%
          </td>
          <td>{sc_str}</td>
        </tr>"""

    html_content += f"""
      </tbody>
    </table>
  </div>

  <div id="tab-flame" class="card" style="display: none;">
    <div id="flame-container">
      <p style="color: var(--text-muted); font-size: 13px;">Hierarchical call tree preview:</p>
      <div id="flame-rows"></div>
    </div>
  </div>

  <div id="tab-syscalls" class="card" style="display: none;">
    <table>
      <thead>
        <tr><th>Syscall</th><th>Count</th><th>Attributed Function</th></tr>
      </thead>
      <tbody>
"""
    syscall_sum = data.get("syscalls", {}).get("summary", {})
    by_func = data.get("syscalls", {}).get("by_function", {})
    for sc_name, count in syscall_sum.items():
        attr_funcs = [
            fname for fname, scs in by_func.items() if sc_name in scs
        ]
        attr_str = ", ".join(attr_funcs) if attr_funcs else "Host / System"
        html_content += f"""<tr><td><b>{sc_name}</b></td><td>{count}</td><td style="font-family: monospace;">{attr_str}</td></tr>"""

    html_content += f"""
      </tbody>
    </table>
  </div>

  <div id="tab-raw" class="card" style="display: none;">
    <pre style="background: #11141a; padding: 16px; border-radius: 8px; overflow: auto; max-height: 500px; font-size: 12px; color: #a5b4fc;">{json.dumps(data, indent=2)}</pre>
  </div>
</div>

<script>
function switchTab(tabId) {{
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-functions').style.display = 'none';
  document.getElementById('tab-flame').style.display = 'none';
  document.getElementById('tab-syscalls').style.display = 'none';
  document.getElementById('tab-raw').style.display = 'none';
  document.getElementById(tabId).style.display = 'block';
}}
</script>
</body>
</html>
"""

    with open(p, "w", encoding="utf-8") as f:
        f.write(html_content)
    return p
