"""Bounded USB diagnosis for the Galaxy A05. No flash, DA, authentication or partition API."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

VERSION = '1.2.0'
TARGET_VID = 0x0E8D
BROM_PID = 0x0003
PRELOADER_PIDS = {0x2000, 0x2001}
EXPECTED_HWCODE = 0x0707
WDT_ADDRESS = 0x10007000
WDT_VALUE = 0x22000064
DLL_HASHES = {
    'root': '3f050dee9fbf995bfbed2f4969131877fcd1b59bfd3afd2a767ff6844e872430',
    'nested': '6b24b0ee1a59cbae385dd15b06eddf2c72b2ff3a875ae279883a880136c59ec8',
}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def json_save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def error_info(error):
    return {'type': type(error).__name__, 'message': str(error),
            'errno': getattr(error, 'errno', None),
            'backend_error_code': getattr(error, 'backend_error_code', None)}


class DiagnosticError(Exception):
    def __init__(self, code, phase, message, **details):
        super().__init__(message)
        self.code, self.phase, self.details = code, phase, details


class Recorder:
    def __init__(self, directory=None, quiet=False):
        self.events = []
        self.started = time.monotonic()
        self.directory, self.quiet = directory, quiet
        self.stream = (directory/'eventos.jsonl').open('w', encoding='utf-8') if directory else None

    def event(self, kind, phase, **details):
        event = {'utc': stamp(), 'elapsed_ms': round((time.monotonic()-self.started)*1000, 2),
                 'kind': kind, 'phase': phase, **details}
        self.events.append(event)
        if self.stream:
            self.stream.write(json.dumps(event, ensure_ascii=False)+'\n')
            self.stream.flush()
        if not self.quiet:
            note = details.get('message', '')
            if kind in {'tx', 'rx'}:
                note = f"{details.get('length')} bytes: {details.get('hex')}"
            print(f'[{phase}] {kind}: {note}', flush=True)
        return event

    def close(self):
        if self.stream:
            self.stream.close()


class PacketIO:
    """Reads have deadlines and retain the native USB exception and partial bytes."""
    def __init__(self, ep_in, ep_out, recorder, timeout_ms=1500):
        self.ep_in, self.ep_out, self.recorder = ep_in, ep_out, recorder
        self.timeout_ms = timeout_ms
        self.pending = bytearray()
        self.packet_size = max(8, min(int(ep_in.wMaxPacketSize), 4096))

    def send(self, data, phase):
        self.recorder.event('tx', phase, length=len(data), hex=data.hex())
        try:
            count = self.ep_out.write(data, timeout=self.timeout_ms)
        except Exception as error:
            info = error_info(error)
            self.recorder.event('usb_error', phase, original=info)
            raise DiagnosticError('USB_WRITE_ERROR', phase, 'Falló la escritura USB.', original=info) from error
        if count != len(data):
            raise DiagnosticError('USB_SHORT_WRITE', phase, 'Escritura USB incompleta.', expected=len(data), actual=count)

    def receive(self, length, phase):
        result = bytearray()
        deadline = time.monotonic()+self.timeout_ms/1000
        while len(result) < length:
            if self.pending:
                take = min(length-len(result), len(self.pending))
                result.extend(self.pending[:take])
                del self.pending[:take]
                continue
            remaining = int((deadline-time.monotonic())*1000)
            if remaining <= 0:
                raise DiagnosticError('USB_READ_DEADLINE', phase, 'Terminó el plazo de lectura USB.',
                                      expected=length, actual=len(result), partial_hex=result.hex())
            try:
                # A full endpoint packet avoids turning an extra byte into an overflow.
                packet = bytes(self.ep_in.read(self.packet_size, timeout=max(1, remaining)))
            except Exception as error:
                info = error_info(error)
                self.recorder.event('usb_error', phase, expected=length, actual=len(result),
                                    partial_hex=result.hex(), original=info)
                raise DiagnosticError('USB_READ_ERROR', phase, 'Falló la lectura USB.', expected=length,
                                      actual=len(result), partial_hex=result.hex(), original=info) from error
            self.recorder.event('rx', phase, length=len(packet), hex=packet.hex(), expected=length)
            if not packet:
                time.sleep(0.005)
            self.pending.extend(packet)
        return bytes(result)

    def echo(self, data, phase):
        self.send(data, phase)
        received = self.receive(len(data), phase)
        if received != data:
            raise DiagnosticError('ECHO_MISMATCH', phase, 'La respuesta no coincide con el eco esperado.',
                                  expected_hex=data.hex(), received_hex=received.hex())

    def handshake_packet(self, phase):
        # The original client's Preloader handshake compares the last byte of a packet.
        try:
            packet = bytes(self.ep_in.read(self.packet_size, timeout=self.timeout_ms))
        except Exception as error:
            info = error_info(error)
            self.recorder.event('usb_error',phase,original=info)
            raise DiagnosticError('USB_READ_ERROR',phase,'Falló la lectura de sincronización.',original=info) from error
        self.recorder.event('rx',phase,length=len(packet),hex=packet.hex())
        if not packet:
            raise DiagnosticError('EMPTY_HANDSHAKE',phase,'La sincronización devolvió un paquete vacío.')
        return packet[-1:]


def decode_target_config(flags, status):
    return {'raw_hex': f'0x{flags:08x}', 'status_hex': f'0x{status:04x}',
            'flags': {name: bool(flags & mask) for name, mask in (
                ('sbc_enabled', 1), ('sla_enabled', 2), ('daa_enabled', 4),
                ('epp_enabled', 8), ('certificate_required', 16),
                ('memory_read_authentication', 32), ('memory_write_authentication', 64),
                ('command_c8_blocked', 128))},
            'unknown_bits_hex': f'0x{flags & ~0xff:08x}'}


def probe(io, recorder, *, watchdog=False, security_info=False, usb_pid=BROM_PID):
    """Bounded identification, optional target configuration or a fixed WDT test."""
    if watchdog and security_info:
        raise DiagnosticError('PROFILE_CONFLICT', 'profile',
                              'La consulta de seguridad y la prueba WDT son perfiles separados.')
    if usb_pid not in {BROM_PID, 0x2000}:
        raise DiagnosticError('WRONG_USB_MODE', 'usb.mode', 'El identificador USB no es uno de los modos de diagnóstico admitidos.',
                              usb_pid=f'{usb_pid:04x}')
    preloader = usb_pid == 0x2000
    recorder.event('phase', 'sync', message='Sincronización Preloader.' if preloader else 'Sincronización BROM; cuatro intercambios.')
    if preloader:
        io.send(b'\xa0','sync.preloader_wakeup')
    sequence=b'\xa0\x0a\x50\x05'
    index, attempts = 0, 0
    while index < len(sequence):
        value=sequence[index]
        attempts+=1
        if attempts>16:
            raise DiagnosticError('HANDSHAKE_RETRY_LIMIT','sync','No se completó la sincronización dentro del límite.')
        phase = f'sync.{value:02x}'
        io.send(bytes([value]), phase)
        reply = io.handshake_packet(phase) if preloader else io.receive(1, phase)
        expected = bytes([value ^ 0xff])
        if reply != expected:
            if preloader:
                if reply == bytes([value]):
                    raise DiagnosticError('SYNC_ECHO','sync',
                        'El teléfono devuelve eco de A0 en lugar de 5F; la sincronización no está confirmada.',
                        expected_hex=expected.hex(),received_hex=reply.hex())
                recorder.event('resync',phase,expected_hex=expected.hex(),received_hex=reply.hex())
                index=0
                continue
            raise DiagnosticError('HANDSHAKE_MISMATCH', phase, 'No coincide la respuesta de sincronización.',
                                  expected_hex=expected.hex(), received_hex=reply.hex())
        index+=1
    io.echo(b'\xfd', 'identify.command')
    raw = io.receive(4, 'identify.hwcode')
    hwcode, hwver = struct.unpack('>HH', raw)
    result = {'hwcode': f'0x{hwcode:04x}', 'hwver': f'0x{hwver:04x}',
              'expected_model_user_supplied': 'SM-A055M', 'model_read_from_device': False,
              'usb_mode_indication': 'Preloader según VID/PID' if preloader else 'BROM según VID/PID', 'watchdog_requested': watchdog,
              'watchdog_completed': False, 'security_info_requested': security_info}
    recorder.event('identified', 'identify.hwcode', **result)
    if watchdog:
        if hwcode != EXPECTED_HWCODE:
            raise DiagnosticError('CHIP_GUARD', 'watchdog.guard',
                                  'Chip distinto del esperado; se omite la escritura al registro.',
                                  expected=f'0x{EXPECTED_HWCODE:04x}', actual=f'0x{hwcode:04x}')
        recorder.event('phase', 'watchdog', message='Comprobación del registro WDT fijo del MT6768/MT6769.',
                       address=f'0x{WDT_ADDRESS:08x}', value=f'0x{WDT_VALUE:08x}')
        # This ordering is the one recovered from the original Preloader.write.
        io.echo(b'\xd4', 'watchdog.command')
        io.echo(struct.pack('>I', WDT_ADDRESS), 'watchdog.address')
        io.echo(struct.pack('>I', 1), 'watchdog.count')
        raw_status = io.receive(2, 'watchdog.status.initial')
        status, = struct.unpack('>H', raw_status)
        recorder.event('status', 'watchdog.status.initial', status=status, hex=f'0x{status:04x}')
        if status > 3:
            raise DiagnosticError('DEVICE_STATUS_REJECTED', 'watchdog.status.initial',
                                  'El teléfono devolvió un estado que no permite continuar.', status=f'0x{status:04x}')
        io.echo(struct.pack('>I', WDT_VALUE), 'watchdog.value')
        final, = struct.unpack('>H', io.receive(2, 'watchdog.status.final'))
        recorder.event('status', 'watchdog.status.final', status=final, hex=f'0x{final:04x}')
        if final > 0xff:
            raise DiagnosticError('DEVICE_STATUS_REJECTED', 'watchdog.status.final',
                                  'El teléfono rechazó la escritura al registro.', status=f'0x{final:04x}')
        result['watchdog_completed'] = True
    # In the watchdog profile this comes after the failure point, preserving its command ordering.
    io.send(b'\xfe', 'identify.boot_mode')
    mode_reply = io.receive(1, 'identify.boot_mode')[0]
    result['boot_mode_response'] = f'0x{mode_reply:02x}'
    result['protocol_mode'] = 'BROM' if mode_reply == 0xfe else 'Preloader'
    recorder.event('mode_confirmed', 'identify.boot_mode', mode=result['protocol_mode'], response=result['boot_mode_response'])
    if security_info:
        io.echo(b'\xd8', 'security.command')
        raw_config = io.receive(6, 'security.target_config')
        flags, status = struct.unpack('>IH', raw_config)
        config = decode_target_config(flags, status)
        recorder.event('target_config', 'security.target_config', **config)
        if status > 0xff:
            raise DiagnosticError('TARGET_CONFIG_REJECTED', 'security.target_config',
                                  'La consulta de configuración devolvió un estado de rechazo.',
                                  status=f'0x{status:04x}', raw_hex=raw_config.hex())
        result['target_config'] = config
    return result


class LibusbVersion(ctypes.Structure):
    _fields_ = [('major', ctypes.c_uint16), ('minor', ctypes.c_uint16), ('micro', ctypes.c_uint16),
                ('nano', ctypes.c_uint16), ('rc', ctypes.c_char_p), ('describe', ctypes.c_char_p)]


def asset_directory():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent/'diag_assets'))


def prepare_backend(args, recorder):
    import usb
    import usb.backend.libusb1 as api
    name = 'libusb-1.0.dll' if args.library == 'root' else 'libusb-1.0_nested.dll'
    path = asset_directory()/name
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != DLL_HASHES[args.library]:
        raise DiagnosticError('DLL_HASH_MISMATCH', 'backend.library', 'La DLL no coincide con la copia verificada.')
    # Explicit path bypasses the packaging hook's implicit DLL search order.
    dll = ctypes.CDLL(str(path))
    dll.libusb_get_version.restype = ctypes.POINTER(LibusbVersion)
    version = dll.libusb_get_version().contents
    recorder.event('backend', 'backend.library', message=f'libusb {version.major}.{version.minor}.{version.micro}.{version.nano}',
                   library=args.library, sha256=digest, pyusb=usb.__version__,
                   init_order=args.init_order, backend=args.backend)
    set_option = dll.libusb_set_option
    set_option.argtypes = [ctypes.c_void_p, ctypes.c_int]
    set_option.restype = ctypes.c_int
    if args.backend == 'usbdk' and args.init_order == 'early':
        rc = set_option(None, 1)
        recorder.event('option', 'backend.usbdk.before_init', result=rc)
        if rc != 0:
            raise DiagnosticError('USBDK_OPTION_FAILED', 'backend.usbdk.before_init',
                                  'No se pudo seleccionar UsbDk antes de inicializar.', result=rc)
    api._load_library = lambda *a, **kw: dll
    backend = api.get_backend(find_library=lambda _: str(path))
    if backend is None:
        raise DiagnosticError('BACKEND_UNAVAILABLE', 'backend.init', 'PyUSB no pudo inicializar libusb.')
    if args.backend == 'usbdk' and args.init_order == 'legacy':
        rc = set_option(backend.ctx, 1)
        recorder.event('option', 'backend.usbdk.after_init', result=rc)
        if rc != 0:
            raise DiagnosticError('USBDK_OPTION_FAILED', 'backend.usbdk.after_init',
                                  'No se pudo seleccionar UsbDk en el contexto.', result=rc)
    # Native C messages go to the worker's stderr file as well.
    if hasattr(dll, 'libusb_set_debug'):
        dll.libusb_set_debug.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dll.libusb_set_debug(backend.ctx, 2)
    return backend


def device_brief(device):
    return {'vid': f'{device.idVendor:04x}', 'pid': f'{device.idProduct:04x}',
            'device_class': device.bDeviceClass, 'bcdDevice': f'{device.bcdDevice:04x}',
            'bus': device.bus, 'address': device.address}


def obtain_configuration(device, recorder, usb_error_type):
    try:
        return device.get_active_configuration()
    except usb_error_type as error:
        recorder.event('configuration_error','usb.configuration',original=error_info(error))
        if getattr(error,'strerror',None) != 'Configuration not set':
            raise
        recorder.event('phase','usb.set_configuration',message='Seleccionar la configuración USB, como el cliente original.')
        device.set_configuration()
        return device.get_active_configuration()


class ArrivalGate:
    """Never start a new protocol transaction on a device present at startup."""
    def __init__(self):
        self.armed=False

    def accept(self, present):
        if not present:
            self.armed=True
            return False
        return self.armed


def worker(args):
    import usb.core
    import usb.util
    directory = Path(args.session)
    recorder = Recorder(directory)
    result = {'tool_version': VERSION, 'started_utc': stamp(), 'status': 'starting',
              'profile': 'watchdog' if args.watchdog else 'security_info' if args.security_info else 'identify',
              'partition_operations_available': False, 'da_upload_available': False}
    device, interface = None, None
    claimed = []
    try:
        backend = prepare_backend(args, recorder)
        deadline = time.monotonic()+(0 if args.inventory_only else args.wait_seconds)
        previous = None
        observed = {}
        arrival=ArrivalGate()
        blocked_existing=False
        while True:
            devices = list(usb.core.find(find_all=True, backend=backend))
            relevant = [d for d in devices if d.idVendor in {TARGET_VID, 0x04E8}]
            descriptions = [device_brief(d) for d in relevant]
            for item in descriptions:
                observed[(item['vid'],item['pid'])] = item
            result['observed_usb_modes'] = list(observed.values())
            fingerprint = json.dumps(descriptions, sort_keys=True)
            if fingerprint != previous:
                recorder.event('inventory', 'usb.enumeration', devices=descriptions)
                previous = fingerprint
            if args.inventory_only:
                result.update(status='inventory_only', devices=descriptions, commands_sent=0)
                break
            accepted_pids = {BROM_PID,0x2000} if args.transport_mode=='auto' else {BROM_PID}
            candidates = [d for d in relevant if d.idVendor == TARGET_VID and d.idProduct in accepted_pids]
            if len(candidates) > 1:
                raise DiagnosticError('MULTIPLE_BROM_DEVICES', 'usb.selection', 'Hay más de un dispositivo MediaTek candidato conectado.')
            fresh=arrival.accept(bool(candidates))
            if candidates and not fresh and not blocked_existing:
                blocked_existing=True
                recorder.event('phase','usb.await_disconnect',message='Equipo ya conectado: se espera desconexión y nueva llegada USB.')
            if candidates and fresh:
                device = candidates[0]
                result['usb_device'] = device_brief(device)
                break
            if time.monotonic() >= deadline:
                hints = [d for d in observed.values() if d['vid']==f'{TARGET_VID:04x}' and int(d['pid'],16) in PRELOADER_PIDS]
                code = 'PRELOADER_ONLY' if hints else 'NO_BROM_DEVICE'
                raise DiagnosticError(code, 'usb.enumeration', 'No apareció un dispositivo en los modos de diagnóstico seleccionados.',
                                      observed_devices=list(observed.values()),currently_present=descriptions)
            time.sleep(0.25)
        if not args.inventory_only:
            recorder.event('phase', 'usb.open', message='Abrir configuración e interfaz de datos USB.')
            device.default_timeout=args.timeout_ms
            configuration = obtain_configuration(device,recorder,usb.core.USBError)
            choices=[]
            for intf in configuration:
                endpoints = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
                ins = [e for e in endpoints if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN]
                outs = [e for e in endpoints if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT]
                if ins and outs:
                    choices.append((intf.bInterfaceClass != 0x0a, intf.bInterfaceNumber, intf, ins[0], outs[0]))
            if not choices:
                raise DiagnosticError('NO_BULK_INTERFACE', 'usb.interface', 'No se encontró un par de endpoints de datos.')
            choices.sort(key=lambda x: (x[0], x[1]))
            _, interface, intf, ep_in, ep_out = choices[0]
            recorder.event('interface', 'usb.interface', number=interface, alternate=intf.bAlternateSetting,
                           ep_in=f'0x{ep_in.bEndpointAddress:02x}', ep_out=f'0x{ep_out.bEndpointAddress:02x}',
                           max_packet=ep_in.wMaxPacketSize)
            if interface != 0 and any(i.bInterfaceNumber == 0 for i in configuration):
                recorder.event('phase','usb.claim_control',message='Reclamar interfaz de control 0, como el cliente original.')
                usb.util.claim_interface(device, 0)
                claimed.append(0)
            recorder.event('phase','usb.claim_data',message=f'Reclamar interfaz de datos {interface}.')
            usb.util.claim_interface(device, interface)
            claimed.append(interface)
            if intf.bAlternateSetting:
                device.set_interface_altsetting(interface=interface, alternate_setting=intf.bAlternateSetting)
            details = probe(PacketIO(ep_in, ep_out, recorder, args.timeout_ms), recorder,
                            watchdog=args.watchdog, security_info=args.security_info, usb_pid=device.idProduct)
            result.update(status='probe_completed', **details)
    except DiagnosticError as error:
        result.update(status='diagnostic_failure', failure={'code': error.code, 'phase': error.phase,
                                                           'message': str(error), **error.details})
        recorder.event('failure', error.phase, code=error.code, message=str(error), **error.details)
    except Exception as error:
        phase = recorder.events[-1]['phase'] if recorder.events else 'startup'
        info = error_info(error)
        result.update(status='unexpected_failure', failure={'code':'USB_BACKEND_ERROR' if info['backend_error_code'] is not None else 'RUNTIME_ERROR',
                                                          'phase':phase, **info})
        recorder.event('failure', phase, original=info)
        traceback.print_exc(file=sys.stderr)
    finally:
        # Preserve completed observations even when a later command fails.
        for event in recorder.events:
            if event['kind'] == 'identified':
                result.update({key: value for key, value in event.items()
                               if key not in {'utc', 'elapsed_ms', 'kind', 'phase'}})
            elif event['kind'] == 'mode_confirmed':
                result.update(protocol_mode=event['mode'], boot_mode_response=event['response'])
            elif event['kind'] == 'target_config':
                result['target_config_observation'] = {key: value for key, value in event.items()
                                                      if key not in {'utc', 'elapsed_ms', 'kind', 'phase'}}
        if result.get('status') == 'probe_completed' and args.watchdog:
            result['watchdog_completed'] = True
        if device is not None:
            for claimed_interface in reversed(claimed):
                try:
                    usb.util.release_interface(device, claimed_interface)
                except Exception as error:
                    recorder.event('cleanup_error','usb.release_interface',interface=claimed_interface,original=error_info(error))
            try:
                usb.util.dispose_resources(device)
            except Exception as error:
                recorder.event('cleanup_error', 'usb.release', original=error_info(error))
        result['finished_utc'] = stamp()
        result['last_event'] = recorder.events[-1] if recorder.events else None
        result['usb_write_count'] = sum(e['kind']=='tx' for e in recorder.events)
        json_save(directory/'resultado.json', result)
        recorder.close()
    return 0 if result['status'] in {'probe_completed', 'inventory_only'} else 2


def host_inventory(directory, filename='equipo.json'):
    result = {'utc': stamp(), 'windows': platform.platform(), 'python': platform.python_version(),
              'is_admin': bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name=='nt' else False}
    # Do not request serial numbers, IMEI or the serial-bearing PnP instance path.
    script = r'''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$services = @(Get-CimInstance Win32_SystemDriver -Filter "Name LIKE '%UsbDk%'" | Select-Object Name,State,StartMode)
$devices = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_0E8D|VID_04E8' } | Select-Object Status,Class,FriendlyName)
$drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -match 'VID_0E8D|VID_04E8' } | Select-Object DeviceName,DriverVersion,DriverProviderName,InfName)
@{ services=$services; devices=$devices; drivers=$drivers } | ConvertTo-Json -Depth 5 -Compress
'''
    if os.name == 'nt':
        try:
            completed = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command', script],
                capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
            stdout = completed.stdout.decode('utf-8-sig', errors='replace')
            # PowerShell 5.1 can use an OEM encoding for redirected text; names are informational.
            result['pnp'] = json.loads(stdout) if completed.returncode==0 else {'exit_code': completed.returncode,
                'stderr': completed.stderr.decode('utf-8', errors='replace')}
        except Exception as error:
            result['inventory_error'] = error_info(error)
    json_save(directory/filename, result)
    return result


def interpretation(result):
    status = result.get('status')
    if status == 'inventory_only':
        return 'Inventario terminado. No se enviaron comandos de diagnóstico al teléfono.'
    if status == 'probe_completed':
        if result.get('watchdog_completed'):
            return 'La inicialización y el intercambio del watchdog terminaron. Comparar este registro con PrimeToolX.'
        if result.get('profile') == 'security_info':
            return 'Identificación y consulta de configuración terminadas. No se probó el registro WDT.'
        return 'Identificación terminada. El registro del watchdog no se ha probado en este perfil.'
    failure = result.get('failure', {})
    phase, code = failure.get('phase','desconocida'), failure.get('code','ERROR')
    explanations = {
        'NO_BROM_DEVICE': 'No se detectó BROM; revisar modo, conexión y reconocimiento USB antes del protocolo.',
        'PRELOADER_ONLY': 'Se detectó un identificador de Preloader, pero no el de BROM esperado.',
        'CHIP_GUARD': 'El chip no coincide con el esperado; la prueba de escritura al registro se omitió.',
        'DEVICE_STATUS_REJECTED': 'Hubo respuesta del teléfono con un estado de rechazo; no es una lectura vacía.',
        'ECHO_MISMATCH': 'Se recibió una respuesta distinta del eco esperado; la secuencia se detuvo ahí.',
        'USB_READ_ERROR': 'Falló una lectura USB; el error original y los bytes parciales quedaron guardados.',
        'USBDK_OPTION_FAILED': 'Falló la selección de UsbDk; revisar el entorno antes de atribuirlo al teléfono.',
        'SYNC_ECHO': 'A0 recibió eco en vez de 5F. No confirma sincronización ni demuestra que una sesión anterior siga activa.',
        'TARGET_CONFIG_REJECTED': 'La consulta recibió un estado de rechazo; conservar el código exacto sin atribuir una causa.',
    }
    return f"{code} en {phase}. {explanations.get(code, 'Consultar el resultado y las dos salidas conservadas.')}"


def supervisor(args):
    base = Path(args.log_dir) if args.log_dir else ((Path(sys.executable).resolve().parent if getattr(sys,'frozen',False)
                                                   else Path(__file__).resolve().parent)/'registros')
    directory = base/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%fZ')
    directory.mkdir(parents=True, exist_ok=False)
    print(f'A05 Diagnóstico {VERSION}\nRegistro: {directory}\n', flush=True)
    print('Recopilando controladores y estado de UsbDk...', flush=True)
    inventory = host_inventory(directory)
    print('Administrador:', inventory.get('is_admin'), flush=True)
    if not args.inventory_only:
        print('Cierra las demás herramientas. Conecta el A05 apagado; captura BROM o Preloader al aparecer.' if args.transport_mode=='auto'
              else 'Cierra las demás herramientas. Conecta el A05 en BROM durante la espera.', flush=True)
        print('Perfil:', 'comprobar watchdog' if args.watchdog else 'identificación y consulta de configuración' if args.security_info else 'solo identificación', flush=True)
    cmd = ([sys.executable] if getattr(sys,'frozen',False) else [sys.executable, str(Path(__file__).resolve())])
    cmd += ['--worker','--session',str(directory),'--wait-seconds',str(args.wait_seconds),
            '--timeout-ms',str(args.timeout_ms),'--library',args.library,'--backend',args.backend,
            '--init-order',args.init_order,'--transport-mode',args.transport_mode]
    if args.watchdog:
        cmd.append('--watchdog')
    if args.security_info:
        cmd.append('--security-info')
    if args.inventory_only:
        cmd.append('--inventory-only')
    timed_out, interrupted, returncode = False, False, None
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    stdout_path, stderr_path = directory/'stdout.txt', directory/'stderr.txt'
    with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
        process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline = time.monotonic()+(25 if args.inventory_only else args.wait_seconds+25)
        offset = 0
        try:
            while process.poll() is None:
                time.sleep(0.15)
                with stdout_path.open('rb') as current:
                    current.seek(offset)
                    update = current.read()
                    offset += len(update)
                if update:
                    print(update.decode('utf-8',errors='replace'), end='', flush=True)
                if time.monotonic() > deadline:
                    timed_out = True
                    process.kill()
                    break
        except KeyboardInterrupt:
            interrupted = True
            process.kill()
        finally:
            returncode = process.wait(timeout=10)
        with stdout_path.open('rb') as current:
            current.seek(offset)
            remainder = current.read()
        if remainder:
            print(remainder.decode('utf-8',errors='replace'),end='',flush=True)
    result_path = directory/'resultado.json'
    if result_path.exists() and not (timed_out or interrupted):
        result = json.loads(result_path.read_text(encoding='utf-8'))
        if returncode != 0 and result.get('status') in {'probe_completed','inventory_only'}:
            result['probe_status_before_process_exit'] = result['status']
            result['status'] = 'worker_exit_error'
            result['failure'] = {'code':'WORKER_EXIT_ERROR','phase':'process.exit',
                                 'message':'El proceso terminó con error después de guardar el resultado.'}
    else:
        result = {'tool_version':VERSION, 'status':'worker_timeout' if timed_out else 'interrupted' if interrupted else 'worker_crash',
                  'failure':{'code':'WORKER_TIMEOUT' if timed_out else 'INTERRUPTED' if interrupted else 'WORKER_CRASH',
                             'phase':'supervisor', 'worker_exit_code':returncode}}
        json_save(result_path, result)
    result['worker_exit_code'] = returncode
    json_save(result_path,result)
    if not args.inventory_only:
        print('Recopilando el estado de los controladores después de la prueba...',flush=True)
        host_inventory(directory,'equipo_final.json')
    message = interpretation(result)
    (directory/'LEEME_RESULTADO.txt').write_text(message+'\n\nArchivos: resultado.json, eventos.jsonl, stdout.txt, stderr.txt, equipo.json y equipo_final.json (cuando se realiza una prueba).\n',encoding='utf-8')
    print('\n'+message+'\nRegistros guardados en: '+str(directory), flush=True)
    if stderr_path.stat().st_size:
        print('También se conservó stderr.txt, incluido cualquier mensaje nativo de libusb.',flush=True)
    return 0 if result.get('status') in {'probe_completed','inventory_only'} else 2


def self_test():
    """All tests below use scripted endpoints; none loads libusb or enumerates USB."""
    class FakeUSBError(Exception):
        errno=110
        backend_error_code=-7
    class FakeIn:
        wMaxPacketSize=64
        def __init__(self, replies): self.replies=list(replies)
        def read(self, length, timeout):
            if not self.replies: raise FakeUSBError('simulated timeout')
            reply=self.replies.pop(0)
            if isinstance(reply,Exception): raise reply
            return reply
    class FakeOut:
        def __init__(self): self.frames=[]
        def write(self,data,timeout): self.frames.append(bytes(data)); return len(data)
    sync=[b'\x5f',b'\xf5',b'\xaf',b'\xfa']
    identity=[b'\xfd',struct.pack('>HH',EXPECTED_HWCODE,0)]
    wdt=[b'\xd4',struct.pack('>I',WDT_ADDRESS),struct.pack('>I',1),b'\0\0',struct.pack('>I',WDT_VALUE),b'\0\0']
    checks=[]
    def run_case(name,replies,*,watchdog=True,security_info=False,pid=BROM_PID,expected_code=None,expected_phase=None):
        rec=Recorder(quiet=True); out=FakeOut(); result=None; failure=None
        try: result=probe(PacketIO(FakeIn(replies),out,rec,50),rec,watchdog=watchdog,security_info=security_info,usb_pid=pid)
        except DiagnosticError as error: failure=error
        if expected_code:
            assert failure and failure.code==expected_code,(name,failure)
            if expected_phase: assert failure.phase==expected_phase,(name,failure.phase)
        else: assert failure is None,(name,failure)
        checks.append({'name':name,'passed':True})
        return result,failure,out,rec
    result,_,out,_=run_case('complete_watchdog',sync+identity+wdt+[b'\xfe'])
    assert result['watchdog_completed'] and result['protocol_mode']=='BROM'
    assert out.frames==[bytes([x]) for x in b'\xa0\x0a\x50\x05']+[b'\xfd',b'\xd4',struct.pack('>I',WDT_ADDRESS),struct.pack('>I',1),struct.pack('>I',WDT_VALUE),b'\xfe']
    _,failure,out,_=run_case('missing_initial_status',sync+identity+wdt[:3],expected_code='USB_READ_ERROR',expected_phase='watchdog.status.initial')
    assert failure.details['actual']==0 and failure.details['original']['backend_error_code']==-7
    assert struct.pack('>I',WDT_VALUE) not in out.frames
    _,failure,_,_=run_case('partial_initial_status',sync+identity+wdt[:3]+[b'\0'],expected_code='USB_READ_ERROR',expected_phase='watchdog.status.initial')
    assert failure.details['actual']==1 and failure.details['partial_hex']=='00'
    _,_,out,rec=run_case('bad_count_echo',sync+identity+wdt[:2]+[b'\0\0\0\2'],expected_code='ECHO_MISMATCH',expected_phase='watchdog.count')
    assert not any(e['phase']=='watchdog.status.initial' for e in rec.events)
    _,failure,out,_=run_case('device_rejects_initial_write',sync+identity+wdt[:3]+[b'\x20\0'],expected_code='DEVICE_STATUS_REJECTED',expected_phase='watchdog.status.initial')
    assert struct.pack('>I',WDT_VALUE) not in out.frames
    _,_,_,_=run_case('device_rejects_final_write',sync+identity+wdt[:5]+[b'\x20\0'],expected_code='DEVICE_STATUS_REJECTED',expected_phase='watchdog.status.final')
    _,_,out,_=run_case('wrong_chip_guard',sync+[b'\xfd',struct.pack('>HH',0x1234,0)],expected_code='CHIP_GUARD')
    assert b'\xd4' not in out.frames
    result,_,out,_=run_case('identify_only_has_no_wdt',sync+identity+[b'\xfe'],watchdog=False)
    assert not result['watchdog_completed'] and b'\xd4' not in out.frames
    result,_,_,_=run_case('protocol_reports_preloader',sync+identity+[b'\x01'],watchdog=False)
    assert result['protocol_mode']=='Preloader'
    _,_,out,_=run_case('wrong_pid_sends_nothing',[],pid=0x2001,expected_code='WRONG_USB_MODE')
    assert not out.frames
    result,_,_,_=run_case('fragmented_valid_status',sync+identity+wdt[:3]+[b'\0',b'\0']+wdt[4:]+[b'\xfe'])
    assert result['watchdog_completed']
    run_case('handshake_stops_at_first_mismatch',[b'\0'],expected_code='HANDSHAKE_MISMATCH',expected_phase='sync.a0')
    rec=Recorder(quiet=True); out=FakeOut()
    buffered=PacketIO(FakeIn([b'\x01\x02\x03']),out,rec,50)
    assert buffered.receive(2,'buffer')==b'\x01\x02' and buffered.receive(1,'buffer')==b'\x03'
    checks.append({'name':'packet_remainder_preserved','passed':True})
    class ShortOut(FakeOut):
        def write(self,data,timeout): return 0
    try: PacketIO(FakeIn([]),ShortOut(),Recorder(quiet=True),50).send(b'\xfd','short')
    except DiagnosticError as error: assert error.code=='USB_SHORT_WRITE'
    else: raise AssertionError('short write accepted')
    checks.append({'name':'short_usb_write_stops','passed':True})
    class EmptyIn:
        wMaxPacketSize=64
        def read(self,length,timeout): return b''
    try: PacketIO(EmptyIn(),FakeOut(),Recorder(quiet=True),20).receive(2,'deadline')
    except DiagnosticError as error:
        assert error.code=='USB_READ_DEADLINE' and error.details['actual']==0
    else: raise AssertionError('unbounded empty reads')
    checks.append({'name':'empty_successful_reads_hit_deadline','passed':True})
    class FakeDisconnect(FakeUSBError):
        errno=19
        backend_error_code=-4
    _,failure,_,_=run_case('disconnect_retains_original_code',sync+identity+wdt[:3]+[FakeDisconnect('simulated disconnect')],
                          expected_code='USB_READ_ERROR',expected_phase='watchdog.status.initial')
    assert failure.details['original']['backend_error_code']==-4
    result,_,out,_=run_case('preloader_identification',sync+identity+[b'\x01'],watchdog=False,pid=0x2000)
    assert result['protocol_mode']=='Preloader' and out.frames[:2]==[b'\xa0',b'\xa0'] and b'\xd4' not in out.frames
    result,_,_,_=run_case('preloader_watchdog',sync+identity+wdt+[b'\x01'],pid=0x2000)
    assert result['watchdog_completed'] and result['protocol_mode']=='Preloader'
    result,_,out,rec=run_case('preloader_banner_resynchronization',[b'READY']+sync+identity+[b'\x01'],watchdog=False,pid=0x2000)
    assert result['protocol_mode']=='Preloader' and any(e['kind']=='resync' for e in rec.events)
    import usb
    import usb.core
    class ConfigDevice:
        def __init__(self,error=None): self.error=error; self.calls=0; self.set_calls=0; self.configuration=object()
        def get_active_configuration(self):
            self.calls+=1
            if self.calls==1 and self.error is not None: raise self.error
            return self.configuration
        def set_configuration(self): self.set_calls+=1
    configured=ConfigDevice()
    assert obtain_configuration(configured,Recorder(quiet=True),usb.core.USBError) is configured.configuration and configured.set_calls==0
    checks.append({'name':'existing_configuration_kept','passed':True})
    unconfigured=ConfigDevice(usb.core.USBError('Configuration not set'))
    assert obtain_configuration(unconfigured,Recorder(quiet=True),usb.core.USBError) is unconfigured.configuration
    assert unconfigured.set_calls==1 and unconfigured.calls==2
    checks.append({'name':'unconfigured_usb_matches_original_fallback','passed':True})
    denied=ConfigDevice(usb.core.USBError('Access denied',error_code=-3))
    try: obtain_configuration(denied,Recorder(quiet=True),usb.core.USBError)
    except usb.core.USBError: assert denied.set_calls==0
    else: raise AssertionError('access error hidden by configuration fallback')
    checks.append({'name':'unrelated_configuration_errors_preserved','passed':True})
    _,_,out,_=run_case('unexpected_preloader_echo_stops_immediately',[b'\xa0'],watchdog=False,pid=0x2000,expected_code='SYNC_ECHO')
    assert out.frames==[b'\xa0',b'\xa0']
    gate=ArrivalGate()
    assert gate.accept(True) is False and gate.accept(True) is False
    assert gate.accept(False) is False and gate.accept(True) is True
    checks.append({'name':'existing_device_requires_new_usb_arrival','passed':True})
    result,_,out,_=run_case('read_only_security_configuration',sync+identity+[b'\x01', b'\xd8', struct.pack('>IH',0xe7,0)],
                          watchdog=False,security_info=True,pid=0x2000)
    assert result['protocol_mode']=='Preloader' and result['target_config']['raw_hex']=='0x000000e7'
    assert result['target_config']['flags']['sbc_enabled'] and result['target_config']['flags']['daa_enabled']
    assert not result['target_config']['flags']['certificate_required']
    assert out.frames==[b'\xa0']+[bytes([x]) for x in b'\xa0\x0a\x50\x05']+[b'\xfd',b'\xfe',b'\xd8']
    _,failure,out,rec=run_case('security_rejection_retains_response',sync+identity+[b'\xfe',b'\xd8',struct.pack('>IH',7,0x2000)],
                             watchdog=False,security_info=True,expected_code='TARGET_CONFIG_REJECTED')
    assert failure.details['status']=='0x2000' and any(e['kind']=='mode_confirmed' for e in rec.events)
    assert b'\xd4' not in out.frames
    _,failure,out,_=run_case('partial_security_response',sync+identity+[b'\xfe',b'\xd8',b'\0\0'],
                            watchdog=False,security_info=True,expected_code='USB_READ_ERROR',expected_phase='security.target_config')
    assert failure.details['actual']==2 and failure.details['expected']==6
    _,_,out,_=run_case('conflicting_profiles_send_nothing',[],security_info=True,expected_code='PROFILE_CONFLICT')
    assert not out.frames
    output={'version':VERSION,'tests':checks,'test_count':len(checks),'all_passed':True,
            'phone_access':False,'libusb_loaded':False,'pyusb_version':usb.__version__}
    print(json.dumps(output,indent=2),flush=True)
    return 0


def main():
    for stream in (sys.stdout,sys.stderr):
        if stream is not None and hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace',line_buffering=True)
    parser=argparse.ArgumentParser(description='Diagnóstico de conexión BROM del Galaxy A05. Sin operaciones de particiones.')
    profiles=parser.add_mutually_exclusive_group()
    profiles.add_argument('--watchdog',action='store_true',help='Prueba únicamente el registro WDT fijo del MT6768/MT6769.')
    profiles.add_argument('--security-info',action='store_true',help='Identificación y consulta de configuración; sin escrituras a registros.')
    parser.add_argument('--inventory-only',action='store_true',help='Solo enumeración USB, sin comandos del protocolo.')
    parser.add_argument('--self-test',action='store_true',help='Pruebas completamente simuladas, sin acceso USB.')
    parser.add_argument('--wait-seconds',type=int,default=45)
    parser.add_argument('--timeout-ms',type=int,default=1500)
    parser.add_argument('--library',choices=['root','nested'],default='root')
    parser.add_argument('--backend',choices=['usbdk','native'],default='usbdk')
    parser.add_argument('--init-order',choices=['legacy','early'],default='legacy')
    parser.add_argument('--transport-mode',choices=['auto','brom'],default='auto')
    parser.add_argument('--log-dir')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--session',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 1<=args.wait_seconds<=900 or not 100<=args.timeout_ms<=5000:
        parser.error('La espera debe ser 1-900 s y el timeout USB 100-5000 ms.')
    if args.self_test:
        return self_test()
    if args.worker:
        if not args.session:
            parser.error('Falta el directorio de sesión del trabajador.')
        return worker(args)
    return supervisor(args)


if __name__=='__main__':
    raise SystemExit(main())
