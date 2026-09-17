"""Deterministic checks of stopping behavior; no compiler or device required."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import benchmark


class SamplingTests(unittest.TestCase):
    def collect(self, values, cost=0, fail=None, batched=False, **overrides):
        options = SimpleNamespace(runs=None, min_runs=20, max_runs=200,
                                  max_time=30, precision=1, warmup=2)
        for key, value in overrides.items():
            setattr(options, key, value)
        targets = {name: {'warmups': [], 'samples': []} for name in values}
        clock = [0.0]
        calls = []

        def measure(name, phase, iteration):
            calls.append((name, phase, iteration))
            clock[0] += cost
            if fail == (name, phase, iteration):
                return {'error': 'Run timed out', 'elapsed_ns': None}
            series = values[name]
            return {'elapsed_ns': series[iteration % len(series)]}

        with patch.object(benchmark.time, 'monotonic', side_effect=lambda: clock[0]), \
                contextlib.redirect_stdout(io.StringIO()):
            batch = (lambda jobs, remaining: [measure(*job) for job in jobs]) if batched else None
            result = benchmark.collect_samples(targets, measure, options, benchmark.Console(False), batch)
        return result, targets, calls

    def test_stable_comparison_and_alternating_order(self):
        result, targets, calls = self.collect({'a': [1000], 'b': [2000]})
        self.assertEqual(result['stop_reason'], 'stable')
        self.assertEqual(result['actual_runs'], 30)
        self.assertEqual([len(t['samples']) for t in targets.values()], [30, 30])
        self.assertEqual([c[0] for c in calls if c[1] == 'samples'][:4], ['a', 'b', 'b', 'a'])

    def test_noisy_target_prevents_early_stop(self):
        result, _, _ = self.collect({'a': [1000], 'b': [100, 1900]}, max_runs=40)
        self.assertEqual(result['stop_reason'], 'max_runs')
        self.assertEqual(result['actual_runs'], 40)

    def test_batches_stop_at_stability_checkpoint_with_equal_counts(self):
        result, targets, calls = self.collect({'a': [1000], 'b': [2000]}, batched=True)
        self.assertEqual(result['stop_reason'], 'stable')
        self.assertEqual(result['actual_runs'], 30)
        self.assertEqual([len(t['samples']) for t in targets.values()], [30, 30])
        self.assertEqual([c[0] for c in calls if c[1] == 'samples'][:4], ['a', 'b', 'b', 'a'])

    def test_partial_final_batch_preserves_fixed_count(self):
        result, targets, _ = self.collect({'a': [1000]}, batched=True, runs=7, warmup=3)
        self.assertEqual(result['actual_runs'], 7)
        self.assertEqual(len(targets['a']['warmups']), 3)
        self.assertEqual(len(targets['a']['samples']), 7)

    def test_partial_final_batch_respects_adaptive_run_cap(self):
        result, _, _ = self.collect({'a': [100, 1900]}, batched=True, max_runs=27)
        self.assertEqual(result['actual_runs'], 27)
        self.assertEqual(result['checked_runs'], 25)
        self.assertEqual(result['stop_reason'], 'max_runs')

    def test_failed_check_resets_stability_streak(self):
        checks = [{'passed': passed, 'relative_standard_error_percent': 0.5 if passed else 10,
                   'median_drift_percent': 0} for passed in (True, True, False, True, True, True)]
        with patch.object(benchmark, 'stability', side_effect=checks):
            result, _, _ = self.collect({'a': [1000]})
        self.assertEqual(result['stop_reason'], 'stable')
        self.assertEqual(result['actual_runs'], 45)

    def test_time_budget_finishes_round_and_excludes_warmups(self):
        result, targets, _ = self.collect({'a': [1000], 'b': [1000]}, cost=1, max_time=3)
        self.assertEqual(result['stop_reason'], 'time_limit')
        self.assertEqual(result['elapsed_seconds'], 4)
        self.assertEqual([len(t['samples']) for t in targets.values()], [2, 2])

    def test_fixed_count_ignores_stability_and_time_budget(self):
        result, _, _ = self.collect({'a': [1000]}, runs=7, max_time=0.1, cost=1)
        self.assertEqual(result['stop_reason'], 'fixed_runs')
        self.assertEqual(result['actual_runs'], 7)

    def test_failure_finishes_comparison_round(self):
        result, targets, _ = self.collect({'a': [1000], 'b': [1000]}, fail=('b', 'samples', 1))
        self.assertEqual(result['stop_reason'], 'failed_run')
        self.assertEqual([len(t['samples']) for t in targets.values()], [2, 2])

    def test_warmup_failure_prevents_measurement(self):
        result, targets, _ = self.collect({'a': [1000]}, fail=('a', 'warmups', 0))
        self.assertEqual(result['stop_reason'], 'failed_run')
        self.assertEqual(targets['a']['samples'], [])

    def test_zero_timings_do_not_converge(self):
        result, _, _ = self.collect({'a': [0]}, max_runs=35)
        self.assertEqual(result['stop_reason'], 'max_runs')

    def test_drift_rejects_small_standard_error(self):
        check = benchmark.stability([1000] * 1000 + [1100] * 1000, 1)
        self.assertLess(check['relative_standard_error_percent'], 1)
        self.assertFalse(check['passed'])


if __name__ == '__main__':
    unittest.main()
