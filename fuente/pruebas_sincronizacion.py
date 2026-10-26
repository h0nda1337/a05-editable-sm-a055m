"""Handshake regressions with stub endpoints and original vendor bytecode."""
from pathlib import Path
import argparse
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from a05_motor_trace import bounded_run_handshake, bounded_handshake, HandshakeFailure
from pruebas_inicializacion import original_method


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, event, site, **details):
        self.events.append({'event': event, 'site': site, **details})

    def flush(self):
        pass

    def console(self, message):
        pass


class HandshakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vendor_run = staticmethod(original_method('mtkclient.Library.Port', 'run_handshake'))
        cls.vendor_wait = staticmethod(original_method('mtkclient.Library.Port', 'handshake'))

    def setUp(self):
        self.recorder = Recorder()
        self.now = 0.0
        self.replies = iter([b'\x5f', b'\xf5', b'\xaf', b'\xfa'])
        self.frames, self.timeouts, self.messages = [], [], []
        self.reads = self.connects = 0
        parent = self

        class In:
            wMaxPacketSize = 512

            def read(self, length, timeout=None):
                parent.reads += 1
                parent.timeouts.append(timeout)
                assert length == 512
                value = next(parent.replies)
                if isinstance(value, BaseException):
                    raise value
                return value

        class Out:
            def write(self, data, timeout=None):
                parent.frames.append(bytes(data))
                parent.timeouts.append(timeout)
                return len(data)

        self.cdc = types.SimpleNamespace(connected=True, pid=0x2000, is_serial=False, EP_IN=In(), EP_OUT=Out())
        self.port = types.SimpleNamespace(cdc=self.cdc, serialportname=None, info=self.messages.append)
        self.port.run_handshake = lambda: bounded_run_handshake(self.port, self.recorder,
                                                               clock=lambda: self.now, sleep=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def run_handshake(self):
        return self.port.run_handshake()

    def failure(self, reason):
        with self.assertRaises(HandshakeFailure) as caught:
            self.run_handshake()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.recorder.events[-1]['code'], reason)
        return caught.exception

    def test_original_echo_loop_requires_external_interrupt(self):
        class SimulationStop(BaseException):
            pass
        self.replies = iter([b'\xa0']*32+[SimulationStop()])
        with self.assertRaises(SimulationStop):
            self.vendor_run(self.port)
        self.assertEqual(self.reads, 33)
        self.assertEqual(set(self.frames), {b'\xa0'})

    def test_original_wait_skips_already_connected_cdc(self):
        self.assertFalse(self.vendor_wait(self.port, 100, 0))
        self.assertEqual(self.frames, [])

    def test_preloader_success_uses_exact_diagnostic_sequence(self):
        self.assertTrue(self.run_handshake())
        self.assertEqual(self.frames, [b'\xa0', b'\xa0', b'\x0a', b'\x50', b'\x05'])
        self.assertEqual(self.reads, 4)
        self.assertTrue(all(value and 0 < value <= 1500 for value in self.timeouts))

    def test_brom_does_not_send_preloader_wakeup(self):
        self.cdc.pid = 3
        self.assertTrue(self.run_handshake())
        self.assertEqual(self.frames, [b'\xa0', b'\x0a', b'\x50', b'\x05'])

    def test_echo_stops_after_one_read_without_busy_retry(self):
        self.replies = iter([b'\xa0']*100)
        self.failure('SYNC_ECHO')
        self.assertEqual((len(self.frames), self.reads), (2, 1))

    def test_preloader_banner_can_resynchronize_with_finite_budget(self):
        self.replies = iter([b'READY', b'\x5f', b'\xf5', b'\xaf', b'\xfa'])
        self.assertTrue(self.run_handshake())
        self.assertEqual(self.reads, 5)

    def test_wrong_preloader_packets_stop_at_sixteen_exchanges(self):
        self.replies = iter([b'?']*100)
        self.failure('SYNC_RETRY_LIMIT')
        self.assertEqual((len(self.frames), self.reads), (17, 16))

    def test_empty_packet_is_not_indexed_or_retried(self):
        self.replies = iter([b''])
        self.failure('SYNC_EMPTY_READ')
        self.assertEqual(self.reads, 1)

    def test_short_write_stops_before_read(self):
        self.cdc.EP_OUT.write = lambda data, timeout=None: 0
        self.failure('SYNC_SHORT_WRITE')
        self.assertEqual(self.reads, 0)

    def test_native_read_error_codes_remain_in_failure(self):
        error = OSError(10060, 'SIMULATED_TIMEOUT')
        error.backend_error_code = -7
        self.replies = iter([error])
        failure = self.failure('SYNC_USB_READ')
        self.assertEqual(failure.details['original']['errno'], 10060)
        self.assertEqual(failure.details['original']['backend_error_code'], -7)

    def test_deadline_stops_before_next_usb_transfer(self):
        def slow_write(data, timeout=None):
            self.now += 11
            return len(data)
        self.cdc.EP_OUT.write = slow_write
        self.failure('SYNC_DEADLINE')
        self.assertEqual(self.reads, 0)

    def test_connected_cdc_now_runs_handshake_without_reopening(self):
        self.cdc.connect = lambda: self.fail('Existing handle must not reopen')
        method = bounded_handshake(self.vendor_wait, self.recorder, clock=lambda: self.now, sleep=self.advance)
        self.assertTrue(method(self.port, maxtries=100))
        self.assertEqual(self.reads, 4)

    def test_connection_wait_has_deadline_without_any_transfer(self):
        self.cdc.connected = False
        def connect():
            self.connects += 1
            return False
        self.cdc.connect = connect
        method = bounded_handshake(self.vendor_wait, self.recorder, clock=lambda: self.now, sleep=self.advance)
        with patch.dict(os.environ, {'A05_USB_WAIT_SECONDS': '2'}):
            with self.assertRaises(HandshakeFailure) as caught:
                method(self.port, maxtries=100)
        self.assertEqual(caught.exception.reason, 'SYNC_NO_USB')
        self.assertLessEqual(self.connects, 9)
        self.assertEqual(self.frames, [])

    def test_new_usb_arrival_connects_then_completes_handshake(self):
        self.cdc.connected = False
        def connect():
            self.connects += 1
            self.cdc.connected = self.connects == 3
            return self.cdc.connected
        self.cdc.connect = connect
        method = bounded_handshake(self.vendor_wait, self.recorder, clock=lambda: self.now, sleep=self.advance)
        self.assertTrue(method(self.port, maxtries=100))
        self.assertEqual(self.connects, 3)
        self.assertEqual(self.reads, 4)

    def test_serial_path_preserves_original_arguments_and_result(self):
        token, calls = object(), []
        def original(port, **kwargs):
            calls.append((port, kwargs))
            return token
        self.cdc.is_serial = True
        self.assertIs(bounded_handshake(original, self.recorder)(self.port, 7, 2), token)
        self.assertEqual(calls, [(self.port, {'maxtries': 7, 'loop': 2})])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    args, remaining = parser.parse_known_args()
    result = unittest.main(argv=[sys.argv[0], *remaining], exit=False).result
    if args.report:
        args.report.write_text(json.dumps({'count': result.testsRun, 'all_passed': result.wasSuccessful(),
                                           'original_handshake_bytecode_executed': True,
                                           'only_stub_endpoints': True, 'usb_access': False,
                                           'phone_operation_executed': False}, indent=2), encoding='utf-8')
    sys.exit(not result.wasSuccessful())
