"""Bounded display buffer. Complete process output is saved before it reaches here."""
from collections import deque
import threading
import time


class UiEventBuffer:
    def __init__(self, max_output_chars=262144):
        self.max_output_chars = max_output_chars
        self.output = deque()
        self.states = deque(maxlen=64)
        self.stage = None
        self.finished = None
        self.output_chars = 0
        self.dropped_chars = self.pending_dropped = self.peak_chars = 0
        self.last_drop_notice = 0
        self.lock = threading.Lock()

    def _trim(self, limit):
        while self.output_chars > limit:
            kind, text = self.output.popleft()
            removed = min(len(text), self.output_chars-limit)
            if removed < len(text):
                self.output.appendleft((kind, text[removed:]))
            self.output_chars -= removed
            self.dropped_chars += removed
            self.pending_dropped += removed

    def put(self, item):
        kind, value = item
        with self.lock:
            if kind in {'stdout', 'stderr'}:
                if value:
                    self.output.append((kind, value))
                    self.output_chars += len(value)
                    self._trim(self.max_output_chars)
                    self.peak_chars = max(self.peak_chars, self.output_chars)
            elif kind == 'stage':
                self.stage = value
            elif kind == 'finished':
                self.finished = value
                self._trim(16384)
            else:
                self.states.append((kind, value))

    def drain(self, max_chars=24576, max_messages=48):
        items = []
        with self.lock:
            for _ in range(min(8, len(self.states))):
                items.append(self.states.popleft())
            if self.stage is not None:
                items.append(('stage', self.stage))
                self.stage = None
            now = time.monotonic()
            if self.pending_dropped and (now-self.last_drop_notice >= 1 or self.finished is not None):
                items.append(('state', f'Consola resumida: {self.pending_dropped:,} caracteres de salida rápida se conservan en los archivos completos.\n'))
                self.pending_dropped = 0
                self.last_drop_notice = now
            remaining = max_chars
            while self.output and remaining and len(items) < max_messages:
                kind, text = self.output.popleft()
                part = text[:remaining]
                if len(part) < len(text):
                    self.output.appendleft((kind, text[len(part):]))
                self.output_chars -= len(part)
                remaining -= len(part)
                if items and items[-1][0] == kind:
                    items[-1] = (kind, items[-1][1]+part)
                else:
                    items.append((kind, part))
            if self.finished is not None and not self.output and not self.states:
                items.append(('finished', self.finished))
                self.finished = None
        return items

    def snapshot(self):
        with self.lock:
            return {'pending_output_chars': self.output_chars, 'peak_output_chars': self.peak_chars,
                    'display_chars_omitted': self.dropped_chars, 'output_limit_chars': self.max_output_chars}
