"""Run supplied simulated suites in isolated temporary directories, without USB."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sin-motor', action='store_true', help='Solo las 40 pruebas que no necesitan el motor local.')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Las pruebas del bytecode original requieren Python 3.12.')
    temporary = ROOT/'build/tests'
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary))
    suites = [(['pruebas_controlador.py', '--work-dir', str(temporary)], 25),
              (['pruebas_traza.py', '--work-dir', str(temporary)], 12),
              (['pruebas_consola.py'], 3)]
    if not args.sin_motor:
        if not (ROOT/'motor/mtk.exe').is_file():
            raise RuntimeError('Importa el motor original o usa --sin-motor para las 40 pruebas independientes.')
        suites += [(['pruebas_inicializacion.py', '--work-dir', str(temporary)], 12),
                   (['pruebas_sincronizacion.py'], 15)]
    for arguments, count in suites:
        subprocess.run([sys.executable, '-B', str(ROOT/'fuente'/arguments[0]), *arguments[1:]],
                       cwd=ROOT, env=environment, check=True)
    report = {'passed': True, 'simulated_test_count': sum(count for _, count in suites),
              'usb_access': False, 'phone_operation_executed': False}
    (ROOT/'validacion_local.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
