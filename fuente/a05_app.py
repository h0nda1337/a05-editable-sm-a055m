"""Independent editable window for the reconstructed SM-A055M workflow."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from a05_core import (MODEL, VERSION, ProcessRunner, build_plan, load_settings,
                      resolve_path, save_json, validate_environment, import_private_profile, completion_message)
from a05_usb_guard import present_mediatek_usb
from a05_console import UiEventBuffer


def default_config():
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    return base/'configuracion.json'


def run_ui_load_test(config_path, report_path):
    """Exercise Tk with a synthetic output flood and a fake Stop target; no USB."""
    root = tk.Tk()
    root.withdraw()
    window = A05Window(root, config_path)
    class FakeRunner:
        cancelled = False
        def cancel(self):
            self.cancelled = True
    runner = FakeRunner()
    window.runner = runner
    window.set_running(True)
    completed = threading.Event()
    produced = [0]
    chunk = ('libusb: debug synthetic display stress\n'*100)
    def produce():
        for index in range(100000):
            window.events.put(('stderr', chunk))
            produced[0] += len(chunk)
            if index % 1000 == 0:
                window.events.put(('stage', 'Prueba sintética de consola; no se usa USB.\n'))
        completed.set()
    started = previous = time.monotonic()
    gaps = []
    outcome = {}
    def tick():
        nonlocal previous
        now = time.monotonic()
        gaps.append(now-previous)
        previous = now
        elapsed = now-started
        if elapsed > 10 or completed.is_set() and elapsed >= 1.5:
            state = window.events.snapshot()
            outcome.update(all_passed=(completed.is_set() and runner.cancelled and max(gaps) < 1
                                       and state['peak_output_chars'] <= state['output_limit_chars']),
                           tool_version=VERSION, simulated_input_chars=produced[0],
                           max_event_loop_delay_ms=round(max(gaps)*1000, 2),
                           gui_timer_callbacks=len(gaps), stop_button_responded=runner.cancelled,
                           display_buffer=state, usb_access=False, backend_started=False)
            window.running = False
            window.close()
            return
        root.after(10, tick)
    root.after(10, tick)
    root.after(150, window.stop_button.invoke)
    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    root.mainloop()
    producer.join(timeout=1)
    save_json(report_path, outcome)
    return 0 if outcome.get('all_passed') else 2


class A05Window:
    def __init__(self, root, config_path):
        self.root, self.config_path = root, Path(config_path).resolve()
        self.runner = None
        self.events = UiEventBuffer()
        self.running = False
        self.query_ready = False
        self.profile_ready = False
        self.last_session = None
        self.settings = load_settings(self.config_path)
        root.title('A05 Editable '+VERSION+' · '+MODEL)
        root.geometry('930x660')
        root.minsize(810, 570)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Title.TLabel', font=('Segoe UI', 20, 'bold'))
        style.configure('Muted.TLabel', foreground='#596579')
        body = ttk.Frame(root, padding=22)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='A05 Editable', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='Samsung Galaxy A05 · SM-A055M · código de control editable',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 18))
        controls = ttk.LabelFrame(body, text='Conexión y ejecución', padding=12)
        controls.pack(fill='x')
        self.skip = tk.BooleanVar(value=self.settings['skip_watchdog'])
        self.wait = tk.StringVar(value=str(self.settings['wait_seconds']))
        self.skip_control = ttk.Checkbutton(controls, text='Omitir inicialización del watchdog (skipwdt)', variable=self.skip)
        self.skip_control.grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(controls, text='Espera de conexión USB:').grid(row=1, column=0, sticky='w', pady=(9, 4))
        self.wait_control = ttk.Spinbox(controls, from_=1, to=900, textvariable=self.wait, width=8)
        self.wait_control.grid(row=1, column=1, sticky='w', padx=(10, 0), pady=(9, 4))
        ttk.Label(controls, text='segundos', style='Muted.TLabel').grid(row=1, column=2, sticky='w', padx=6, pady=(9, 4))
        buttons = ttk.Frame(controls)
        buttons.grid(row=2, column=0, columnspan=4, sticky='w', pady=(10, 2))
        self.query_button = ttk.Button(buttons, text='Consultar estado USB', command=lambda: self.start('consulta'))
        self.query_button.pack(side='left')
        self.run_button = ttk.Button(buttons, text='Ejecutar FRP del A05', command=lambda: self.start('perfil'))
        self.run_button.pack(side='left', padx=8)
        self.stop_button = ttk.Button(buttons, text='Detener', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Label(controls, text='La consulta identifica chip, modo y configuración. FRP utiliza el perfil local del SM-A055M.',
                  style='Muted.TLabel').grid(row=3, column=0, columnspan=4, sticky='w', pady=(10, 0))
        ttk.Label(controls, text='FRP comprueba primero que MediaTek se desconectó. Conecta el A05 apagado cuando el motor indique que espera conexión.',
                  wraplength=720, style='Muted.TLabel').grid(row=4, column=0, columnspan=4, sticky='w', pady=(5, 0))
        tools = ttk.Frame(body)
        tools.pack(fill='x', pady=12)
        self.edit_core = ttk.Button(tools, text='Editar código', command=self.open_source)
        self.edit_core.pack(side='left')
        self.edit_profile = ttk.Button(tools, text='Editar perfil privado', command=self.open_profile)
        self.edit_profile.pack(side='left', padx=8)
        self.import_profile_button = ttk.Button(tools, text='Importar perfil', command=self.import_profile)
        self.import_profile_button.pack(side='left', padx=(0, 8))
        ttk.Button(tools, text='Abrir registros locales', command=self.open_logs).pack(side='left')
        self.status = tk.StringVar(value='Validando archivos locales…')
        ttk.Label(body, textvariable=self.status, wraplength=850).pack(anchor='w', pady=(0, 7))
        self.progress = ttk.Progressbar(body, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 10))
        self.console = ScrolledText(body, font=('Consolas', 10), height=15,
                                    background='#14202b', foreground='#e2e8f0', insertbackground='white',
                                    relief='flat', padx=12, pady=10, wrap='none')
        self.console.pack(fill='both', expand=True)
        self.console.tag_configure('stderr', foreground='#ffb485')
        self.console.tag_configure('state', foreground='#81cfed')
        self.console.configure(state='disabled')
        ttk.Label(body, text='Interfaz '+VERSION+' · motor MTK externo · ninguna operación se inicia al abrir',
                  style='Muted.TLabel').pack(anchor='w', pady=(10, 0))
        self.log('state', 'Cierra otras herramientas antes de una prueba USB. La consulta acepta Preloader o BROM.\n')
        self.refresh_environment(notify=True)
        self.poll_id = root.after(80, self.poll)

    def refresh_environment(self, notify=False):
        issues = {}
        for action, attribute in (('consulta', 'query_ready'), ('perfil', 'profile_ready')):
            try:
                validate_environment(self.config_path, action=action)
                setattr(self, attribute, True)
            except Exception as error:
                setattr(self, attribute, False)
                issues[action] = str(error)
        self.query_button.configure(state='normal' if self.query_ready and not self.running else 'disabled')
        self.run_button.configure(state='normal' if self.profile_ready and not self.running else 'disabled')
        profile = resolve_path(self.config_path, self.settings['private_profile'])
        self.edit_profile.configure(state='normal' if profile.is_file() and not self.running else 'disabled')
        if notify:
            if self.query_ready and self.profile_ready:
                self.status.set('Listo. Consulta USB y FRP del A05 disponibles.')
            elif self.query_ready:
                self.status.set('Consulta USB disponible. Importa el perfil para habilitar FRP del A05.')
            else:
                self.status.set('Revisar los componentes de la consulta USB.')
            for action, issue in issues.items():
                self.log('state', ('Consulta USB: ' if action == 'consulta' else 'Perfil A05: ')+issue+'\n')

    def log(self, kind, message):
        self.log_batch([(kind, message)])

    def log_batch(self, items):
        if not items:
            return
        follow = self.console.yview()[1] >= 0.98
        self.console.configure(state='normal')
        for kind, message in items:
            self.console.insert('end', message, kind)
        characters = self.console.count('1.0', 'end-1c', 'chars')
        if characters and characters[0] > 131072:
            self.console.delete('1.0', 'end-98304c')
        lines = int(self.console.index('end-1c').split('.')[0])
        if lines > 1500:
            self.console.delete('1.0', str(lines-1200)+'.0')
        if follow:
            self.console.see('end')
        self.console.configure(state='disabled')

    def set_running(self, running):
        self.running = running
        for widget in (self.skip_control, self.wait_control,
                       self.edit_core, self.edit_profile, self.import_profile_button):
            widget.configure(state='disabled' if running else 'normal')
        self.stop_button.configure(state='normal' if running else 'disabled')
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()
        self.refresh_environment()

    def start(self, kind):
        if self.running:
            return
        try:
            settings = load_settings(self.config_path)
            settings.update(skip_watchdog=self.skip.get(), wait_seconds=int(self.wait.get()))
            if not 1 <= settings['wait_seconds'] <= 900:
                raise ValueError('La espera debe estar entre 1 y 900 segundos.')
            save_json(self.config_path, settings)
            self.settings = load_settings(self.config_path)
            plan = build_plan(self.config_path, kind)
        except Exception as error:
            self.log('stderr', 'No se inició la prueba: '+str(error)+'\n')
            return
        self.runner = ProcessRunner()
        self.last_session = plan.cwd
        self.set_running(True)
        self.status.set('Consulta USB en curso…' if kind == 'consulta' else 'Preparando FRP: comprobando desconexión USB…')
        self.log('state', '\n'+self.status.get()+'\n')
        if kind == 'perfil':
            self.log('state', 'Mantén el A05 desconectado hasta que el motor muestre «Waiting for connection» o «Waiting for PreLoader». Después conéctalo apagado.\n')
            self.log('state', 'Traza detallada activada. Habrá un aviso cada 5 segundos con la fase y el tiempo transcurrido. Después de conectarlo, espera el siguiente aviso antes de reconectar.\n')

        def work():
            try:
                result = self.runner.run(plan, lambda event, text: self.events.put((event, text)))
            except Exception as error:
                result = {'status': 'runtime_error', 'error_type': type(error).__name__}
            self.events.put(('finished', result))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.running and self.runner is not None:
            self.runner.cancel()
            self.stop_button.configure(state='disabled')
            self.status.set('Deteniendo el proceso iniciado por esta herramienta…')

    def poll(self):
        display = []
        for kind, value in self.events.drain():
            if kind == 'finished':
                self.set_running(False)
                text = completion_message(value)
                self.status.set(text)
                display.append(('state', '\n'+text+'\n'))
            elif kind == 'stage':
                self.status.set(value.strip())
                display.append(('state', value))
            else:
                display.append((kind, value))
        self.log_batch(display)
        self.poll_id = self.root.after(80, self.poll)

    def open_source(self):
        source = self.config_path.parent/'fuente/a05_core.py'
        subprocess.Popen(['notepad.exe', str(source)])

    def open_profile(self):
        profile = resolve_path(self.config_path, self.settings['private_profile'])
        subprocess.Popen(['notepad.exe', str(profile)])

    def open_logs(self):
        directory = self.last_session or resolve_path(self.config_path, self.settings['private_logs'])
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(directory))

    def import_profile(self):
        from tkinter import filedialog
        selected = filedialog.askopenfilename(parent=self.root, title='Importar perfil local del SM-A055M',
                                              filetypes=[('JSON', '*.json')])
        if not selected:
            return
        try:
            import_private_profile(selected, resolve_path(self.config_path, self.settings['private_profile']))
            validate_environment(self.config_path, action='perfil')
            self.refresh_environment()
            self.status.set('Perfil local importado y verificado.')
            self.log('state', 'Perfil local importado. No se ejecutó ninguna operación.\n')
        except Exception as error:
            self.log('stderr', 'No se pudo importar el perfil: '+str(error)+'\n')

    def close(self):
        if self.running:
            self.log('state', 'Usa Detener y espera al resultado antes de cerrar la ventana.\n')
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=default_config())
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--ui-smoke', action='store_true')
    parser.add_argument('--inventory-check', action='store_true')
    parser.add_argument('--ui-load-test', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.ui_load_test:
        return run_ui_load_test(args.config, args.report or args.config.parent/'verificacion_carga_gui.json')
    if args.inventory_check:
        result = {'tool_version': VERSION, 'usb_device_opened': False, 'backend_started': False}
        try:
            result.update(known_mediatek_modes=present_mediatek_usb(), windows_present_inventory_read=True,
                          all_passed=True)
        except Exception as error:
            result.update(all_passed=False, error_type=type(error).__name__)
        save_json(args.report or args.config.parent/'verificacion_inventario.json', result)
        return 0 if result['all_passed'] else 2
    if args.verify or args.ui_smoke:
        try:
            result = validate_environment(args.config, action='consulta' if args.ui_smoke else 'all')
            if args.ui_smoke:
                root = tk.Tk()
                root.withdraw()
                window = A05Window(root, args.config)
                root.update_idletasks()
                result['ui_created'] = True
                result['runtime_in_app_temp'] = (Path(sys._MEIPASS).resolve().is_relative_to(
                    (Path(os.environ['LOCALAPPDATA'])/'A05Editable/temporal').resolve())
                    if getattr(sys, 'frozen', False) else None)
                if getattr(sys, 'frozen', False):
                    result['runtime_directory'] = str(Path(sys._MEIPASS).resolve())
                    result['expected_runtime_root'] = str((Path(os.environ['LOCALAPPDATA'])/'A05Editable/temporal').resolve())
                    # Windows can redirect an unpackaged child of a packaged
                    # host into that package's private LocalCache directory.
                    runtime = Path(sys._MEIPASS).resolve()
                    virtual = (runtime.is_relative_to((Path(os.environ['LOCALAPPDATA'])/'Packages').resolve())
                               and 'LocalCache' in runtime.parts and runtime.parent.name == 'temporal'
                               and runtime.parent.parent.name == 'A05Editable')
                    result['runtime_redirected_by_windows'] = virtual
                    result['runtime_in_app_temp'] |= virtual
                result['run_button_initially_enabled'] = str(window.run_button.cget('state')) == 'normal'
                result['query_button_initially_enabled'] = str(window.query_button.cget('state')) == 'normal'
                result['profile_ready'] = window.profile_ready
                result['frp_button_identified'] = window.run_button.cget('text') == 'Ejecutar FRP del A05'
                root.after_cancel(window.poll_id)
                window.events.put(('stage', 'MediaTek sigue conectado. Desconecta el cable del A05; el motor está detenido.\n'))
                window.poll()
                result['usb_disconnect_stage_visible'] = 'Desconecta el cable' in window.status.get()
                window.events.put(('finished', {'kind': 'perfil', 'status': 'reconnect_required', 'returncode': 0}))
                root.after_cancel(window.poll_id)
                window.poll()
                result['reconnect_exit_zero_shown_as_incomplete'] = (
                    'Inicialización incompleta.' in window.status.get() and 'Código del proceso: 0.' in window.status.get())
                result['buttons_match_available_actions'] = (
                    result['run_button_initially_enabled'] == window.profile_ready and
                    result['query_button_initially_enabled'] == window.query_ready)
                result['stop_button_initially_disabled'] = str(window.stop_button.cget('state')) == 'disabled'
                assert (result['buttons_match_available_actions'] and result['stop_button_initially_disabled']
                        and result['frp_button_identified'] and result['reconnect_exit_zero_shown_as_incomplete']
                        and result['usb_disconnect_stage_visible'])
                window.close()
            result['all_passed'] = True
        except Exception as error:
            result = {'all_passed': False, 'error_type': type(error).__name__, 'usb_access': False}
        save_json(args.report or args.config.parent/'verificacion_local.json', result)
        return 0 if result['all_passed'] else 2
    root = tk.Tk()
    try:
        A05Window(root, args.config)
    except Exception as error:
        from tkinter import messagebox
        messagebox.showerror('A05 Editable', 'No se pudo abrir la configuración: '+str(error))
        root.destroy()
        return 2
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
