"""Trace and progress regressions with fake calls and files only; never use USB."""
from pathlib import Path
import argparse
import json
import sys
import tempfile
import unittest

from a05_motor_trace import TraceRecorder, instrument, packet_summary
from a05_progress import EngineProgress

WORK = None


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a05-trace-', dir=WORK)
        self.base = Path(self.temp.name)
        self.current = 0.0
        self.events = []
        self.resources = []

    def tearDown(self):
        for item in self.resources:
            item.close()
        self.temp.cleanup()

    def progress(self):
        item = EngineProgress(self.base, lambda kind, text: self.events.append((kind, text)), clock=lambda: self.current)
        self.resources.append(item)
        return item

    def recorder(self):
        item = TraceRecorder(self.base)
        item.console = lambda text: None
        self.resources.append(item)
        return item

    def records(self):
        for item in self.resources:
            if isinstance(item, TraceRecorder):
                item.flush()
        return [json.loads(line) for line in (self.base/'traza_motor.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_observer_preserves_return_and_original_arguments(self):
        recorder = self.recorder()
        token, calls = object(), []
        def operation(value, option=None):
            calls.append((value, option))
            return token
        observed = instrument(operation, 'fake.operation', recorder)
        self.assertIs(observed('SAMPLE', option=7), token)
        self.assertEqual(calls, [('SAMPLE', 7)])
        self.assertEqual([r['event'] for r in self.records()], ['begin', 'end'])

    def test_original_usb_error_is_recorded_and_reraised_unchanged(self):
        recorder = self.recorder()
        failure = OSError(10060, 'SIMULATED_TIMEOUT')
        failure.backend_error_code = -7
        def operation():
            raise failure
        try:
            instrument(operation, 'usb.read', recorder)()
        except OSError as caught:
            self.assertIs(caught, failure)
        else:
            self.fail('Exception was swallowed')
        error = self.records()[-1]['error']
        self.assertEqual(error['errno'], 10060)
        self.assertEqual(error['backend_error_code'], -7)
        self.assertIn('operation', error['traceback'])

    def test_packet_preview_does_not_dump_entire_large_transfer(self):
        data = bytes(range(256))*4096
        result = packet_summary(data)
        self.assertEqual(result['bytes'], len(data))
        self.assertEqual(result['preview_hex'], data[:64].hex())
        self.assertTrue(result['preview_truncated'])

    def test_failed_handshake_is_counted_without_claiming_synchronization(self):
        progress = self.progress()
        for attempt in range(2):
            progress.consume({'event': 'begin', 'site': 'port.handshake_attempt'})
            progress.consume({'event': 'end', 'site': 'port.handshake_attempt', 'result': {'value': False}})
        state = progress.snapshot()
        self.assertEqual(state['failed_handshake_attempts'], 2)
        self.assertFalse(state['handshake_confirmed'])
        progress.consume({'event': 'begin', 'site': 'port.handshake_attempt'})
        progress.consume({'event': 'end', 'site': 'port.handshake_attempt', 'result': {'value': True}})
        self.assertTrue(progress.snapshot()['handshake_confirmed'])
        self.assertEqual(progress.phase, 'inicializando')

    def test_slow_call_has_elapsed_notice_even_without_new_output(self):
        progress = self.progress()
        progress.consume({'event': 'begin', 'site': 'port.handshake_attempt'})
        progress.consume({'event': 'begin', 'site': 'usb.read', 'seq': 5})
        self.current = 35
        progress.heartbeat()
        message = self.events[-1][1]
        self.assertIn('35s', message)
        self.assertIn('esperando respuesta', message)
        self.assertIn('Mantén el cable conectado', message)
        self.assertEqual(progress.snapshot()['pending_usb_call']['site'], 'usb.read')
        self.assertTrue((self.base/'estado_motor.jsonl').is_file())

    def test_partial_trace_record_is_retained_until_newline(self):
        progress = self.progress()
        path = self.base/'traza_motor.jsonl'
        line = json.dumps({'event': 'begin', 'site': 'port.handshake_attempt'}).encode()
        path.write_bytes(line[:15])
        progress.poll()
        self.assertEqual(progress.attempts, 0)
        with path.open('ab') as stream:
            stream.write(line[15:]+b'\n')
        progress.poll()
        self.assertEqual(progress.attempts, 1)

    def test_fragmented_connection_prompt_is_understood(self):
        progress = self.progress()
        progress.observe_text('Preloader - Status: Waiting for connec')
        self.assertEqual(progress.phase, 'arranque')
        progress.observe_text('tion\n')
        self.assertEqual(progress.phase, 'esperando_usb')
        self.assertIn('una sola vez', self.events[-1][1])

    def test_recovered_initialization_keeps_cable_instruction(self):
        progress = self.progress()
        progress.consume({'event': 'begin', 'site': 'a05.initialize_present_preloader'})
        self.assertEqual(progress.phase, 'inicializando')
        self.assertFalse(progress.synchronized)
        self.assertIn('Mantén el cable conectado', self.events[-1][1])

    def test_recovered_configuration_error_is_not_reported_as_current_failure(self):
        progress = self.progress()
        progress.consume({'event': 'error', 'site': 'usb.configuration',
                          'error': {'type': 'USBError', 'message': 'Configuration not set'}})
        self.assertIsNotNone(progress.last_error)
        progress.consume({'event': 'end', 'site': 'usb.configuration', 'result': {'type': 'Configuration'}})
        self.assertIsNone(progress.last_error)

    def test_controlled_echo_failure_does_not_claim_motor_is_retrying(self):
        progress = self.progress()
        progress.consume({'event': 'failure', 'site': 'a05.handshake', 'code': 'SYNC_ECHO', 'message': 'Se recibió eco.'})
        progress.consume({'event': 'error', 'site': 'port.handshake_attempt', 'error': {'type': 'HandshakeFailure'}})
        self.assertEqual(progress.phase, 'sincronizacion_detenida')
        self.assertEqual(progress.snapshot()['handshake_failure']['code'], 'SYNC_ECHO')
        self.assertFalse(progress.synchronized)

    def test_presence_changes_do_not_claim_physical_cable_removal(self):
        progress = self.progress()
        progress.observe_text('Waiting for connection')
        progress.observe_presence(('0e8d:2000',))
        self.assertEqual(progress.phase, 'usb_detectado')
        progress.observe_presence(())
        self.assertEqual(progress.phase, 'esperando_reenumeracion')
        self.assertIn('Si el cable sigue conectado', self.events[-1][1])
        self.assertEqual(progress.snapshot()['windows_usb_modes'], ())

    def test_malformed_trace_does_not_hide_following_valid_event(self):
        progress = self.progress()
        (self.base/'traza_motor.jsonl').write_text('BROKEN\n'+json.dumps({'event':'begin','site':'port.handshake_attempt'})+'\n', encoding='utf-8')
        progress.poll()
        self.assertEqual(progress.attempts, 1)
        self.assertTrue(any('registro de traza incompleto' in text for _, text in self.events))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path)
    args, remaining = parser.parse_known_args()
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        WORK = args.work_dir.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
