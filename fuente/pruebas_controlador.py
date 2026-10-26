"""Meaningful controller tests. Only simulated profiles and Python subprocesses; no USB."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from a05_core import (MODEL, ProcessRunner, RunPlan, build_plan, file_hash,
                      import_private_profile, save_json, set_watchdog_option, validate_environment, completion_message)
from a05_usb_guard import wait_for_usb_absence
from a05_console import UiEventBuffer

WORK = None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a05-', dir=WORK)
        self.base = Path(self.temp.name)
        self.backend = self.base/'motor.exe'
        self.backend.write_bytes(b'FAKE_BACKEND_NEVER_EXECUTED')
        self.diagnostic = self.base/'diagnostic.exe'
        self.diagnostic.write_bytes(b'FAKE_DIAGNOSTIC_NEVER_EXECUTED')
        self.profile = self.base/'private.json'
        self.secret = 'SIMULATED_PRIVATE_ARGUMENT'
        save_json(self.profile, {'model': MODEL, 'arguments': ['simulated', self.secret, '--skipwdt', '1'], 'loader': 'off'})
        self.config = self.base/'config.json'
        save_json(self.config, {'model': MODEL, 'skip_watchdog': True, 'wait_seconds': 30,
                               'backend': str(self.backend), 'backend_sha256': file_hash(self.backend),
                               'diagnostic': str(self.diagnostic), 'diagnostic_sha256': file_hash(self.diagnostic),
                               'private_profile': str(self.profile), 'private_logs': str(self.base/'logs')})

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_and_option_preserve_other_arguments(self):
        self.assertEqual(set_watchdog_option(['simulated', 'value', '--skipwdt=0', '--skipwdt', '2'], True),
                         ['simulated', 'value', '--skipwdt', '1'])
        self.assertEqual(set_watchdog_option(['simulated', 'value', '--skipwdt', '1'], False), ['simulated', 'value'])
        plan = build_plan(self.config, 'perfil')
        self.assertIn(self.secret, plan.command)
        self.assertEqual(plan.command[-2:], ('--skipwdt', '1'))
        self.assertEqual(plan.disconnect_timeout_seconds, 30)

    def test_validation_report_does_not_expose_private_arguments(self):
        report = validate_environment(self.config)
        self.assertNotIn(self.secret, json.dumps(report))
        self.assertFalse(report['usb_access'])

    def test_invalid_backend_hash_stops_before_process(self):
        self.backend.write_bytes(b'CHANGED')
        with patch('a05_core.subprocess.Popen') as popen:
            with self.assertRaises(ValueError):
                build_plan(self.config, 'perfil')
            popen.assert_not_called()

    def test_missing_watchdog_value_is_rejected(self):
        with self.assertRaises(ValueError):
            set_watchdog_option(['simulated', '--skipwdt'], True)

    def test_consultation_has_no_private_operation_arguments(self):
        plan = build_plan(self.config, 'consulta')
        self.assertIn('--security-info', plan.command)
        self.assertNotIn(self.secret, plan.command)
        self.assertNotIn('--watchdog', plan.command)
        self.assertGreater(plan.hard_timeout_seconds, 30)

    def test_consultation_works_without_profile_or_operation_backend(self):
        self.profile.unlink()
        self.backend.unlink()
        with patch('a05_core.load_profile', side_effect=AssertionError('Profile must not be read')):
            with patch('a05_core.subprocess.Popen') as popen:
                plan = build_plan(self.config, 'consulta')
                self.assertEqual(plan.kind, 'consulta')
                popen.assert_not_called()

    def test_operation_without_profile_gives_import_guidance(self):
        self.profile.unlink()
        with patch('a05_core.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'Importar perfil'):
                build_plan(self.config, 'perfil')
            popen.assert_not_called()
        self.assertFalse((self.base/'logs').exists())

    def test_operation_does_not_require_diagnostic_executable(self):
        self.diagnostic.unlink()
        self.assertEqual(build_plan(self.config, 'perfil').kind, 'perfil')

    def test_invalid_private_profile_does_not_block_consultation(self):
        self.profile.write_text('INVALID_JSON', encoding='utf-8')
        report = validate_environment(self.config, action='consulta')
        self.assertTrue(report['diagnostic_verified'])
        self.assertIsNone(report['profile_valid'])

    def test_invalid_import_preserves_current_profile(self):
        before = self.profile.read_bytes()
        wrong = self.base/'invalid.json'
        save_json(wrong, {'model': MODEL, 'arguments': ['simulated', '--loader', 'unapproved.bin'], 'loader': 'off'})
        with self.assertRaises(ValueError):
            import_private_profile(wrong, self.profile)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_original_metadata_imports_only_target_model(self):
        source = self.base/'metadata.json'
        save_json(source, {'Samsung': [{'nombre': 'A05 '+MODEL, 'comando_base': 'simulated value', 'loader': 'off'},
                                      {'nombre': 'Otro modelo', 'comando_base': 'other value', 'loader': 'off'}]})
        import_private_profile(source, self.profile)
        result = json.loads(self.profile.read_text(encoding='utf-8'))
        self.assertEqual(result['arguments'], ['simulated', 'value'])
        self.assertEqual(len(list(self.base.glob('private.json.backup-*'))), 1)

    def test_stdout_and_stderr_are_retained_on_failure(self):
        plan = RunPlan((sys.executable, '-u', '-c',
                        "import sys; print('SALIDA_DE_PRUEBA',flush=True); print('ERROR_DE_PRUEBA',file=sys.stderr,flush=True); sys.exit(7)"),
                       self.base, 'simulated')
        events = []
        result = ProcessRunner().run(plan, lambda kind, text: events.append((kind, text)))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['returncode'], 7)
        self.assertIn('SALIDA_DE_PRUEBA', (self.base/'stdout.txt').read_text())
        self.assertIn('ERROR_DE_PRUEBA', (self.base/'stderr.txt').read_text())
        self.assertTrue(any(kind == 'stdout' for kind, _ in events))
        self.assertTrue(any(kind == 'stderr' for kind, _ in events))
        self.assertNotIn('command', result)

    def test_cancel_before_start_launches_nothing(self):
        runner = ProcessRunner()
        runner.cancel()
        with patch('a05_core.subprocess.Popen') as popen:
            result = runner.run(RunPlan(('never-execute',), self.base, 'simulated'), lambda *args: None)
            popen.assert_not_called()
        self.assertEqual(result['status'], 'cancelled')

    def test_reconnect_message_with_exit_zero_is_not_completion(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                program = ("import sys; print('DaHandler - Please disconnect, start mtkclient and reconnect.',"
                           "file=sys."+stream+"); sys.exit(0)")
                result = ProcessRunner().run(RunPlan((sys.executable, '-u', '-c', program), self.base, 'perfil'), lambda *args: None)
                self.assertEqual(result['returncode'], 0)
                self.assertEqual(result['status'], 'reconnect_required')
                self.assertTrue(result['reconnect_message_observed'])
                self.assertIn('Inicialización incompleta', completion_message(result))
                saved = json.loads((self.base/'resultado_controlador.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], 'reconnect_required')

    def test_zero_exit_without_operation_evidence_remains_unverified(self):
        result = ProcessRunner().run(RunPlan((sys.executable, '-c', "print('BANNER_ONLY')"), self.base, 'perfil'), lambda *args: None)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertIn('no está confirmado', completion_message(result))

    def test_output_without_newline_arrives_before_process_exit(self):
        runner = ProcessRunner()
        events = []
        def receive(kind, text):
            events.append((kind, text))
            if kind == 'stdout' and '.' in text:
                runner.cancel()
        program = "import os,time; os.write(1,b'.'); time.sleep(10)"
        result = runner.run(RunPlan((sys.executable, '-u', '-c', program), self.base, 'simulated', 2), receive)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(any(kind == 'stdout' and '.' in text for kind, text in events))

    def test_split_utf8_character_is_decoded_without_replacement(self):
        runner = ProcessRunner()
        received = []
        def receive(kind, text):
            if kind == 'stdout':
                received.append(text)
                if 'ñ' in text:
                    runner.cancel()
        program = "import os,time; os.write(1,bytes([0xc3])); time.sleep(0.2); os.write(1,bytes([0xb1])); time.sleep(10)"
        result = runner.run(RunPlan((sys.executable, '-u', '-c', program), self.base, 'simulated', 2), receive)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(''.join(received), 'ñ')
        self.assertEqual((self.base/'stdout.txt').read_bytes(), 'ñ'.encode('utf-8'))

    def test_complete_output_is_saved_when_console_discards_backlog(self):
        display = UiEventBuffer(max_output_chars=8192)
        program = "import os; payload=b'FULL_OUTPUT_RECORD\\n'*200000; os.write(1,payload)"
        plan = RunPlan((sys.executable, '-u', '-c', program), self.base, 'simulated', 20)
        result = ProcessRunner().run(plan, lambda kind, text: display.put((kind, text)))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.base/'stdout.txt').read_bytes(), b'FULL_OUTPUT_RECORD\n'*200000)
        self.assertGreater(display.snapshot()['display_chars_omitted'], 0)

    def test_nonzero_exit_is_preserved_when_reconnect_is_requested(self):
        program = "import sys; print('Please disconnect, start mtkclient and reconnect.'); sys.exit(9)"
        result = ProcessRunner().run(RunPlan((sys.executable, '-c', program), self.base, 'perfil'), lambda *args: None)
        self.assertEqual(result['returncode'], 9)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['reconnect_message_observed'])

    def test_motor_starts_only_after_usb_absence_is_confirmed(self):
        calls = 0
        events = []
        actual_popen = subprocess.Popen
        with patch('a05_core.subprocess.Popen', wraps=actual_popen) as popen:
            def presence():
                nonlocal calls
                popen.assert_not_called()
                calls += 1
                return calls < 3
            with patch('a05_core.present_mediatek_usb', side_effect=presence):
                plan = RunPlan((sys.executable, '-u', '-c', "print('Status: Waiting for connection')"),
                               self.base, 'perfil', disconnect_timeout_seconds=10)
                result = ProcessRunner().run(plan, lambda kind, text: events.append((kind, text)))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(result['usb_disconnect_check']['status'], 'absent')
        self.assertTrue(result['usb_disconnect_check']['present_seen'])
        self.assertGreaterEqual(result['usb_disconnect_check']['stable_absence_seconds'], 1.5)
        self.assertTrue(any(kind == 'stage' and 'Conecta ahora' in text for kind, text in events))

    def test_usb_disconnect_timeout_does_not_start_motor(self):
        plan = RunPlan(('NEVER_EXECUTE',), self.base, 'perfil', disconnect_timeout_seconds=0.03)
        with patch('a05_core.present_mediatek_usb', return_value=('0e8d:2000',)):
            with patch('a05_core.subprocess.Popen') as popen:
                result = ProcessRunner().run(plan, lambda *args: None)
                popen.assert_not_called()
        self.assertEqual(result['status'], 'disconnect_timeout')
        self.assertNotIn('returncode', result)
        self.assertIn('El motor no se inició', completion_message(result))

    def test_cancellation_while_usb_connected_does_not_start_motor(self):
        runner = ProcessRunner()
        def presence():
            runner.cancel()
            return ('0e8d:2000',)
        with patch('a05_core.present_mediatek_usb', side_effect=presence):
            with patch('a05_core.subprocess.Popen') as popen:
                plan = RunPlan(('NEVER_EXECUTE',), self.base, 'perfil', disconnect_timeout_seconds=10)
                result = runner.run(plan, lambda *args: None)
                popen.assert_not_called()
        self.assertEqual(result['status'], 'cancelled')

    def test_inventory_failure_is_not_treated_as_disconnection(self):
        with patch('a05_core.present_mediatek_usb', side_effect=OSError('SIMULATED_FAILURE')):
            with patch('a05_core.subprocess.Popen') as popen:
                plan = RunPlan(('NEVER_EXECUTE',), self.base, 'perfil', disconnect_timeout_seconds=10)
                result = ProcessRunner().run(plan, lambda *args: None)
                popen.assert_not_called()
        self.assertEqual(result['status'], 'usb_inventory_error')
        self.assertEqual(result['usb_disconnect_check']['error_type'], 'OSError')

    def test_temporary_usb_absence_is_reset_by_reappearance(self):
        current_time = [0.0]
        flag = threading.Event()
        sequence = iter([False, False, True, False, False, False])
        def advance(seconds):
            current_time[0] += seconds
        with patch('a05_usb_guard.time.monotonic', side_effect=lambda: current_time[0]):
            with patch.object(flag, 'wait', side_effect=advance):
                report = wait_for_usb_absence(lambda: next(sequence), flag, lambda *args: None,
                                             5, stable_seconds=0.5, poll_seconds=0.25)
        self.assertEqual(report['status'], 'absent')
        self.assertEqual(report['samples'], 6)
        self.assertTrue(report['present_seen'])
        self.assertEqual(report['stable_absence_seconds'], 0.5)

    def test_cancel_stops_own_process_and_leaves_other_process(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], creationflags=flags)
        try:
            runner = ProcessRunner()
            def receive(kind, text):
                if 'READY_TO_CANCEL' in text:
                    runner.cancel()
            plan = RunPlan((sys.executable, '-u', '-c', "import time; print('READY_TO_CANCEL',flush=True); time.sleep(30)"),
                           self.base, 'simulated', 10)
            result = runner.run(plan, receive)
            self.assertEqual(result['status'], 'cancelled')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path)
    args, remaining = parser.parse_known_args()
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        WORK = args.work_dir.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
