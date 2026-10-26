"""Describe observed engine phases and elapsed time without guessing completion."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time


class EngineProgress:
    def __init__(self, directory, emit, clock=time.monotonic, interval=5):
        self.directory, self.emit, self.clock = Path(directory), emit, clock
        self.started = self.last_notice = clock()
        self.interval = interval
        self.phase = 'arranque'
        self.attempts = self.failed_attempts = self.trace_events = 0
        self.synchronized = False
        self.last_error = None
        self.handshake_failure = None
        self.last_event = None
        self.pending_usb = {}
        self.presence = None
        self.reader = None
        self.buffer = b''
        self.text_tail = ''
        self.history = (self.directory/'estado_motor.jsonl').open('a', encoding='utf-8', buffering=1)

    def set_phase(self, phase, instruction):
        if phase != self.phase:
            self.phase = phase
            self.emit('stage', instruction+'\n')

    def observe_text(self, text):
        self.text_tail = (self.text_tail+text.lower())[-2048:]
        if any(marker in self.text_tail for marker in ('waiting for connection', 'waiting for preloader')):
            self.set_phase('esperando_usb', 'El motor está esperando conexión. Conecta ahora el A05 apagado una sola vez.')
            self.text_tail = ''

    def observe_presence(self, modes):
        modes = tuple(sorted(modes))
        if modes == self.presence:
            return
        before = self.presence
        self.presence = modes
        self.history.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(),
                                      'event': 'windows_usb_presence', 'modes': modes,
                                      'elapsed_seconds': round(self.clock()-self.started, 1)})+'\n')
        self.history.flush()
        if modes and self.phase in {'esperando_usb', 'esperando_reenumeracion'}:
            self.set_phase('usb_detectado', 'Windows detectó MediaTek; esperando la sincronización del motor. Mantén el cable conectado.')
        elif before and not modes and not self.synchronized:
            self.set_phase('esperando_reenumeracion', 'MediaTek dejó de aparecer en Windows. Si el cable sigue conectado, espera; si lo retiraste, conéctalo apagado una sola vez.')

    def consume(self, record):
        self.trace_events += 1
        self.last_event = self.clock()
        event, site = record.get('event'), record.get('site', '')
        value = record.get('result', {}).get('value')
        if site.startswith('usb.'):
            if event == 'begin':
                self.pending_usb[record.get('seq')] = (site, self.clock())
            elif event in {'end', 'error'}:
                self.pending_usb.pop(record.get('call_id'), None)
        if event == 'error':
            error = record.get('error', {})
            self.last_error = {'site': site, 'type': error.get('type'), 'errno': error.get('errno'),
                               'backend_error_code': error.get('backend_error_code'),
                               'message': str(error.get('message', ''))[:300]}
        elif event == 'end' and site == 'usb.configuration' and self.last_error and self.last_error['site'] == site:
            self.last_error = None
        if site == 'a05.handshake' and event == 'failure':
            self.handshake_failure = {'code': record.get('code'), 'message': record.get('message'),
                                      'original': record.get('original')}
            self.set_phase('sincronizacion_detenida', 'Sincronización detenida: '+str(record.get('code'))+'. '+str(record.get('message')))
        if site == 'a05.initialize_present_preloader' and event == 'begin':
            self.set_phase('inicializando', 'USB abierto; iniciando Preloader original. Mantén el cable conectado mientras se confirma la sincronización.')
        elif site == 'port.handshake_attempt':
            if event == 'begin':
                self.attempts += 1
                self.set_phase('sincronizando', 'Se está intentando sincronizar el teléfono. Mantén el cable conectado.')
            elif event == 'end' and value is True:
                self.synchronized = True
                self.set_phase('inicializando', 'Sincronización confirmada. Continúa la inicialización; mantén el cable conectado.')
            elif event in {'end', 'error'}:
                self.failed_attempts += 1
                if not self.handshake_failure:
                    self.set_phase('sincronizacion_fallida', 'Falló la sincronización y el motor reintenta. Mantén el cable conectado; el error queda registrado.')
        elif site == 'da.configure' and event == 'end':
            if value is None and record.get('result', {}).get('type') is None:
                self.set_phase('inicializacion_incompleta', 'El motor terminó la inicialización sin una conexión preparada. Revisa el resultado antes de reconectar.')
            else:
                self.set_phase('operacion', 'Inicialización terminada; el motor continúa. Mantén el cable conectado hasta que termine.')

    def poll(self):
        path = self.directory/'traza_motor.jsonl'
        if self.reader is None and path.is_file():
            self.reader = path.open('rb')
        if self.reader is not None:
            self.buffer += self.reader.read(262144)
            lines = self.buffer.split(b'\n')
            self.buffer = lines.pop()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        self.consume(record)
                except (ValueError, TypeError):
                    self.emit('state', 'Se encontró un registro de traza incompleto; se conservó el archivo original.\n')
        self.heartbeat()

    def snapshot(self):
        now = self.clock()
        waiting = max(self.pending_usb.values(), key=lambda item: now-item[1], default=None)
        return {'phase': self.phase, 'elapsed_seconds': round(now-self.started, 1),
                'handshake_attempts': self.attempts, 'failed_handshake_attempts': self.failed_attempts,
                'handshake_confirmed': self.synchronized, 'trace_events': self.trace_events,
                'handshake_failure': self.handshake_failure,
                'windows_usb_modes': self.presence,
                'seconds_since_last_trace': None if self.last_event is None else round(now-self.last_event, 1),
                'pending_usb_call': None if waiting is None else {'site': waiting[0], 'seconds': round(now-waiting[1], 1)},
                'last_error': self.last_error}

    def heartbeat(self, force=False):
        now = self.clock()
        if not force and now-self.last_notice < self.interval:
            return
        self.last_notice = now
        state = self.snapshot()
        elapsed = int(state['elapsed_seconds'])
        message = f"Tiempo: {elapsed}s. Fase: {self.phase.replace('_', ' ')}."
        if self.attempts:
            message += f' Intentos de sincronización: {self.attempts}; fallidos: {self.failed_attempts}.'
        if state['pending_usb_call']:
            call = state['pending_usb_call']
            message += f" {call['site']} lleva {call['seconds']}s esperando respuesta."
        if self.last_error:
            message += f" Último error: {self.last_error['type']} (errno {self.last_error['errno']}, backend {self.last_error['backend_error_code']})."
        if self.phase == 'esperando_usb':
            message += ' Esperando conexión; conecta el A05 apagado una sola vez.'
        elif self.phase == 'sincronizacion_fallida':
            message += 'No hay avance confirmado. Evita reconectar repetidamente; puedes usar Detener.'
        elif self.phase in {'sincronizando', 'usb_detectado', 'inicializando', 'operacion'}:
            message += ' Mantén el cable conectado hasta el siguiente aviso.'
        elif self.trace_events == 0:
            message += ' Aún no llegó la traza; el proceso sigue activo.'
        self.emit('stage', message+'\n')
        self.history.write(json.dumps({'utc': datetime.now(timezone.utc).isoformat(), **state}, ensure_ascii=False)+'\n')
        self.history.flush()

    def close(self):
        if self.reader:
            self.reader.close()
        self.history.close()
