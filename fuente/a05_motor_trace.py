"""Engine tracing, initialization recovery and bounded USB synchronization.

USB observers preserve results and exceptions. The configure wrapper invokes
the original Preloader.init only for a newly opened, uninitialized connection
without a saved DA state. USB loads the diagnostic's verified DLL and uses a
finite handshake. Security and operation handlers remain original.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import functools
import faulthandler
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
import threading
import time
import traceback

TRACE_VERSION = '1.2.0'
LIBUSB_SHA256 = '3f050dee9fbf995bfbed2f4969131877fcd1b59bfd3afd2a767ff6844e872430'
_recorder = None


def packet_summary(value):
    if isinstance(value, bool) or value is None:
        return {'value': value}
    if isinstance(value, int):
        return {'value': value}
    try:
        view = memoryview(value).cast('B')
        return {'bytes': len(view), 'preview_hex': view[:64].hex(), 'preview_truncated': len(view) > 64}
    except (TypeError, ValueError):
        return {'type': type(value).__name__}


def usb_parameters(site, signature, args, kwargs):
    try:
        values = signature.bind_partial(*args, **kwargs).arguments
    except TypeError:
        return {}
    result = {}
    for key in ('timeout', 'maxtimeout', 'resplen', 'ep_in', 'ep_out', 'bmRequestType',
                'bRequest', 'wValue', 'wIndex', 'idVendor', 'idProduct'):
        value = values.get(key)
        if isinstance(value, int) or value is None and key in values:
            result[key] = value
    endpoint = values.get('endpoint')
    if isinstance(endpoint, int):
        result['endpoint_hex'] = hex(endpoint)
    if site == 'usb.write' and 'data' in values:
        result['payload'] = packet_summary(values['data'])
    if site == 'usb.read' and 'size_or_buffer' in values:
        value = values['size_or_buffer']
        result['requested_bytes'] = value if isinstance(value, int) else packet_summary(value).get('bytes')
    if site == 'usb.control' and 'data_or_wLength' in values:
        result['data_or_length'] = packet_summary(values['data_or_wLength'])
    return result


class TraceRecorder:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory/'traza_motor.jsonl').open('a', encoding='utf-8', buffering=65536)
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.sequence = 0
        self.failed = False
        self.fault_stream = None
        self.closed = False
        self.console_notices = {}
        self.stop_flush = threading.Event()
        self.flusher = threading.Thread(target=self._flush_periodically, daemon=True, name='A05TraceFlush')
        self.flusher.start()

    def _flush_periodically(self):
        while not self.stop_flush.wait(0.25):
            try:
                self.flush()
            except Exception:
                pass

    def flush(self):
        with self.lock:
            if not self.closed:
                self.stream.flush()

    def event(self, event, site, **details):
        try:
            with self.lock:
                self.sequence += 1
                record = {'seq': self.sequence, 'utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                          'elapsed_ms': round((time.monotonic()-self.started)*1000, 3),
                          'event': event, 'site': site, **details}
                self.stream.write(json.dumps(record, ensure_ascii=False, default=str)+'\n')
                if event in {'error', 'installed', 'selftest_passed'}:
                    self.stream.flush()
                return self.sequence
        except Exception:
            if not self.failed:
                self.failed = True
                self.console('No se pudo seguir escribiendo la traza. Revisa el espacio y los permisos de los registros.')
            return 0

    @staticmethod
    def console(message):
        try:
            os.write(2, ('[MOTOR TRAZA] '+message+'\n').encode('utf-8', errors='replace'))
        except OSError:
            pass

    def notice(self, category, message):
        now = time.monotonic()
        previous = self.console_notices.get(category)
        if previous is None or now-previous >= 1:
            self.console_notices[category] = now
            self.console(message)

    def close(self):
        self.stop_flush.set()
        if self.flusher is not threading.current_thread():
            self.flusher.join(timeout=1)
        try:
            if self.fault_stream:
                faulthandler.cancel_dump_traceback_later()
                faulthandler.disable()
                self.fault_stream.close()
            with self.lock:
                if not self.closed:
                    self.stream.flush()
                    self.stream.close()
                    self.closed = True
        except Exception:
            pass


def instrument(original, site, recorder):
    signature = inspect.signature(original)

    @functools.wraps(original)
    def observed(*args, **kwargs):
        started = time.monotonic()
        call_id = recorder.event('begin', site, parameters=usb_parameters(site, signature, args, kwargs))
        try:
            value = original(*args, **kwargs)
        except BaseException as error:
            recorder.event('error', site, call_id=call_id,
                           duration_ms=round((time.monotonic()-started)*1000, 3),
                           error={'type': type(error).__name__, 'message': str(error)[:2000],
                                  'errno': getattr(error, 'errno', None),
                                  'backend_error_code': getattr(error, 'backend_error_code', None),
                                  'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__, limit=12))})
            if isinstance(error, Exception):
                recorder.notice(site+type(error).__name__, site+': '+type(error).__name__+'; '+str(error)[:300])
            raise
        else:
            summary = packet_summary(value)
            state = {}
            if site == 'usb.connect' and args:
                own = getattr(args[0], '__dict__', {})
                state = {key: own[key] for key in ('vid', 'pid', 'interface', 'connected')
                         if isinstance(own.get(key), (int, bool, str))}
                for key in ('EP_IN', 'EP_OUT'):
                    endpoint = getattr(own.get(key), '__dict__', {})
                    state[key] = {name: endpoint[name] for name in ('bEndpointAddress', 'wMaxPacketSize')
                                  if isinstance(endpoint.get(name), int)}
            elif site == 'usb.device_candidate' and args:
                cached = getattr(args[0], '__dict__', {})
                state = {key: cached[key] for key in ('idVendor', 'idProduct', 'bDeviceClass', 'bus', 'address')
                         if isinstance(cached.get(key), int)}
            recorder.event('end', site, call_id=call_id, result=summary, state=state,
                           duration_ms=round((time.monotonic()-started)*1000, 3))
            return value
    return observed


def recover_connected_initialization(original, recorder):
    """Initialize a present Preloader once, retaining the original DA-state path.

    The vendor method otherwise initializes only when its first connect fails.
    Restore the bound connection method before init: its handshake may call
    connect again, and must never re-enter this one-shot adapter.
    """
    @functools.wraps(original)
    def configured(self, mtk, preloader):
        cdc = mtk.port.cdc
        connect = cdc.connect
        missing = object()
        previous = vars(cdc).get('connect', missing)
        restored = False

        def restore():
            nonlocal restored
            if not restored:
                if previous is missing:
                    del cdc.connect
                else:
                    cdc.connect = previous
                restored = True

        def first_connect(*args, **kwargs):
            restore()
            connected = connect(*args, **kwargs)
            if (connected and mtk.serialportname is None
                    and mtk.config.target_config is None
                    and not os.path.exists(os.path.join(mtk.config.hwparam_path, '.state'))):
                # Match the original assignment, which follows this call, so
                # the original handshake can use the handle already opened.
                cdc.connected = connected
                recorder.event('begin', 'a05.initialize_present_preloader',
                               reason='usb_open_target_config_missing_no_da_state')
                recorder.console('USB abierto sin configuración del equipo. Iniciando Preloader original; mantén el cable conectado.')
                try:
                    result = mtk.preloader.init()
                except BaseException:
                    recorder.event('error', 'a05.initialize_present_preloader',
                                   initialization_raised=True)
                    raise
                recorder.event('end', 'a05.initialize_present_preloader',
                               result=packet_summary(result),
                               target_config_ready=mtk.config.target_config is not None)
            return connected

        cdc.connect = first_connect
        try:
            return original(self, mtk, preloader)
        finally:
            restore()
    return configured


class HandshakeFailure(SystemExit):
    """Stop the vendor's nested retry loops with a controlled process exit."""
    def __init__(self, reason, message, **details):
        self.reason, self.message, self.details = reason, message, details
        super().__init__(2)

    def __str__(self):
        return self.reason+': '+self.message


def handshake_failure(recorder, reason, message, **details):
    recorder.event('failure', 'a05.handshake', code=reason, message=message, **details)
    recorder.flush()
    recorder.console(reason+': '+message+' La sesión se detuvo; no se confirmó la sincronización.')
    raise HandshakeFailure(reason, message, **details)


def bounded_run_handshake(port, recorder, *, clock=time.monotonic, sleep=time.sleep):
    """Use the diagnostic's handshake with finite transfers and resync budget."""
    cdc = port.cdc
    ep_in, ep_out = cdc.EP_IN, cdc.EP_OUT
    packet_size = max(8, min(int(ep_in.wMaxPacketSize), 4096))
    deadline = clock()+10
    sequence = b'\xa0\x0a\x50\x05'
    preloader = cdc.pid != 3

    def timeout():
        remaining = int((deadline-clock())*1000)
        if remaining <= 0:
            handshake_failure(recorder, 'SYNC_DEADLINE', 'Terminó el plazo de sincronización.')
        return max(1, min(1500, remaining))

    def send(data, phase):
        try:
            count = ep_out.write(data, timeout=timeout())
        except Exception as error:
            handshake_failure(recorder, 'SYNC_USB_WRITE', 'Falló la escritura de sincronización.',
                              phase=phase, original={'type': type(error).__name__, 'message': str(error),
                              'errno': getattr(error, 'errno', None),
                              'backend_error_code': getattr(error, 'backend_error_code', None)})
        if count != len(data):
            handshake_failure(recorder, 'SYNC_SHORT_WRITE', 'La escritura de sincronización quedó incompleta.',
                              phase=phase, expected=len(data), actual=count)

    if preloader:
        send(sequence[:1], 'wakeup')
    index = 0
    for attempt in range(1, 17):
        value = sequence[index:index+1]
        phase = 'sync.'+value.hex()
        send(value, phase)
        try:
            packet = bytes(ep_in.read(packet_size, timeout=timeout()))
        except Exception as error:
            handshake_failure(recorder, 'SYNC_USB_READ', 'Falló la lectura de sincronización.',
                              phase=phase, original={'type': type(error).__name__, 'message': str(error),
                              'errno': getattr(error, 'errno', None),
                              'backend_error_code': getattr(error, 'backend_error_code', None)})
        expected = bytes([value[0] ^ 0xff])
        recorder.event('exchange', 'a05.handshake', phase=phase, attempt=attempt,
                       sent_hex=value.hex(), received=packet_summary(packet), expected_hex=expected.hex())
        if not packet:
            handshake_failure(recorder, 'SYNC_EMPTY_READ', 'La lectura de sincronización devolvió cero bytes.', phase=phase)
        if packet[-1:] == expected:
            index += 1
            if index == len(sequence):
                recorder.event('synchronized', 'a05.handshake', exchanges=attempt)
                port.info('Device detected :)')
                return True
        elif packet[-1:] == value:
            handshake_failure(recorder, 'SYNC_ECHO', 'Se recibió eco de '+value.hex().upper()+' en lugar de '+expected.hex().upper()+'.',
                              phase=phase, received_hex=packet[-1:].hex(), expected_hex=expected.hex())
        elif not preloader:
            handshake_failure(recorder, 'SYNC_MISMATCH', 'La respuesta BROM no coincide con la esperada.',
                              phase=phase, received_hex=packet[-1:].hex(), expected_hex=expected.hex())
        else:
            index = 0
            sleep(0.02)
    handshake_failure(recorder, 'SYNC_RETRY_LIMIT', 'Se agotaron los 16 intercambios permitidos para sincronizar.')


def bounded_handshake(original, recorder, *, clock=time.monotonic, sleep=time.sleep):
    @functools.wraps(original)
    def handshake(port, maxtries=None, loop=0):
        if getattr(port.cdc, 'is_serial', False) or getattr(port, 'serialportname', None):
            return original(port, maxtries=maxtries, loop=loop)
        wait_seconds = max(1, min(900, int(os.environ.get('A05_USB_WAIT_SECONDS', '120'))))
        deadline = clock()+wait_seconds
        while not port.cdc.connected:
            if port.cdc.connect():
                break
            if clock() >= deadline:
                handshake_failure(recorder, 'SYNC_NO_USB', 'No apareció MediaTek dentro de la espera configurada.',
                                  wait_seconds=wait_seconds)
            sleep(min(0.3, max(0, deadline-clock())))
        # Unlike the original loop, an already opened CDC must also handshake.
        return port.run_handshake()
    return handshake


def pin_libusb(recorder):
    """Pin the same verified DLL used by the successful read-only diagnostic.

    Loading the DLL and querying its version neither enumerate nor open USB.
    Backend/context creation remains deferred until the original client needs it.
    """
    import ctypes
    import usb.backend.libusb1 as api
    path = Path(sys._MEIPASS)/'libusb-1.0.dll'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != LIBUSB_SHA256:
        raise RuntimeError('La DLL USB no coincide con la copia verificada del diagnóstico.')
    if getattr(api, '_lib_object', None) is not None:
        raise RuntimeError('El backend USB ya se creó antes de fijar la biblioteca verificada.')
    dll = ctypes.CDLL(str(path))
    class Version(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint16) for name in ('major', 'minor', 'micro', 'nano')]
    dll.libusb_get_version.restype = ctypes.POINTER(Version)
    version = dll.libusb_get_version().contents
    api._load_library = lambda *args, **kwargs: dll
    recorder.event('pinned', 'a05.usb_library', path=str(path), sha256=digest,
                   loader='ctypes.CDLL', version='.'.join(str(getattr(version, name)) for name in ('major', 'minor', 'micro', 'nano')),
                   usb_context_created=False, usb_device_opened=False)


def install(directory=None):
    global _recorder
    if _recorder is not None:
        return _recorder
    recorder = TraceRecorder(directory or os.environ.get('A05_TRACE_DIR') or Path.cwd())
    # The original utils module detached the initial wrappers. Keep shutdown
    # aliases attached to the live wrappers and make console records immediate.
    sys.__stdout__, sys.__stderr__ = sys.stdout, sys.stderr
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace', line_buffering=True, write_through=True)
    recorder.fault_stream = (Path(directory or os.environ.get('A05_TRACE_DIR') or Path.cwd())/'pilas_motor.log').open('a', encoding='utf-8')
    faulthandler.enable(file=recorder.fault_stream, all_threads=True)
    faulthandler.dump_traceback_later(30, repeat=True, file=recorder.fault_stream)
    # Modules were imported by the unchanged original entry point before main().
    import usb.core
    import usb.util
    from mtkclient.Library.Connection.usblib import UsbClass
    from mtkclient.Library.Connection.devicehandler import DeviceClass
    from mtkclient.Library.Port import Port
    from mtkclient.Library.mtk_preloader import Preloader
    from mtkclient.Library.DA.mtk_da_handler import DaHandler
    pin_libusb(recorder)
    DaHandler.configure_da = recover_connected_initialization(DaHandler.configure_da, recorder)
    Port.handshake = bounded_handshake(Port.handshake, recorder)
    Port.run_handshake = lambda port: bounded_run_handshake(port, recorder)
    targets = [
        (usb.core, 'find', 'usb.enumerate'),
        (usb.core.Device, '__init__', 'usb.device_candidate'),
        (usb.core.Device, 'read', 'usb.read'), (usb.core.Device, 'write', 'usb.write'),
        (usb.core.Device, 'ctrl_transfer', 'usb.control'),
        (usb.core.Device, 'get_active_configuration', 'usb.configuration'),
        (usb.core.Device, 'set_configuration', 'usb.set_configuration'),
        (usb.util, 'claim_interface', 'usb.claim_interface'),
        (UsbClass, 'connect', 'usb.connect'), (UsbClass, 'close', 'usb.close'),
        (UsbClass, 'usbread', 'client.usbread'), (DeviceClass, 'rword', 'client.rword'),
        (Port, 'handshake', 'port.handshake_loop'), (Port, 'run_handshake', 'port.handshake_attempt'),
        (Preloader, 'init', 'preloader.init'), (DaHandler, 'configure_da', 'da.configure'),
    ]
    for owner, name, site in targets:
        setattr(owner, name, instrument(getattr(owner, name), site, recorder))
    recorder.event('installed', 'trace', version=TRACE_VERSION,
                   sites=[site for _, _, site in targets], preview_limit_bytes=64,
                   usb_observers_add_requests=False, records_original_exceptions=True, live_stdio_aliases=True,
                   initialization_fix='present_usb_missing_target_config_without_da_state',
                   invokes_original_preloader_init=True, security_handlers_modified=False,
                   handshake_fix='finite_transfers_echo_stop_16_exchanges_10_second_budget',
                   usb_library_pinned_sha256=LIBUSB_SHA256,
                   periodic_stack_seconds=30, libusb_debug_requested=os.environ.get('LIBUSB_DEBUG'),
                   pyusb_debug_requested=os.environ.get('PYUSB_DEBUG'), buffered_flush_seconds=0.25)
    recorder.console('Correcciones de inicialización y sincronización cargadas; DLL USB fijada a la del diagnóstico. Se conservan los controladores originales de seguridad y operación.')
    atexit.register(recorder.close)
    _recorder = recorder
    return recorder


def self_test(terminal_echo=False):
    recorder = _recorder
    token = object()
    failure = OSError(123, 'SIMULATED_TRACE_ERROR')
    def success(value):
        return value
    def failed():
        raise failure
    assert instrument(success, 'test.return', recorder)(token) is token
    try:
        instrument(failed, 'test.exception', recorder)()
    except OSError as caught:
        assert caught is failure
    else:
        raise AssertionError('Original exception was not preserved')
    # Exercise the patched vendor configure method in the frozen engine with
    # stub devices. Stop inside stub init, before any downstream vendor call.
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from mtkclient.Library.DA.mtk_da_handler import DaHandler
    init_reached = RuntimeError('SIMULATED_ORIGINAL_INIT_REACHED')
    calls = []
    class StubCDC:
        connected = False

        def connect(self):
            return True

    def init():
        calls.append('init')
        raise init_reached

    with TemporaryDirectory(prefix='a05-init-selftest-', dir=os.environ.get('TEMP')) as state_dir:
        cdc = StubCDC()
        mtk = SimpleNamespace(port=SimpleNamespace(cdc=cdc), serialportname=None,
                              config=SimpleNamespace(target_config=None, hwparam_path=state_dir),
                              preloader=SimpleNamespace(init=init))
        try:
            DaHandler.configure_da(SimpleNamespace(info=lambda text: None), mtk, None)
        except RuntimeError as caught:
            assert caught is init_reached
        else:
            raise AssertionError('Present USB skipped original initialization')
        assert calls == ['init'] and cdc.connected is True and 'connect' not in vars(cdc)
    # Exercise the installed Port methods with fake endpoints only. Both a
    # successful exchange and the observed endless-echo failure must terminate.
    from types import MethodType
    from mtkclient.Library.Port import Port
    for echo in (False, True):
        replies = iter([b'\xa0'] if echo else [b'\x5f', b'\xf5', b'\xaf', b'\xfa'])
        frames = []
        class StubIn:
            wMaxPacketSize = 512
            def read(self, length, timeout=None):
                assert length == 512 and 0 < timeout <= 1500
                return next(replies)
        class StubOut:
            def write(self, data, timeout=None):
                assert 0 < timeout <= 1500
                frames.append(bytes(data))
                return len(data)
        port = SimpleNamespace(cdc=SimpleNamespace(connected=True, pid=0x2000, is_serial=False,
                                                   EP_IN=StubIn(), EP_OUT=StubOut()),
                               serialportname=None, info=lambda text: None)
        port.run_handshake = MethodType(Port.run_handshake, port)
        try:
            success = Port.handshake(port, maxtries=100)
        except HandshakeFailure as error:
            assert echo and error.reason == 'SYNC_ECHO' and len(frames) == 2
            if terminal_echo:
                raise
        else:
            assert not echo and success is True and frames == [b'\xa0', b'\xa0', b'\x0a', b'\x50', b'\x05']
    recorder.event('selftest_passed', 'trace', usb_access=False,
                   return_identity_preserved=True, exception_identity_preserved=True,
                   original_initialization_reached_with_stub_device=True,
                   patched_handshake_success_and_echo_stop_verified=True)
    recorder.console('Autoprueba correcta. No se abrió ningún dispositivo USB.')
