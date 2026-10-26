"""Editable A05 orchestration reconstructed from the local PrimeToolX 27 flow.

The shared MTK backend remains an external executable. Private operation
arguments are loaded from a local profile and are never included in reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import codecs
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from a05_usb_guard import present_mediatek_usb, wait_for_usb_absence
from a05_progress import EngineProgress

MODEL = 'SM-A055M'
VERSION = '1.0.8'
RECONNECT_MESSAGE = 'please disconnect, start mtkclient and reconnect.'


def completion_message(result):
    """A process exit code does not verify an operation on the phone."""
    labels = {
        'completed': 'Consulta USB completada.' if result.get('kind') == 'consulta' else 'Proceso terminado.',
        'completed_unverified': 'El motor terminó; el resultado de FRP no está confirmado.',
        'reconnect_required': ('Inicialización incompleta. Desconecta el A05, inicia FRP y '
                               'conéctalo apagado cuando el motor indique que espera conexión.'),
        'failed': 'La consulta USB no se completó.' if result.get('kind') == 'consulta' else 'El motor terminó con error.',
        'cancelled': 'Proceso detenido.',
        'timeout': 'Terminó el plazo de la consulta.',
        'runtime_error': 'Error del controlador de procesos.',
        'disconnect_timeout': 'No se confirmó la desconexión USB a tiempo. El motor no se inició.',
        'usb_inventory_error': 'No se pudo comprobar la desconexión USB. El motor no se inició.',
        'native_crash': 'El motor se cerró por un fallo nativo (0xC0000005). Se conservaron los registros.',
    }
    message = labels.get(result.get('status'), 'Proceso terminado.')
    failure = (result.get('engine_progress') or {}).get('handshake_failure')
    if result.get('status') == 'failed' and failure:
        message = 'Sincronización detenida ['+str(failure.get('code'))+']. '+str(failure.get('message'))
    if 'returncode' in result:
        message += ' Código del proceso: '+str(result['returncode'])+'.'
    return message


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.new')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def resolve_path(config_path, value):
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else Path(config_path).resolve().parent/path


def load_settings(config_path):
    settings = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    if settings.get('model') != MODEL:
        raise ValueError('Esta herramienta corresponde al SM-A055M.')
    if type(settings.get('skip_watchdog')) is not bool:
        raise ValueError('skip_watchdog debe ser true o false.')
    wait = settings.get('wait_seconds', 120)
    if type(wait) is not int or not 1 <= wait <= 900:
        raise ValueError('wait_seconds debe estar entre 1 y 900.')
    for name in ('backend', 'diagnostic', 'private_profile', 'private_logs'):
        if not isinstance(settings.get(name), str) or not settings[name]:
            raise ValueError('Falta una ruta en la configuración.')
    for name in ('debug_engine', 'engine_trace'):
        if name in settings and type(settings[name]) is not bool:
            raise ValueError(name+' debe ser true o false.')
    return settings


def load_profile(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('Falta el perfil local. Usa Importar perfil para habilitar la función del A05.')
    profile = json.loads(path.read_text(encoding='utf-8-sig'))
    return validate_profile(profile)


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get('model') != MODEL:
        raise ValueError('El perfil local no corresponde al SM-A055M.')
    arguments = profile.get('arguments')
    if not isinstance(arguments, list) or not arguments or any(
            not isinstance(item, str) or not item or '\0' in item for item in arguments):
        raise ValueError('El perfil local necesita una lista válida de argumentos.')
    # This A05 profile used no protected loader. Do not invent another model's flow.
    if str(profile.get('loader', 'off')).lower() not in {'off', 'n/a', 'none', ''}:
        raise ValueError('Este perfil A05 no admite un cargador externo.')
    if '--loader' in arguments or any(item.startswith('--loader=') for item in arguments):
        raise ValueError('El perfil contiene un cargador externo no previsto.')
    return profile


def import_private_profile(source, destination):
    data = json.loads(Path(source).read_text(encoding='utf-8-sig'))
    if isinstance(data, dict) and data.get('model') == MODEL and 'arguments' in data:
        profile = load_profile(source)
    else:
        def records(value):
            if isinstance(value, dict):
                if any(isinstance(item, str) and MODEL in item for item in value.values()):
                    yield value
                for item in value.values():
                    yield from records(item)
            elif isinstance(value, list):
                for item in value:
                    yield from records(item)
        matches = list(records(data))
        if len(matches) != 1 or not isinstance(matches[0].get('comando_base'), str):
            raise ValueError('El archivo debe contener un único perfil SM-A055M.')
        profile = {'model': MODEL, 'arguments': matches[0]['comando_base'].split(),
                   'loader': matches[0].get('loader', 'off')}
        # Validate without ever including arguments in an exception or public report.
        if not profile['arguments'] or str(profile['loader']).lower() not in {'off', 'n/a', 'none', ''}:
            raise ValueError('El perfil necesita otro flujo de cargador y no se importó.')
    validate_profile(profile)
    destination = Path(destination)
    if destination.exists():
        old = destination.read_bytes()
        backup = destination.with_name(destination.name+'.backup-'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f'))
        backup.write_bytes(old)
    save_json(destination, profile)
    load_profile(destination)


def set_watchdog_option(arguments, enabled):
    """Normalize only the recovered --skipwdt option; preserve other arguments."""
    result = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == '--skipwdt':
            if index+1 >= len(arguments) or arguments[index+1].startswith('--'):
                raise ValueError('La opción skipwdt del perfil necesita un valor.')
            index += 2
            continue
        if item.startswith('--skipwdt='):
            index += 1
            continue
        result.append(item)
        index += 1
    if enabled:
        result.extend(['--skipwdt', '1'])
    return result


def operation_arguments(profile, settings):
    arguments = list(profile['arguments'])
    if settings.get('debug_engine', False) and '--debugmode' not in arguments:
        arguments.append('--debugmode')
    return set_watchdog_option(arguments, settings['skip_watchdog'])


def validate_environment(config_path, action='all'):
    components = {'consulta': ('diagnostic',), 'perfil': ('backend',), 'all': ('backend', 'diagnostic')}
    if action not in components:
        raise ValueError('Acción no admitida.')
    settings = load_settings(config_path)
    report = {'tool_version': VERSION, 'model': MODEL, 'action': action, 'profile_valid': None,
              'backend_verified': None, 'diagnostic_verified': None,
              'skip_watchdog': settings['skip_watchdog'], 'argument_count': None, 'usb_access': False}
    for key in components[action]:
        path = resolve_path(config_path, settings[key])
        if not path.is_file():
            raise ValueError('Falta un componente de la herramienta: '+key)
        if file_hash(path) != settings.get(key+'_sha256'):
            raise ValueError('La huella del componente no coincide: '+key)
        report[key+'_verified'] = True
    if action != 'consulta':
        profile_path = resolve_path(config_path, settings['private_profile'])
        profile = load_profile(profile_path)
        normalized = operation_arguments(profile, settings)
        report.update(profile_valid=True, argument_count=len(normalized))
    return report


@dataclass(frozen=True)
class RunPlan:
    command: tuple[str, ...]
    cwd: Path
    kind: str
    hard_timeout_seconds: int = 0
    disconnect_timeout_seconds: float = 0
    engine_trace: bool = False


def build_plan(config_path, kind):
    """Reconstruct the A05 launch function without a hidden default operation."""
    if kind not in {'consulta', 'perfil'}:
        raise ValueError('Acción no admitida.')
    validate_environment(config_path, action=kind)
    settings = load_settings(config_path)
    base = resolve_path(config_path, settings['private_logs'])
    session = base/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%fZ')
    session.mkdir(parents=True, exist_ok=False)
    if kind == 'consulta':
        executable = resolve_path(config_path, settings['diagnostic'])
        command = (str(executable), '--security-info', '--wait-seconds', str(settings['wait_seconds']),
                   '--log-dir', str(session/'usb'))
        return RunPlan(command, session, kind, settings['wait_seconds']+85)
    profile = load_profile(resolve_path(config_path, settings['private_profile']))
    arguments = operation_arguments(profile, settings)
    executable = resolve_path(config_path, settings['backend'])
    # No shell, no command concatenation, and no additional command or partition.
    return RunPlan((str(executable), *arguments), session, kind,
                   disconnect_timeout_seconds=settings['wait_seconds'], engine_trace=settings.get('engine_trace', False))


class ProcessRunner:
    """Keep the UI responsive and retain stdout and stderr in separate private files."""
    def __init__(self):
        self.cancel_event = threading.Event()
        self.process = None
        self.lock = threading.Lock()

    def cancel(self):
        self.cancel_event.set()

    def _stop_own_tree(self, process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            stopped = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                     capture_output=True, timeout=10,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            if stopped.returncode and process.poll() is None:
                process.kill()
        else:
            process.terminate()

    def run(self, plan, emit):
        started = time.monotonic()
        result = {'tool_version': VERSION, 'model': MODEL, 'kind': plan.kind,
                  'status': 'starting', 'started_utc': datetime.now(timezone.utc).isoformat()}
        process = None
        readers = []
        messages = queue.Queue(maxsize=128)
        reader_shutdown = threading.Event()
        active_readers = 0
        output_tail = {'stdout': '', 'stderr': ''}
        reconnect_observed = False
        progress = None
        progress_error_reported = False
        last_generic_notice = time.monotonic()
        last_inventory_check = 0
        inventory_warning_reported = False
        try:
            if self.cancel_event.is_set():
                result['status'] = 'cancelled'
                return result
            if plan.disconnect_timeout_seconds:
                gate = wait_for_usb_absence(present_mediatek_usb, self.cancel_event, emit,
                                           plan.disconnect_timeout_seconds)
                result['usb_disconnect_check'] = gate
                if gate['status'] != 'absent':
                    result['status'] = {'cancelled': 'cancelled', 'timeout': 'disconnect_timeout',
                                        'inventory_error': 'usb_inventory_error'}[gate['status']]
                    return result
                if self.cancel_event.is_set():
                    result['status'] = 'cancelled'
                    return result
            environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
            runtime_temp = Path(os.environ.get('LOCALAPPDATA', str(plan.cwd)))/'A05Editable/temporal'
            runtime_temp.mkdir(parents=True, exist_ok=True)
            environment.update(TEMP=str(runtime_temp), TMP=str(runtime_temp))
            result['runtime_temp'] = str(runtime_temp)
            result['runtime_temp_free_bytes'] = shutil.disk_usage(runtime_temp).free
            emit('state', 'Preparando archivos temporales de la herramienta; el motor puede tardar unos segundos en arrancar.\n')
            if plan.engine_trace:
                environment['A05_TRACE_DIR'] = str(plan.cwd)
                environment['LIBUSB_DEBUG'] = '2'
                environment['PYUSB_DEBUG'] = 'warning'
                environment['PYUSB_LOG_FILENAME'] = str(plan.cwd/'pyusb_debug.log')
                environment['A05_USB_WAIT_SECONDS'] = str(plan.disconnect_timeout_seconds or 120)
                (plan.cwd/'logs').mkdir(exist_ok=True)
            process = subprocess.Popen(plan.command, cwd=plan.cwd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            with self.lock:
                self.process = process
            if plan.engine_trace:
                progress = EngineProgress(plan.cwd, emit)

            def enqueue(item):
                while not reader_shutdown.is_set():
                    try:
                        messages.put(item, timeout=0.1)
                        return
                    except queue.Full:
                        continue

            def read_stream(stream, name):
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                try:
                    with (plan.cwd/(name+'.txt')).open('wb', buffering=262144) as saved:
                        last_flush = time.monotonic()
                        while chunk := stream.read1(16384):
                            saved.write(chunk)
                            if time.monotonic()-last_flush >= 0.25:
                                saved.flush()
                                last_flush = time.monotonic()
                            decoded = decoder.decode(chunk)
                            if decoded:
                                enqueue((name, decoded))
                        remaining = decoder.decode(b'', final=True)
                        if remaining:
                            enqueue((name, remaining))
                finally:
                    stream.close()
                    enqueue(('reader_finished', name))

            for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
                reader = threading.Thread(target=read_stream, args=(stream, name), daemon=True)
                reader.start()
                readers.append(reader)
                active_readers += 1
            emit('state', 'Proceso iniciado. Las dos salidas se conservan en registros locales privados.\n')
            stopped = False
            connection_prompt_observed = False
            while process.poll() is None or active_readers:
                if progress is not None and time.monotonic()-last_inventory_check >= 1:
                    last_inventory_check = time.monotonic()
                    try:
                        progress.observe_presence(present_mediatek_usb())
                    except Exception as error:
                        if not inventory_warning_reported:
                            inventory_warning_reported = True
                            result['active_inventory_error'] = type(error).__name__
                            emit('state', 'No se pudo actualizar la presencia USB de Windows; la traza del motor continúa.\n')
                if progress is not None and not progress_error_reported:
                    try:
                        progress.poll()
                    except Exception as error:
                        progress_error_reported = True
                        result['progress_monitor_error'] = type(error).__name__
                        emit('state', 'No se pudo actualizar el resumen de la traza. Se sigue conservando la salida del motor.\n')
                elif progress is None and time.monotonic()-last_generic_notice >= 5:
                    last_generic_notice = time.monotonic()
                    emit('stage', 'El proceso sigue en curso: '+str(int(time.monotonic()-started))+'s transcurridos. Aún no hay resultado final.\n')
                if not stopped and (self.cancel_event.is_set() or (
                        plan.hard_timeout_seconds and time.monotonic()-started > plan.hard_timeout_seconds)):
                    result['status'] = 'cancelled' if self.cancel_event.is_set() else 'timeout'
                    self._stop_own_tree(process)
                    stopped = True
                try:
                    name, message = messages.get(timeout=0.10)
                except queue.Empty:
                    continue
                if name == 'reader_finished':
                    active_readers -= 1
                else:
                    output_tail[name] = (output_tail[name]+message.lower())[-1024:]
                    reconnect_observed |= RECONNECT_MESSAGE in output_tail[name]
                    emit(name, message)
                    if progress is not None:
                        progress.observe_text(message)
                    elif plan.disconnect_timeout_seconds and not connection_prompt_observed and any(
                            marker in output_tail[name] for marker in ('waiting for connection', 'waiting for preloader')):
                        connection_prompt_observed = True
                        emit('stage', 'El motor está esperando conexión. Conecta ahora el A05 apagado.\n')
            result['returncode'] = process.wait(timeout=10)
            result['reconnect_message_observed'] = reconnect_observed
            if not stopped:
                if result['returncode'] != 0:
                    result['status'] = ('native_crash' if result['returncode'] & 0xffffffff == 0xc0000005 else 'failed')
                elif plan.kind == 'perfil':
                    result['status'] = 'reconnect_required' if reconnect_observed else 'completed_unverified'
                else:
                    result['status'] = 'completed'
        except Exception as error:
            # A subprocess exception may embed argv. Preserve its type, not private arguments.
            result.update(status='runtime_error', error_type=type(error).__name__)
            emit('stderr', 'Falló el controlador de procesos: '+type(error).__name__+'\n')
        finally:
            if process is not None and process.poll() is None:
                self._stop_own_tree(process)
                process.wait(timeout=10)
            reader_shutdown.set()
            for reader in readers:
                reader.join(timeout=2)
            if progress is not None:
                try:
                    progress.poll()
                    result['engine_progress'] = progress.snapshot()
                except Exception as error:
                    result['progress_monitor_error'] = type(error).__name__
                finally:
                    progress.close()
            with self.lock:
                self.process = None
            result.update(elapsed_seconds=round(time.monotonic()-started, 3),
                          finished_utc=datetime.now(timezone.utc).isoformat())
            save_json(plan.cwd/'resultado_controlador.json', result)
        return result
