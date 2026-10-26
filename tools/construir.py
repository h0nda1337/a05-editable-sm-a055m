"""Build the two local tools and engine copy; never start a phone operation."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT/'fuente'
sys.path.insert(0, str(SOURCE))
from a05_core import VERSION, save_json
from reconstruir_motor_traza import EXPECTED_ORIGINAL


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.name != 'nt' or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Construcción verificada para Windows y Python 3.12.')
    original = ROOT/'motor/mtk.exe'
    if not original.is_file() or digest(original) != EXPECTED_ORIGINAL:
        raise RuntimeError('Primero importa el motor mediante tools/preparar_componentes.py.')
    for name in ('libusb-1.0.dll', 'libusb-1.0_nested.dll'):
        if not (SOURCE/'diag_assets'/name).is_file():
            raise RuntimeError('Falta '+name+'; vuelve a preparar los componentes.')
    runtime = Path(os.environ['LOCALAPPDATA'])/'A05Editable/temporal'
    runtime.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TEMP=str(runtime), TMP=str(runtime),
                       PYINSTALLER_CONFIG_DIR=str(ROOT/'build/pyinstaller-cache'))

    def run(args):
        subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, env=environment, check=True)

    run(['-B', SOURCE/'reconstruir_motor_traza.py', '--original', original,
         '--output', ROOT/'motor/mtk_traza.exe', '--config', ROOT/'configuracion.json',
         '--report', ROOT/'build/validacion_motor_traza.json'])
    common = ['-m', 'PyInstaller', '--noconfirm', '--onefile',
              '--runtime-tmpdir', '%LOCALAPPDATA%/A05Editable/temporal',
              '--workpath', ROOT/'build/pyinstaller', '--specpath', ROOT/'build/spec']
    run([*common, '--console', '--name', 'A05_Diagnostico', '--distpath', ROOT/'motor',
         '--collect-submodules', 'usb',
         '--add-binary', str(SOURCE/'diag_assets/libusb-1.0.dll')+';.',
         '--add-binary', str(SOURCE/'diag_assets/libusb-1.0_nested.dll')+';.', SOURCE/'diagnostico_usb.py'])
    settings = json.loads((ROOT/'configuracion.json').read_text(encoding='utf-8'))
    settings['diagnostic_sha256'] = digest(ROOT/'motor/A05_Diagnostico.exe')
    save_json(ROOT/'configuracion.json', settings)
    run([*common, '--windowed', '--name', 'A05_Editable_'+VERSION.replace('.', '_'),
         '--paths', SOURCE, '--distpath', ROOT, SOURCE/'a05_app.py'])
    print('Construcción terminada. No se inició ninguna operación USB.')


if __name__ == '__main__':
    main()
