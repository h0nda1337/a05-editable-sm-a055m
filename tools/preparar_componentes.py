"""Import a hash-verified local engine and extract its required DLLs. No USB."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'fuente'))
from reconstruir_motor_traza import EXPECTED_ORIGINAL, read_archive

DLLS = {
    'libusb-1.0.dll': ('libusb-1.0.dll', '3f050dee9fbf995bfbed2f4969131877fcd1b59bfd3afd2a767ff6844e872430'),
    'mtkclient\\Windows\\libusb-1.0.dll': ('libusb-1.0_nested.dll', '6b24b0ee1a59cbae385dd15b06eddf2c72b2ff3a875ae279883a880136c59ec8'),
}


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.new')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def prepare(original_path):
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Usa Python 3.12 para esta copia del motor.')
    original = Path(original_path).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != EXPECTED_ORIGINAL:
        raise ValueError('Huella del motor no reconocida. No se importó ningún componente.')
    _, entries, _ = read_archive(original)
    files = []
    # Verify every required component before writing anything.
    for archive_name, (name, expected) in DLLS.items():
        entry = next(item for item in entries if item['name'] == archive_name)
        data = zlib.decompress(entry['payload']) if entry['compressed'] else entry['payload']
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('La DLL '+name+' no coincide con la copia verificada.')
        files.append((name, data, expected))
    target = ROOT/'motor/mtk.exe'
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != EXPECTED_ORIGINAL:
        raise FileExistsError('Ya existe un motor distinto en motor/mtk.exe; conserva esa copia antes de continuar.')
    if Path(original_path).resolve() != target.resolve():
        atomic_write(target, original)
    for name, data, expected in files:
        atomic_write(ROOT/'fuente/diag_assets'/name, data)
    report = {'original_sha256': digest,
              'dlls': [{'name': name, 'sha256': expected} for name, _, expected in files],
              'usb_access': False, 'operation_executed': False}
    (ROOT/'componentes_locales.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--motor-original', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.motor_original), indent=2))
