import threading
import time
import unittest
from collections import deque
from unittest.mock import patch

from eks_console import backend
from eks_console.performance import PerformanceMonitor


class PerformanceFilterTests(unittest.TestCase):
    def test_demo_capacity_can_be_filtered_by_microservice_prefix(self):
        with patch.object(backend, 'DEMO', True):
            result = backend.capacity_snapshot(
                'generaciondocumentaldigital-qa', 'demo-context', micro_prefix='62001'
            )

        self.assertTrue(result['pods'])
        self.assertTrue(all(pod['micro'].startswith('62001') for pod in result['pods']))
        self.assertEqual(3, result['sample']['total'])

    def test_status_returns_only_the_requested_time_window(self):
        monitor = PerformanceMonitor.__new__(PerformanceMonitor)
        monitor.lock = threading.RLock()
        monitor.samples = deque([
            {'time': time.time() - 700, 'total': 1, 'ready': 1, 'measured': 1},
            {'time': time.time() - 60, 'total': 1, 'ready': 1, 'measured': 1},
        ], maxlen=2880)
        monitor.run_samples = deque(maxlen=2880)
        monitor.started = monitor.ended = None
        monitor.mode = 'live'
        monitor.scope = monitor.selection = monitor.latest = monitor.run = None
        monitor.error = monitor.azure_error = ''
        monitor.jtl = None
        monitor.slo = {'p95_ms': 1000, 'errors_percent': 1}
        monitor.run_latest = None
        monitor.is_demo = lambda: True

        result = monitor.status(300)

        self.assertEqual(300, result['window_seconds'])
        self.assertEqual(1, len(result['samples']))


if __name__ == '__main__':
    unittest.main()
