#!/usr/bin/env python3
"""Benchmark main() through a temporary, instrumented CMake Release build."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time


class BenchError(Exception):
    pass


class Console:
    """Small terminal formatter with color that degrades cleanly to plain text."""

    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[31m'
    CYAN = '\033[36m'

    def __init__(self, color=None):
        self.interactive = sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = (sys.stdout.isatty() and 'NO_COLOR' not in os.environ and
                      os.environ.get('TERM') != 'dumb') if color is None else color
        self.width = max(32, min(88, shutil.get_terminal_size((88, 24)).columns - 4))
        try:
            '─│┌┐└┘├┤┬┴┼█░'.encode(sys.stdout.encoding or 'utf-8')
            self.unicode = True
        except UnicodeEncodeError:
            self.unicode = False

    def glyph(self, value, fallback):
        return value if self.unicode else fallback

    def styled(self, text, *styles):
        if not self.color or not any(styles):
            return str(text)
        return ''.join(styles) + str(text) + self.RESET

    def title(self, text):
        print('\n  ' + self.styled(text.upper(), self.BOLD, self.CYAN))
        print('  ' + self.styled(self.glyph('─', '-') * self.width, self.DIM))

    def step(self, number, total, text):
        marker = self.styled(f'[{number}/{total}]', self.BOLD, self.CYAN)
        print(f'  {marker} {text}', flush=True)

    def field(self, label, value, indent=2):
        prefix = f'{label:<12} '
        lines = textwrap.wrap(str(value), width=max(16, self.width - len(prefix))) or ['']
        for index, line in enumerate(lines):
            key = prefix if index == 0 else ' ' * len(prefix)
            print(' ' * indent + self.styled(key, self.DIM) + line)

    def progress(self, phase, completed, total, name=''):
        label = 'Warmup' if phase == 'warmups' else 'Measure'
        if not self.interactive:
            if completed == 0:
                print(f'  {label:<12} {total} runs', flush=True)
            elif completed % 5 == 0 or completed == total:
                print(f'  {label:<12} {completed}/{total} completed', flush=True)
            return
        filled = 16 * completed // total
        bar = self.glyph('█', '#') * filled + self.glyph('░', '-') * (16 - filled)
        text = f'  {label:<8} {bar}  {completed}/{total}  {name}'
        print('\r\033[2K' + self.styled(text[:self.width + 2], self.CYAN),
              end='\n' if completed == total else '', flush=True)

    def adaptive_progress(self, count, limit, elapsed, streak, done=False, checks=None, precision=3):
        text = (f'  Measure  {count} runs/target | {elapsed:.1f}s | checks {streak}/3')
        if checks and self.interactive:
            errors = [c['relative_standard_error_percent'] for c in checks.values()]
            drifts = [c['median_drift_percent'] for c in checks.values()]
            error = f'{max(errors):.1f}%' if all(v is not None for v in errors) else 'n/a'
            drift = f'{max(drifts):.1f}%' if all(v is not None for v in drifts) else 'n/a'
            text = (f'  {count} runs | {elapsed:.1f}s | error {error}/{precision:g}%'
                    f' | drift {drift}/{2 * precision:g}% | {streak}/3')
        checkpoint = (count % 5 == 0 and not done) or (done and count % 5 != 0)
        if self.interactive:
            print('\r\033[2K' + self.styled(text[:self.width + 2], self.CYAN),
                  end='\n' if done else '', flush=True)
        elif checkpoint:
            print(text, flush=True)
        if checks and ((self.interactive and done) or (not self.interactive and checkpoint)):
            self.stability_details(checks, precision)

    def stability_details(self, checks, precision):
        for name, check in checks.items():
            details = []
            for label, key, limit in [('error', 'relative_standard_error_percent', precision),
                                      ('drift', 'median_drift_percent', 2 * precision)]:
                value = check[key]
                shown = f'{value:.2f}%' if value is not None else 'unavailable'
                state = 'OK' if value is not None and value <= limit else 'WAIT'
                details.append(f'{label} {shown} / {limit:g}% [{state}]')
            self.field(name, ' | '.join(details))

    def failure(self, message):
        for index, line in enumerate(message.splitlines()):
            self.field('FAILED' if index == 0 else '', self.styled(line, self.RED))

    def table(self, headers, rows):
        """Align plain cell text before styling; wrap long target names to fit."""
        columns = len(headers)
        available = self.width - 3 * columns - 1
        widths = [max(len(str(row[i])) for row in [headers] + rows)
                  for i in range(columns)]
        while sum(widths) > available:
            widest = max(range(columns), key=widths.__getitem__)
            widths[widest] -= 1

        def border(left, middle, right):
            line = left + middle.join(self.glyph('─', '-') * (w + 2) for w in widths) + right
            print('  ' + self.styled(line, self.DIM))

        def cells(row, heading=False):
            wrapped = [textwrap.wrap(str(value), width=w) or ['']
                       for value, w in zip(row, widths)]
            for line in range(max(map(len, wrapped))):
                pieces = []
                for i, (parts, w) in enumerate(zip(wrapped, widths)):
                    value = parts[line] if line < len(parts) else ''
                    padded = value.ljust(w) if i == 0 else value.rjust(w)
                    emphasis = heading or row[0] == 'Median'
                    pieces.append(' ' + self.styled(padded, self.BOLD if emphasis else '',
                                                    self.CYAN if emphasis else '') + ' ')
                edge = self.styled(self.glyph('│', '|'), self.DIM)
                print('  ' + edge + edge.join(pieces) + edge)

        border(self.glyph('┌', '+'), self.glyph('┬', '+'), self.glyph('┐', '+'))
        cells(headers, heading=True)
        border(self.glyph('├', '+'), self.glyph('┼', '+'), self.glyph('┤', '+'))
        for row in rows:
            cells(row)
        border(self.glyph('└', '+'), self.glyph('┴', '+'), self.glyph('┘', '+'))

    def results(self, report):
        self.title('Results')
        names = list(report['targets'])
        rows = []
        for label, key in [('Median', 'median_ns'), ('Mean', 'mean_ns'),
                           ('Min', 'min_ns'), ('Max', 'max_ns'), ('Std dev', 'stddev_ns')]:
            rows.append([label] + [readable((report['targets'][n]['summary'] or {}).get(key))
                                   for n in names])
        variability = [(report['targets'][n]['summary'] or {}).get('variability_percent')
                       for n in names]
        rows.append(['Variability'] + [f'{v:.2f}%' if v is not None else 'unavailable'
                                        for v in variability])
        rows.append(['Valid runs'] + [f"{sum('error' not in s for s in report['targets'][n]['samples'])}/{len(report['targets'][n]['samples'])}"
                                     for n in names])
        headers = ['Metric'] + names
        natural_width = sum(max(len(str(row[i])) for row in [headers] + rows)
                            for i in range(len(headers))) + 3 * len(headers) + 1
        if len(names) > 1 and natural_width > self.width:
            for index, name in enumerate(names, 1):
                self.field('Target', name)
                self.table(['Metric', 'Time / value'], [[row[0], row[index]] for row in rows])
        else:
            self.table(headers, rows)
        print('  ' + self.styled('main() timing; warmups excluded', self.DIM))


def command(args, **kwargs):
    result = subprocess.run([str(x) for x in args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    if result.returncode:
        raise BenchError('Command failed: ' + repr([str(x) for x in args]) + '\n' + result.stdout)
    return result.stdout


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def tokens(text):
    """Return tokens with original offsets, omitting literals/comments/directives."""
    result = []
    i = 0
    conditional = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise BenchError('Unterminated comment')
            i = end + 2
            continue
        if c == '#' and not text[text.rfind('\n', 0, i) + 1:i].strip():
            end = i
            while True:
                end = text.find('\n', end)
                if end < 0:
                    end = len(text)
                    break
                if text[end - 1] != '\\':
                    break
                end += 1
            directive = text[i:end]
            if re.match(r'#\s*(if|ifdef|ifndef)\b', directive):
                conditional += 1
            elif re.match(r'#\s*endif\b', directive):
                conditional -= 1
            if re.match(r'#\s*define\s+main\b', directive):
                raise BenchError('Macro-defined main is unsupported')
            i = end
            continue
        raw = re.match(r'(?:u8|u|U|L)?R"([^\s()\\]{0,16})\(', text[i:])
        if raw:
            closing = ')' + raw.group(1) + '"'
            end = text.find(closing, i + raw.end())
            if end < 0:
                raise BenchError('Unterminated raw string')
            i = end + len(closing)
            continue
        literal = re.match(r'(?:u8|u|U|L)?["\']', text[i:])
        if literal:
            quote = text[i + literal.end() - 1]
            i += literal.end()
            while i < len(text):
                if text[i] == '\\':
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            continue
        word = re.match(r"[A-Za-z_][A-Za-z_0-9]*|[0-9][A-Za-z_0-9'.]*", text[i:])
        value = word.group() if word else c
        result.append((value, i, conditional))
        i += len(value)
    return result


def main_offset(text):
    if '\\\n' in text or '\\\r\n' in text:
        raise BenchError('Line-spliced entrypoint source is unsupported')
    ts = tokens(text)
    found = []
    depth = 0
    for n, (value, offset, conditional) in enumerate(ts):
        if value == 'main' and depth == 0 and n and ts[n - 1][0] == 'int':
            j = n + 1
            if j >= len(ts) or ts[j][0] != '(':
                continue
            balance = 1
            j += 1
            while j < len(ts) and balance:
                balance += (ts[j][0] == '(') - (ts[j][0] == ')')
                j += 1
            if j < len(ts) and ts[j][0] == '{':
                if conditional or ts[j][2]:
                    raise BenchError('Conditionally defined main is unsupported')
                found.append(ts[j][1] + 1)
        depth += (value == '{') - (value == '}')
    if len(found) > 1:
        raise BenchError('Multiple main definitions are unsupported')
    return found[0] if found else None


def copy_project(source, destination):
    def ignore(directory, names):
        skipped = []
        for name in names:
            p = Path(directory) / name
            if name in ('.git', '.hg', '.svn', '__pycache__', 'CMakeFiles'):
                skipped.append(name)
            elif p.is_dir() and (p / 'CMakeCache.txt').exists():
                skipped.append(name)
        return skipped
    shutil.copytree(source, destination, ignore=ignore, symlinks=True)


def configure(source, build, extra):
    query = build / '.cmake/api/v1/query'
    query.mkdir(parents=True)
    (query / 'codemodel-v2').touch()
    (query / 'toolchains-v1').touch()
    args = ['cmake', '-S', str(source), '-B', str(build),
            '-DCMAKE_BUILD_TYPE=Release'] + extra
    command(args)
    reply = build / '.cmake/api/v1/reply'
    index = json.loads(sorted(reply.glob('index-*.json'))[-1].read_text())
    def read_reply(key):
        entry = index['reply'][key]
        if 'jsonFile' not in entry:
            raise BenchError('CMake File API error: ' + str(entry))
        return json.loads((reply / entry['jsonFile']).read_text())
    model = read_reply('codemodel-v2')
    configs = model['configurations']
    config = next((c for c in configs if c['name'] == 'Release'), None)
    if config is None:
        raise BenchError('CMake did not provide a Release configuration')
    targets = {}
    for item in config['targets']:
        target = json.loads((reply / item['jsonFile']).read_text())
        if target['type'] == 'EXECUTABLE':
            targets[target['name']] = target
    return targets, args, read_reply('toolchains-v1')


def instrument(target, source, header, original, changed):
    candidates = []
    for entry in target.get('sources', []):
        p = Path(entry['path'])
        p = p if p.is_absolute() else source / p
        if p.suffix.lower() not in ('.cpp', '.cc', '.cxx', '.c++') or not p.is_file():
            continue
        text = p.read_text(encoding='utf-8')
        offset = main_offset(text)
        if offset is not None:
            if entry.get('isGenerated') or not p.resolve().is_relative_to(source.resolve()):
                raise BenchError('Entrypoint must be a non-generated source inside the project')
            candidates.append((p, text, offset))
    if len(candidates) != 1:
        raise BenchError('Expected one ordinary int main() in target ' + target['name'])
    p, text, offset = candidates[0]
    if p in changed:
        return changed[p]
    location = str(original / p.relative_to(source)).replace('\\', '/').replace('"', '\\"')
    header_path = str(header).replace('\\', '/').replace('"', '\\"')
    inserted = text[:offset] + '\n::bench::MainTimer main_bench_scope_timer;\n' + text[offset:]
    p.write_text('#include "' + header_path + '"\n#line 1 "' + location + '"\n' + inserted,
                 encoding='utf-8')
    info = {'path': str(original / p.relative_to(source)),
            'sha256': hashlib.sha256(text.encode()).hexdigest()}
    changed[p] = info
    return info


def summarize(values):
    if not values:
        return None
    mean = statistics.mean(values)
    dev = statistics.pstdev(values)
    return {'mean_ns': mean, 'median_ns': statistics.median(values),
            'min_ns': min(values), 'max_ns': max(values), 'stddev_ns': dev,
            'variability_percent': dev / mean * 100 if mean else None}


def stability(values, precision):
    """A stopping heuristic, not a confidence interval or an accuracy guarantee."""
    mean = statistics.mean(values)
    middle = len(values) // 2
    first = statistics.median(values[:middle])
    second = statistics.median(values[middle:])
    error = statistics.stdev(values) / math.sqrt(len(values)) / mean * 100 if mean > 0 else None
    drift = abs(second - first) / first * 100 if first > 0 else None
    return {'relative_standard_error_percent': error, 'median_drift_percent': drift,
            'passed': error is not None and drift is not None and
                      error <= precision and drift <= 2 * precision}


def collect_samples(targets, measure_one, options, console, measure_batch=None):
    """Use identical sampling and stopping rules for local and ADB execution."""
    names = list(targets)
    adaptive = options.runs is None
    limit = options.max_runs if adaptive else options.runs
    reason = 'max_runs' if adaptive else 'fixed_runs'
    streak = 0
    checks = {}
    checked_rounds = 0
    measured_start = None
    elapsed = 0.0
    rounds = 0
    warmup_elapsed = 0.0
    for phase, count in [('warmups', options.warmup), ('samples', limit)]:
        phase_start = time.monotonic()
        completed = 0
        total = count * len(names)
        dynamic = adaptive and phase == 'samples'
        if phase == 'samples':
            measured_start = time.monotonic()
        if dynamic:
            console.adaptive_progress(0, limit, 0, 0)
        elif total:
            console.progress(phase, 0, total)
        iteration = 0
        while iteration < count:
            # Finish each comparison round so both targets receive equal samples.
            if dynamic and iteration and time.monotonic() - measured_start >= options.max_time:
                reason = 'time_limit'
                break
            end = min(iteration + (5 if measure_batch else 1), count)
            jobs = [(name, phase, index) for index in range(iteration, end)
                    for name in (names if index % 2 == 0 else list(reversed(names)))]
            remaining = options.max_time - (time.monotonic() - measured_start) if dynamic else None
            samples = (measure_batch(jobs, remaining) if measure_batch else
                       [measure_one(*job) for job in jobs])
            if not samples or len(samples) > len(jobs) or len(samples) % len(names):
                raise BenchError('Incomplete benchmark round returned by measurement runner')
            round_failed = False
            for (name, _, _), sample in zip(jobs, samples):
                targets[name][phase].append(sample)
                round_failed |= 'error' in sample
                completed += 1
                if not dynamic:
                    console.progress(phase, completed, total, name)
            iteration += len(samples) // len(names)
            if phase == 'samples':
                rounds = iteration
                elapsed = time.monotonic() - measured_start
            if adaptive and round_failed:
                reason = 'failed_run'
                break
            if dynamic:
                if rounds >= options.min_runs and rounds % 5 == 0:
                    checks = {name: stability([s['elapsed_ns'] for s in targets[name]['samples']],
                                             options.precision) for name in names}
                    checked_rounds = rounds
                    streak = streak + 1 if all(c['passed'] for c in checks.values()) else 0
                console.adaptive_progress(rounds, limit, elapsed, streak,
                                          checks=checks, precision=options.precision)
                if streak >= 3:
                    reason = 'stable'
                    break
            if len(samples) < len(jobs):
                if not dynamic:
                    raise BenchError('Measurement runner stopped before the requested fixed count')
                reason = 'time_limit'
                break
        if phase == 'warmups':
            warmup_elapsed = time.monotonic() - phase_start
        if dynamic:
            console.adaptive_progress(rounds, limit, elapsed, streak, done=True,
                                      checks=checks, precision=options.precision)
        if adaptive and reason == 'failed_run':
            if console.interactive and not dynamic and completed < total:
                print()
            break
    return {'mode': 'adaptive' if adaptive else 'fixed', 'stop_reason': reason,
            'requested_runs': options.runs, 'actual_runs': rounds,
            'elapsed_seconds': elapsed, 'stable_checks': streak, 'last_check': checks,
            'checked_runs': checked_rounds,
            'warmup_seconds': warmup_elapsed,
            'min_runs': options.min_runs if adaptive else None,
            'max_runs': limit, 'max_time_seconds': options.max_time if adaptive else None,
            'precision_percent': options.precision if adaptive else None}


def readable(ns):
    if ns is None:
        return 'unavailable'
    if ns >= 1_000_000_000:
        return f'{ns / 1_000_000_000:.3f} s'
    if ns >= 1_000_000:
        return f'{ns / 1_000_000:.3f} ms'
    if ns >= 1000:
        return f'{ns / 1000:.3f} us'
    return f'{ns:.2f} ns'


def measure(executable, args, cwd, record, timeout):
    env = dict(os.environ, MAIN_BENCH_RESULT=str(record))
    start = time.perf_counter_ns()
    try:
        process = subprocess.run([str(executable)] + args, cwd=cwd, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=timeout)
        result = {'exit_code': process.returncode,
                  'stdout': process.stdout.decode('utf-8', errors='replace'),
                  'stderr': process.stderr.decode('utf-8', errors='replace')}
    except subprocess.TimeoutExpired:
        return {'error': 'Run timed out', 'elapsed_ns': None}
    result['process_elapsed_ns'] = time.perf_counter_ns() - start
    raw = record.read_text().strip() if record.exists() else ''
    result['elapsed_ns'] = int(raw) if re.fullmatch(r'[0-9]+', raw) else None
    if process.returncode != 0 or result['elapsed_ns'] is None:
        result['error'] = 'Nonzero exit or missing/invalid main timing record'
    return result


def find_ndk(explicit=None):
    if explicit:
        candidates = [explicit]
    else:
        candidates = []
        for variable in ('ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT'):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]))
        sdk = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
        sdk_roots = ([Path(sdk)] if sdk else []) + [
            Path.home() / 'Library/Android/sdk', Path.home() / 'Android/Sdk']
        versions = []
        for root in sdk_roots:
            ndk_root = root / 'ndk'
            if ndk_root.is_dir():
                versions.extend(path for path in ndk_root.iterdir() if path.is_dir())
        def version_key(path):
            return tuple(int(part) if part.isdigit() else -1
                         for part in re.split(r'[.-]', path.name))
        candidates.extend(sorted(versions, key=version_key, reverse=True))
    for candidate in candidates:
        toolchain = candidate / 'build/cmake/android.toolchain.cmake'
        if toolchain.is_file():
            return candidate.resolve(), toolchain.resolve()
    raise BenchError('Android NDK not found; use --ndk or set ANDROID_NDK_HOME')


def adb_command(adb, serial, *args, check=True, timeout=None):
    invocation = [adb, '-s', serial] + list(args)
    result = subprocess.run(invocation, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace')
        raise BenchError('ADB command failed: ' + repr(invocation) + '\n' + detail)
    return result


def select_adb_device(adb, requested):
    result = subprocess.run([adb, 'devices', '-l'], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise BenchError('adb devices failed: ' + result.stderr)
    devices = {}
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = fields[1]
    if requested:
        if devices.get(requested) != 'device':
            raise BenchError('Requested ADB device is not ready: ' + requested)
        return requested
    ready = [serial for serial, state in devices.items() if state == 'device']
    if len(ready) != 1:
        description = ', '.join(f'{serial} ({state})' for serial, state in devices.items())
        raise BenchError('Expected exactly one ready ADB device; use --device. Found: ' +
                         (description or 'none'))
    return ready[0]


def adb_property(adb, serial, name):
    result = adb_command(adb, serial, 'shell', 'getprop', name)
    return result.stdout.decode('utf-8', errors='replace').strip('\r\n')


def wake_adb_device_if_asleep(adb, serial):
    def wakefulness():
        result = adb_command(adb, serial, 'shell', 'dumpsys', 'power')
        output = result.stdout.decode('utf-8', errors='replace')
        match = re.search(r'^\s*mWakefulness=(\S+)', output, re.MULTILINE)
        if not match:
            raise BenchError('Could not determine ADB device wakefulness')
        return match.group(1)

    if wakefulness() != 'Asleep':
        return False

    adb_command(adb, serial, 'shell', 'input', 'keyevent', 'KEYCODE_POWER')
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if wakefulness() == 'Awake':
            return True
        time.sleep(0.1)
    raise BenchError('ADB device did not wake after pressing the power button')


def prepare_adb(options):
    adb = shutil.which('adb')
    if not adb:
        raise BenchError("'adb' was not found in PATH")
    serial = select_adb_device(adb, options.device)
    woke_device = wake_adb_device_if_asleep(adb, serial)
    abi = adb_property(adb, serial, 'ro.product.cpu.abi')
    if not abi:
        raise BenchError('Could not detect Android device ABI')
    ndk, toolchain = find_ndk(options.ndk.resolve() if options.ndk else None)
    managed = ('CMAKE_TOOLCHAIN_FILE', 'ANDROID_ABI', 'ANDROID_PLATFORM',
               'ANDROID_STL', 'BUILD_TESTING')
    for arg in options.cmake_arg:
        if any(re.match(r'-D' + name + r'(?::[^=]+)?=', arg) for name in managed):
            raise BenchError('--adb manages Android CMake setting in: ' + arg)
    cmake_args = [f'-DCMAKE_TOOLCHAIN_FILE={toolchain}', f'-DANDROID_ABI={abi}',
                  f'-DANDROID_PLATFORM=android-{options.android_api}',
                  '-DANDROID_STL=c++_static', '-DBUILD_TESTING=OFF']
    full_getprop = adb_command(adb, serial, 'shell', 'getprop').stdout.decode(
        'utf-8', errors='replace')
    getenforce = adb_command(adb, serial, 'shell', 'getenforce', check=False)
    metadata = {
        'serial': serial, 'abi': abi, 'model': adb_property(adb, serial, 'ro.product.model'),
        'android_release': adb_property(adb, serial, 'ro.build.version.release'),
        'build_fingerprint': adb_property(adb, serial, 'ro.build.fingerprint'),
        'getenforce': getenforce.stdout.decode('utf-8', errors='replace').strip('\r\n'),
        'getprop': full_getprop, 'ndk': str(ndk), 'adb': adb,
        'woke_device': woke_device,
    }
    return adb, serial, cmake_args, metadata


def measure_adb(adb, serial, remote_executable, args, remote_record, timeout):
    remote_parts = [f'MAIN_BENCH_RESULT={remote_record}', remote_executable] + args
    remote_command = 'cd /data/local/tmp && ' + shlex.join(remote_parts)
    start = time.perf_counter_ns()
    try:
        process = adb_command(adb, serial, 'shell', remote_command,
                              check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': 'Run timed out', 'elapsed_ns': None}
    result = {
        'exit_code': process.returncode,
        'stdout': process.stdout.decode('utf-8', errors='replace'),
        'stderr': process.stderr.decode('utf-8', errors='replace'),
        'process_elapsed_ns': time.perf_counter_ns() - start,
    }
    record = adb_command(adb, serial, 'exec-out', 'cat', remote_record, check=False)
    raw = record.stdout.decode('ascii', errors='replace').strip()
    result['elapsed_ns'] = int(raw) if record.returncode == 0 and re.fullmatch(r'[0-9]+', raw) else None
    adb_command(adb, serial, 'shell', 'rm', '-f', remote_record, check=False)
    if process.returncode != 0 or result['elapsed_ns'] is None:
        result['error'] = 'Nonzero exit or missing/invalid main timing record'
        if record.stderr:
            result['stderr'] += record.stderr.decode('utf-8', errors='replace')
    return result


def parse_adb_batch(payload, expected_count):
    """Read a flat archive in memory; never extract device-provided paths to disk."""
    records = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if name in ('', '.') and member.isdir():
                    continue
                match = re.fullmatch(r'(0|[1-9][0-9]*)\.(ns|rc|out|err)', name)
                if not match or not member.isfile() or name in records:
                    raise BenchError('Unexpected or duplicate ADB batch record: ' + name)
                if int(match[1]) >= expected_count:
                    raise BenchError('ADB batch returned an unexpected sample index')
                records[name] = archive.extractfile(member).read()
    except (tarfile.TarError, EOFError) as error:
        raise BenchError('Invalid or truncated ADB batch archive') from error
    samples = []
    for index in range(expected_count):
        keys = [f'{index}.{suffix}' for suffix in ('ns', 'rc', 'out', 'err')]
        if not any(key in records for key in keys):
            break
        if not all(key in records for key in keys):
            raise BenchError('Incomplete ADB sample record')
        code = records[f'{index}.rc'].strip()
        if not re.fullmatch(rb'[0-9]+', code) or int(code) > 255:
            raise BenchError('Invalid ADB sample exit status')
        raw = records[f'{index}.ns'].strip()
        sample = {'exit_code': int(code),
                  'elapsed_ns': int(raw) if re.fullmatch(rb'[0-9]+', raw) else None,
                  'stdout': records[f'{index}.out'].decode('utf-8', errors='replace'),
                  'stderr': records[f'{index}.err'].decode('utf-8', errors='replace')}
        if sample['exit_code'] or sample['elapsed_ns'] is None:
            sample['error'] = ('Run timed out or was killed (exit 137)' if sample['exit_code'] == 137
                               else 'Nonzero exit or missing/invalid main timing record')
        samples.append(sample)
    if not samples or len(records) != len(samples) * 4:
        raise BenchError('Missing or out-of-order ADB batch samples')
    return samples


def measure_adb_batch(adb, serial, remote_root, executables, args, jobs, timeout,
                      remaining=None, stop_on_failure=True):
    """Run up to five complete rounds in one ADB call, with per-process timeouts."""
    directory = remote_root + '/batch-' + secrets.token_hex(8)
    qdir = shlex.quote(directory)
    # Output streams are separate files, packed once per batch. Arbitrary program
    # output cannot impersonate a timing/status marker. No per-sample cat or rm.
    lines = [f'mkdir {qdir} || exit 1', f'batch={qdir}',
             'trap \'rm -rf "$batch"\' EXIT',
             'cd /data/local/tmp || exit 1', 'SECONDS=0', 'failed=0',
             'run_batch() {']
    previous_round = None
    for index, (name, phase, iteration) in enumerate(jobs):
        this_round = (phase, iteration)
        if this_round != previous_round and previous_round is not None:
            if stop_on_failure:
                lines.append('[ "$failed" = 0 ] || return 0')
            if remaining is not None:
                # Android mksh SECONDS is a built-in elapsed clock with one-second
                # resolution. No external date process is needed per round.
                lines.append(f'[ "$SECONDS" -lt {max(1, math.ceil(remaining))} ] || return 0')
        previous_round = this_round
        prefix = directory + '/' + str(index)
        record, stdout, stderr, status = [shlex.quote(prefix + suffix)
                                        for suffix in ('.ns', '.out', '.err', '.rc')]
        invocation = shlex.join(['toybox', 'timeout', '-s', 'KILL', f'{timeout:.9f}', executables[name]] + args)
        lines.extend([f': > {record} || return 1',
                      f'MAIN_BENCH_RESULT={record} {invocation} </dev/null >{stdout} 2>{stderr}',
                      'rc=$?', f'printf "%s\\n" "$rc" > {status} || return 1',
                      f'ns=; IFS= read -r ns < {record}',
                      'case "$ns" in ""|*[!0-9]*) failed=1;; esac',
                      '[ "$rc" = 0 ] || failed=1'])
    lines.extend(['}', 'run_batch || exit 1', 'toybox tar -cf - -C "$batch" .'])
    try:
        process = adb_command(adb, serial, 'exec-out', 'sh', '-c', '\n'.join(lines),
                              check=False, timeout=len(jobs) * (timeout + 1) + 15)
    except subprocess.TimeoutExpired as error:
        raise BenchError('ADB batch transport timed out; per-process device timeouts remain active') from error
    if process.returncode:
        raise BenchError('ADB batch failed: ' + process.stderr.decode('utf-8', errors='replace'))
    return parse_adb_batch(process.stdout, len(jobs))


def validate_extra(extra):
    for arg in extra:
        if (arg in ('--build', '--install', '--workflow', '--preset', '--fresh') or
                arg.startswith(('-S', '-B', '-C', '-U', '--preset=', '--toolchain=')) or
                re.search(r'CMAKE_(BUILD_TYPE|CONFIGURATION_TYPES|HOME_DIRECTORY|CACHEFILE_DIR)', arg)):
            raise BenchError('Unsupported managed CMake argument: ' + arg)
        if not arg.startswith(('-D', '-G')):
            raise BenchError('Use --cmake-arg=-DNAME=value or --cmake-arg=-GGenerator')


def run():
    run_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--target')
    parser.add_argument('--compare', help='second executable target in the same project')
    parser.add_argument('--runs', type=int, help='fixed measured runs; default: adaptive sampling')
    parser.add_argument('--min-runs', type=int, default=20, help='adaptive minimum per target; default 20')
    parser.add_argument('--max-runs', type=int, default=200, help='adaptive maximum per target; default 200')
    parser.add_argument('--max-time', type=float, default=30,
                        help='adaptive measurement time budget in seconds; default 30 (checked between rounds)')
    parser.add_argument('--precision', type=float, default=3,
                        help='adaptive relative standard error threshold in percent; default 3')
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--json', type=Path)
    color_group = parser.add_mutually_exclusive_group()
    color_group.add_argument('--color', action='store_true', dest='color', default=None,
                             help='always use ANSI colors')
    color_group.add_argument('--no-color', action='store_false', dest='color',
                             help='disable ANSI colors')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--cmake-arg', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--adb', action='store_true',
                        help='cross-compile with CMake and run samples on an ADB device')
    parser.add_argument('--device', help='ADB serial (required when multiple devices are ready)')
    parser.add_argument('--ndk', type=Path, help='Android NDK root')
    parser.add_argument('--android-api', type=int, default=23)
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    options = parser.parse_args(argv[:split])
    console = Console(options.color)
    program_args = argv[split + 1:]
    if ((options.runs is not None and options.runs < 1) or options.warmup < 0 or
            not math.isfinite(options.timeout) or options.timeout <= 0):
        raise BenchError('Require runs >= 1, warmup >= 0, and a positive finite timeout')
    if options.min_runs < 2 or options.max_runs < options.min_runs:
        raise BenchError('Require 2 <= min-runs <= max-runs')
    if any(not math.isfinite(v) or v <= 0 for v in (options.max_time, options.precision)):
        raise BenchError('Require positive finite max-time and precision')
    validate_extra(options.cmake_arg)
    if options.device and not options.adb:
        raise BenchError('--device requires --adb')
    if options.ndk and not options.adb:
        raise BenchError('--ndk requires --adb')
    if options.android_api < 21:
        raise BenchError('--android-api must be at least 21')
    if options.adb and options.cwd:
        raise BenchError('--cwd is only available for local execution')
    original = options.project.resolve()
    cwd = (options.cwd or original).resolve()
    if not (original / 'CMakeLists.txt').is_file():
        raise BenchError('Project must contain CMakeLists.txt')
    if not cwd.is_dir():
        raise BenchError('Working directory does not exist')
    adb = serial = None
    platform_args = []
    device_metadata = None
    if options.adb:
        adb, serial, platform_args, device_metadata = prepare_adb(options)
    report = {'scope': 'main including local destruction; excludes global initialization/destruction',
              'execution': 'adb' if options.adb else 'local',
              'environment': {'os': platform.platform(), 'architecture': platform.machine(),
                              'cpu': platform.processor() or 'unavailable',
                              'cmake': command(['cmake', '--version']).splitlines()[0]},
              'project': str(original), 'cwd': str(cwd), 'arguments': program_args,
              'runs': options.runs, 'warmup_runs': options.warmup, 'targets': {}}
    if device_metadata:
        report['device'] = device_metadata
    timings = report['timings'] = {'setup_seconds': time.monotonic() - run_started}
    console.title('C++ main benchmark')
    console.field('Project', original)
    console.field('Execution', 'ADB device' if options.adb else 'local')
    if options.runs is None:
        console.field('Sampling', f'Adaptive | {options.min_runs}-{options.max_runs} runs per target')
        console.field('Limits', f'{options.max_time:g}s measurement budget | {options.precision:g}% precision heuristic')
        console.field('Warmup', f'{options.warmup} runs per target')
    else:
        console.field('Runs', f'{options.runs} measured + {options.warmup} warmup')
    console.title('Benchmark')
    with tempfile.TemporaryDirectory(prefix='main-benchmark-') as temp:
        root = Path(temp)
        source, build = root / 'source', root / 'build'
        console.step(1, 3, 'Configure Release build')
        stage_start = time.monotonic()
        copy_project(original, source)
        targets, configure_args, toolchains = configure(
            source, build, platform_args + options.cmake_arg)
        report['configure_command'] = configure_args
        timings['configure_seconds'] = time.monotonic() - stage_start
        report['toolchains'] = toolchains
        selected = options.target
        if not selected:
            if len(targets) != 1:
                raise BenchError('Choose --target from: ' + ', '.join(sorted(targets)))
            selected = next(iter(targets))
        names = [selected] + ([options.compare] if options.compare else [])
        if len(set(names)) != len(names):
            raise BenchError('Comparison requires distinct targets')
        for name in names:
            if name not in targets:
                raise BenchError('Unknown target ' + name + '; available: ' + ', '.join(sorted(targets)))
        console.field('Targets', ', '.join(names))
        header = root / 'benchmark.h'
        shutil.copyfile(Path(__file__).with_name('benchmark.h'), header)
        changed = {}
        for name in names:
            info = instrument(targets[name], source, header, original, changed)
            report['targets'][name] = {'source': info, 'samples': [], 'warmups': [],
                                      'compile_groups': targets[name].get('compileGroups', []),
                                      'link': targets[name].get('link', {})}
        build_args = ['cmake', '--build', str(build), '--config', 'Release', '--target'] + names
        report['build_command'] = build_args
        console.step(2, 3, 'Build ' + ', '.join(names))
        stage_start = time.monotonic()
        report['build_output'] = command(build_args)
        timings['build_seconds'] = time.monotonic() - stage_start
        executables = {}
        remote_executables = {}
        for name in names:
            artifacts = targets[name].get('artifacts', [])
            paths = [Path(a['path']) if Path(a['path']).is_absolute() else build / a['path'] for a in artifacts]
            paths = [p for p in paths if p.is_file() and p.suffix.lower() not in ('.pdb', '.lib', '.exp')]
            if len(paths) != 1:
                raise BenchError('Could not identify executable artifact for ' + name)
            executables[name] = paths[0]
            report['targets'][name]['executable_sha256'] = digest(paths[0])
        remote_root = None
        try:
            stage_start = time.monotonic()
            if options.adb:
                remote_root = f'/data/local/tmp/main-benchmark-{secrets.token_hex(8)}'
                adb_command(adb, serial, 'shell', 'mkdir', remote_root)
                report['remote_work_directory'] = remote_root
                for index, name in enumerate(names):
                    safe_name = f'{index}-' + re.sub(r'[^A-Za-z0-9_.-]', '_', name)
                    remote = remote_root + '/' + safe_name
                    adb_command(adb, serial, 'push', str(executables[name]), remote)
                    adb_command(adb, serial, 'shell', 'chmod', '755', remote)
                    remote_executables[name] = remote
                    report['targets'][name]['remote_executable'] = remote
            timings['deploy_seconds'] = time.monotonic() - stage_start if options.adb else 0.0
            console.step(3, 3, 'Run benchmark')
            def measure_one(name, phase, iteration):
                record = root / f'{name}-{phase}-{iteration}.txt'
                return measure(executables[name], program_args, cwd, record, options.timeout)

            def measure_batch(jobs, remaining):
                return measure_adb_batch(adb, serial, remote_root, remote_executables,
                                         program_args, jobs, options.timeout, remaining,
                                         stop_on_failure=options.runs is None)

            report['sampling'] = collect_samples(report['targets'], measure_one, options, console,
                                                measure_batch if options.adb else None)
            report['runs'] = report['sampling']['actual_runs']
            report['sampling']['adb_batch_rounds'] = 5 if options.adb else None
        finally:
            if remote_root:
                adb_command(adb, serial, 'shell', 'rm', '-rf', remote_root,
                            check=False)
        failed = False
        for name, data in report['targets'].items():
            failures = [s for s in data['samples'] + data['warmups'] if 'error' in s]
            failed |= bool(failures)
            values = [s['elapsed_ns'] for s in data['samples'] if 'error' not in s]
            data['summary'] = summarize(values)
        console.results(report)
        stop_labels = {'stable': 'Stable across 3 consecutive checks',
                       'max_runs': 'Run limit reached; stability not established',
                       'time_limit': 'Time limit reached; stability not established',
                       'failed_run': 'Stopped after a failed run',
                       'fixed_runs': 'Fixed run count completed'}
        console.field('Stopped', stop_labels[report['sampling']['stop_reason']])
        console.field('Measured', f"{report['sampling']['actual_runs']} runs per target in "
                                 f"{report['sampling']['elapsed_seconds']:.1f}s")
        if failed:
            console.title('Failed runs')
            for name, data in report['targets'].items():
                for phase in ('warmups', 'samples'):
                    for index, sample in enumerate(data[phase], 1):
                        if 'error' in sample:
                            console.failure(f"{name} / {phase} {index}: {sample['error']}")
                            if sample.get('stderr', '').strip():
                                console.field('stderr', sample['stderr'].strip())
        if len(names) == 2 and not failed and all(report['targets'][n]['summary'] for n in names):
            medians = {n: report['targets'][n]['summary']['median_ns'] for n in names}
            fastest = min(medians, key=medians.get)
            slowest = max(medians, key=medians.get)
            ratio = medians[slowest] / medians[fastest] if medians[fastest] else None
            report['comparison'] = {'fastest': fastest, 'median_speedup': ratio}
            console.title('Comparison')
            if ratio is not None:
                if medians[fastest] == medians[slowest]:
                    console.field('Result', 'Equal median times')
                else:
                    console.field('Fastest', fastest)
                    console.field('Speedup', f'{ratio:.2f}x versus {slowest} (median)')
                console.table(['Target', 'Relative time'],
                              [[n, f'{medians[n] / medians[fastest]:.2f}x'] for n in names])
            else:
                console.field('Status', 'Unavailable: zero median duration')
        timings['warmup_seconds'] = report['sampling']['warmup_seconds']
        timings['collection_seconds'] = report['sampling']['elapsed_seconds']
        timings['main_seconds'] = sum(s['elapsed_ns'] for data in report['targets'].values()
                                      for s in data['samples'] if 'error' not in s) / 1e9
        timings['collection_overhead_seconds'] = max(0.0, timings['collection_seconds'] - timings['main_seconds'])
        timings['total_seconds'] = time.monotonic() - run_started
        console.title('Time breakdown')
        console.table(['Stage', 'Elapsed'],
                      [[label, readable(timings[key] * 1e9)] for label, key in
                       [('Setup', 'setup_seconds'), ('Configure', 'configure_seconds'), ('Build', 'build_seconds'),
                        ('Deploy', 'deploy_seconds'), ('Warmup', 'warmup_seconds'),
                        ('Collection', 'collection_seconds'), ('Timed main()', 'main_seconds'),
                        ('Other collection time', 'collection_overhead_seconds'), ('Total', 'total_seconds')]])
        console.field('Timing', 'Timed main() and other collection time are parts of collection, not additional stages.')
        if options.json:
            options.json.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            console.title('Artifacts')
            console.field('JSON', options.json.resolve())
        print()
        return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(run())
    except (BenchError, OSError, ValueError, KeyError) as error:
        print('benchmark: ' + str(error), file=sys.stderr)
        sys.exit(1)
