"""Rebuild the entry wrapper with tracing and a narrow initialization adapter.

Original archived modules remain byte-identical; the entry wrapper applies the
documented runtime configure_da adapter before the original main is called.
"""
from pathlib import Path
import argparse
import hashlib
import json
import marshal
import os
import struct
import sys
import types
import zlib

MAGIC = b'MEI\014\013\012\013\016'
EXPECTED_ORIGINAL = '65f5fb3dd7880e25a6db1dec0d55a120da666ccbeceed2bac6bcdfd22724d635'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_archive(data):
    cookie = data.rfind(MAGIC)
    magic, total, offset, size, version, dll = struct.unpack_from('!8sIIII64s', data, cookie)
    assert magic == MAGIC and cookie+88 == len(data) and version == 312
    start = cookie+88-total
    entries = []
    cursor, end = start+offset, start+offset+size
    while cursor < end:
        length, position, stored, raw, compressed, kind = struct.unpack_from('!iIIIBc', data, cursor)
        assert length >= 18 and cursor+length <= end
        name_data = data[cursor+18:cursor+length]
        name = name_data.rstrip(b'\0').decode('utf-8')
        payload = data[start+position:start+position+stored]
        entries.append({'name': name, 'name_data': name_data, 'kind': kind, 'compressed': compressed,
                        'raw_size': raw, 'payload': payload})
        cursor += length
    assert cursor == end
    return data[:start], entries, dll


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    assert sys.version_info[:2] == (3, 12), 'Usa Python 3.12 para reconstruir este motor.'
    assert args.original.resolve() != args.output.resolve(), 'La copia original debe conservarse.'
    original = args.original.read_bytes()
    assert sha(original) == EXPECTED_ORIGINAL, 'El motor original no corresponde a esta reconstrucción.'
    prefix, entries, dll = read_archive(original)
    entry = next(item for item in entries if item['name'] == 'mtk' and item['kind'] == b's')
    old_script = zlib.decompress(entry['payload']) if entry['compressed'] else entry['payload']
    root = marshal.loads(old_script)
    main_code = next(item for item in root.co_consts if isinstance(item, types.CodeType) and item.co_name == 'main')
    assert main_code.co_argcount == 0 and not main_code.co_freevars
    trace_path = Path(__file__).with_name('a05_motor_trace.py')
    trace_code = compile(trace_path.read_text(encoding='utf-8'), 'a05_motor_trace.py', 'exec', dont_inherit=True)
    wrapper = ('def main():\n'
               '    import marshal, types, os, sys\n'
               '    scope = {"__name__": "a05_motor_trace"}\n'
               f'    exec(marshal.loads({marshal.dumps(trace_code)!r}), scope)\n'
               '    scope["install"]()\n'
               '    if "--a05-engine-selftest" in sys.argv or "--a05-engine-echo-selftest" in sys.argv:\n'
               '        scope["self_test"](terminal_echo="--a05-engine-echo-selftest" in sys.argv)\n'
               '        return\n'
               f'    original_main = types.FunctionType(marshal.loads({marshal.dumps(main_code)!r}), globals(), "main")\n'
               '    return original_main()\n')
    replacement = next(item for item in compile(wrapper, 'a05_engine_entry.py', 'exec').co_consts
                       if isinstance(item, types.CodeType) and item.co_name == 'main')
    updated = root.replace(co_consts=tuple(replacement if item is main_code else item for item in root.co_consts))
    new_script = marshal.dumps(updated)
    payloads, table = bytearray(), bytearray()
    for item in entries:
        value = item['payload']
        raw_size = item['raw_size']
        if item is entry:
            value = zlib.compress(new_script, 9) if item['compressed'] else new_script
            raw_size = len(new_script)
        position = len(payloads)
        payloads.extend(value)
        table.extend(struct.pack('!iIIIBc', 18+len(item['name_data']), position, len(value), raw_size,
                                 item['compressed'], item['kind'])+item['name_data'])
    cookie = struct.pack('!8sIIII64s', MAGIC, len(payloads)+len(table)+88, len(payloads), len(table), 312, dll)
    rebuilt = prefix+payloads+table+cookie
    new_prefix, new_entries, _ = read_archive(rebuilt)
    assert new_prefix == prefix and len(new_entries) == len(entries)
    for before, after in zip(entries, new_entries):
        assert before['name'] == after['name'] and before['kind'] == after['kind']
        if before['name'] != 'mtk':
            assert before == after
    restored = updated.replace(co_consts=tuple(main_code if item is replacement else item for item in updated.co_consts))
    assert restored == root
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name+'.new')
    temporary.write_bytes(rebuilt)
    os.replace(temporary, args.output)
    report = {'original_sha256': sha(original), 'instrumented_sha256': sha(rebuilt),
              'trace_source_sha256': sha(trace_path.read_bytes()), 'changed_archive_entry': 'mtk',
              'only_main_wrapper_replaced': True, 'all_protocol_and_usb_modules_identical': True,
              'runtime_initialization_modified': True,
              'initialization_fix': 'present_usb_missing_target_config_without_da_state',
              'runtime_handshake_replaced': True,
              'handshake_fix': 'finite_transfers_echo_stop_16_exchanges_10_second_budget',
              'usb_library_pinned_to_diagnostic_copy': True,
              'original_security_and_operation_handlers_retained': True,
              'original_preserved': args.original.read_bytes() == original, 'usb_access': False,
              'phone_operation_executed_by_build': False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.config:
        settings = json.loads(args.config.read_text(encoding='utf-8-sig'))
        settings.update(backend=args.output.resolve().relative_to(args.config.resolve().parent).as_posix(),
                        backend_sha256=sha(rebuilt), debug_engine=False, engine_trace=True)
        args.config.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
