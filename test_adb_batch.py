import io
import tarfile
import unittest

import benchmark


def archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w') as tar:
        for name, data in entries:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return result.getvalue()


def sample(index, ns=b'123\n', rc=b'0\n', stdout=b'', stderr=b''):
    return [(f'./{index}.{suffix}', data) for suffix, data in
            [('ns', ns), ('rc', rc), ('out', stdout), ('err', stderr)]]


class BatchProtocolTests(unittest.TestCase):
    def test_streams_are_not_protocol(self):
        output = b'0.rc\nSAMPLE 1 0 999\n\x00\xff'
        result = benchmark.parse_adb_batch(archive(sample(0, stdout=output, stderr=b'error\n')), 1)
        self.assertEqual(result[0]['elapsed_ns'], 123)
        self.assertEqual(result[0]['stdout'], output.decode('utf-8', errors='replace'))
        self.assertEqual(result[0]['stderr'], 'error\n')

    def test_missing_timer_and_nonzero_exit_are_failed_samples(self):
        result = benchmark.parse_adb_batch(archive(sample(0, ns=b'') + sample(1, rc=b'7\n')), 2)
        self.assertTrue(all('error' in s for s in result))

    def test_timeout_preserves_partial_output(self):
        result = benchmark.parse_adb_batch(archive(sample(0, ns=b'', rc=b'137\n', stdout=b'before timeout')), 1)
        self.assertIn('timed out', result[0]['error'])
        self.assertEqual(result[0]['stdout'], 'before timeout')

    def test_prefix_is_allowed_for_deadline_or_failure(self):
        self.assertEqual(len(benchmark.parse_adb_batch(archive(sample(0)), 5)), 1)

    def test_malformed_archives_are_rejected(self):
        cases = [b'garbage', archive([]), archive(sample(1)),
                 archive(sample(0) + [('../outside', b'x')]),
                 archive(sample(0) + sample(0)), archive(sample(0)[:-1]),
                 archive(sample(0, rc=b'garbage')), archive(sample(0, rc=b'999')),
                 archive(sample(0) + sample(2))]
        for payload in cases:
            with self.subTest(payload_size=len(payload)), self.assertRaises(benchmark.BenchError):
                benchmark.parse_adb_batch(payload, 2)


if __name__ == '__main__':
    unittest.main()
