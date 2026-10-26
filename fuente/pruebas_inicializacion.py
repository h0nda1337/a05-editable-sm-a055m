"""Run the original configure_da bytecode with stubs; never open USB."""
from pathlib import Path
import argparse
import json
import marshal
import os
import struct
import sys
import tempfile
import types
import unittest
import zlib

from a05_motor_trace import recover_connected_initialization
from reconstruir_motor_traza import read_archive, EXPECTED_ORIGINAL, sha

WORK = None


def original_method(module_name, method_name):
    path = Path(__file__).resolve().parent.parent/'motor/mtk.exe'
    data = path.read_bytes()
    assert sha(data) == EXPECTED_ORIGINAL
    _, entries, _ = read_archive(data)
    entry = next(item for item in entries if item['kind'] == b'z')
    pyz = zlib.decompress(entry['payload']) if entry['compressed'] else entry['payload']
    table = dict(marshal.loads(pyz[struct.unpack_from('!I', pyz, 8)[0]:]))
    _, position, length = table[module_name]
    module = marshal.loads(zlib.decompress(pyz[position:position+length]))

    def walk(code):
        yield code
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                yield from walk(value)

    code = next(value for value in walk(module) if value.co_name == method_name)
    return types.FunctionType(code, {'os': os}, method_name)


def original_configure():
    return original_method('mtkclient.Library.DA.mtk_da_handler', 'configure_da')


class ContinuationReached(Exception):
    pass


class TargetConfig:
    def __getitem__(self, name):
        # Stop on the first unchanged instruction after initialization, before
        # authentication, security, DA or operation handlers can be called.
        assert name == 'sbc'
        raise ContinuationReached()


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, event, site, **details):
        self.events.append({'event': event, 'site': site, **details})

    def console(self, message):
        pass


class InitializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vendor = staticmethod(original_configure())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a05-init-', dir=WORK)
        self.base = Path(self.temp.name)
        self.recorder = Recorder()
        self.method = recover_connected_initialization(self.vendor, self.recorder)
        self.connect_calls = self.init_calls = self.reinit_calls = 0
        self.present = True
        self.init_error = self.connect_error = None
        self.probe_inner_connect = False
        self.provide_config = True
        parent = self

        class CDC:
            connected = False

            def connect(self):
                parent.connect_calls += 1
                if parent.connect_error:
                    raise parent.connect_error
                return parent.present

        def init():
            self.init_calls += 1
            if self.probe_inner_connect:
                self.mtk.port.cdc.connect()
            if self.init_error:
                raise self.init_error
            if self.provide_config:
                self.mtk.config.target_config = TargetConfig()
            return self.provide_config

        def reinit():
            self.reinit_calls += 1

        self.mtk = types.SimpleNamespace(
            port=types.SimpleNamespace(cdc=CDC()), serialportname=None,
            config=types.SimpleNamespace(hwparam_path=str(self.base), target_config=None),
            preloader=types.SimpleNamespace(init=init), daloader=types.SimpleNamespace(reinit=reinit))
        self.messages = []
        self.owner = types.SimpleNamespace(info=self.messages.append)
        self.original_connect = self.mtk.port.cdc.connect

    def tearDown(self):
        self.assertEqual(self.mtk.port.cdc.connect, self.original_connect)
        self.temp.cleanup()

    def run_to_continuation(self, method=None):
        with self.assertRaises(ContinuationReached):
            (method or self.method)(self.owner, self.mtk, None)

    def test_original_present_connection_skips_init_and_returns_none(self):
        self.assertIsNone(self.vendor(self.owner, self.mtk, None))
        self.assertEqual(self.init_calls, 0)
        self.assertTrue(any('Please disconnect' in item for item in self.messages))

    def test_present_connection_initializes_once_and_reaches_original_continuation(self):
        self.run_to_continuation()
        self.assertEqual((self.connect_calls, self.init_calls, self.reinit_calls), (1, 1, 0))
        self.assertTrue(self.mtk.port.cdc.connected)
        self.assertEqual([item['event'] for item in self.recorder.events], ['begin', 'end'])
        self.assertTrue(self.recorder.events[-1]['target_config_ready'])
        self.assertNotIn('connect', vars(self.mtk.port.cdc))

    def test_absent_connection_preserves_original_init_path(self):
        self.present = False
        self.run_to_continuation()
        self.assertEqual((self.connect_calls, self.init_calls), (1, 1))
        self.assertEqual(self.recorder.events, [])

    def test_none_connection_preserves_original_init_path(self):
        self.present = None
        self.run_to_continuation()
        self.assertEqual((self.connect_calls, self.init_calls), (1, 1))
        self.assertEqual(self.recorder.events, [])

    def test_saved_da_state_retains_original_reinit_without_preloader_init(self):
        (self.base/'.state').write_bytes(b'SIMULATED_STATE')
        self.assertIs(self.method(self.owner, self.mtk, None), self.mtk)
        self.assertEqual((self.init_calls, self.reinit_calls), (0, 1))
        self.assertEqual(self.recorder.events, [])

    def test_existing_target_config_is_not_initialized_again(self):
        self.mtk.config.target_config = TargetConfig()
        self.run_to_continuation()
        self.assertEqual(self.init_calls, 0)
        self.assertEqual(self.recorder.events, [])

    def test_serial_connection_keeps_original_init_path(self):
        self.mtk.serialportname = 'SIMULATED_COM'
        self.run_to_continuation()
        self.assertEqual(self.init_calls, 1)
        self.assertEqual(self.recorder.events, [])

    def test_inner_handshake_connect_does_not_reenter_adapter(self):
        self.probe_inner_connect = True
        self.run_to_continuation()
        self.assertEqual((self.connect_calls, self.init_calls), (2, 1))

    def test_init_without_target_config_keeps_incomplete_result(self):
        self.provide_config = False
        self.assertIsNone(self.method(self.owner, self.mtk, None))
        self.assertEqual(self.init_calls, 1)
        self.assertFalse(self.recorder.events[-1]['target_config_ready'])
        self.assertTrue(any('Please disconnect' in item for item in self.messages))

    def test_connect_exception_is_preserved_and_adapter_restored(self):
        self.connect_error = OSError(123, 'SIMULATED_CONNECT_FAILURE')
        with self.assertRaises(OSError) as caught:
            self.method(self.owner, self.mtk, None)
        self.assertIs(caught.exception, self.connect_error)
        self.assertEqual(self.init_calls, 0)

    def test_init_exception_is_preserved_and_adapter_restored(self):
        self.init_error = OSError(124, 'SIMULATED_INIT_FAILURE')
        with self.assertRaises(OSError) as caught:
            self.method(self.owner, self.mtk, None)
        self.assertIs(caught.exception, self.init_error)
        self.assertEqual(self.init_calls, 1)
        self.assertEqual(self.recorder.events[-1]['event'], 'error')

    def test_instance_bound_connect_is_restored_exactly(self):
        self.mtk.port.cdc.connect = self.original_connect
        self.run_to_continuation()
        self.assertIs(vars(self.mtk.port.cdc)['connect'], self.original_connect)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--report', type=Path)
    args, remaining = parser.parse_known_args()
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        WORK = args.work_dir.resolve()
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    result = program.result
    if args.report:
        args.report.write_text(json.dumps({'count': result.testsRun, 'all_passed': result.wasSuccessful(),
                                           'original_configure_bytecode_executed': True,
                                           'only_stub_devices': True, 'usb_access': False,
                                           'phone_operation_executed': False}, indent=2), encoding='utf-8')
    sys.exit(not result.wasSuccessful())
