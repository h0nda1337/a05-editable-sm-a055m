"""Read Windows' present device inventory without opening or claiming a USB device."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
import re
import time

CM_GETIDLIST_FILTER_PRESENT = 0x00000100
CR_BUFFER_SMALL = 0x0000001A
MTK_MODES = {'0003', '2000', '2001'}


class UsbInventoryError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def configuration_manager():
    if os.name != 'nt':
        raise UsbInventoryError('La comprobación de desconexión requiere Windows.')
    from ctypes import wintypes
    api = ctypes.WinDLL('cfgmgr32.dll', winmode=0x00000800)
    api.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(wintypes.ULONG), wintypes.LPCWSTR, wintypes.ULONG]
    api.CM_Get_Device_ID_List_SizeW.restype = wintypes.DWORD
    api.CM_Get_Device_ID_ListW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
    api.CM_Get_Device_ID_ListW.restype = wintypes.DWORD
    return api


def present_mediatek_usb():
    """Return VID/PID modes only; never retain device instance IDs or serials."""
    from ctypes import wintypes
    api = configuration_manager()
    for _ in range(4):
        length = wintypes.ULONG()
        result = api.CM_Get_Device_ID_List_SizeW(ctypes.byref(length), None, CM_GETIDLIST_FILTER_PRESENT)
        if result:
            raise UsbInventoryError('No se pudo consultar el inventario USB de Windows: '+hex(result))
        if not 2 <= length.value <= 4_000_000:
            raise UsbInventoryError('Windows devolvió un tamaño de inventario no válido.')
        buffer = ctypes.create_unicode_buffer(length.value)
        result = api.CM_Get_Device_ID_ListW(None, buffer, length.value, CM_GETIDLIST_FILTER_PRESENT)
        if result == CR_BUFFER_SMALL:
            continue
        if result:
            raise UsbInventoryError('No se pudo leer el inventario USB de Windows: '+hex(result))
        modes = set()
        for instance in buffer[:].split('\0'):
            match = re.match(r'USB\\VID_0E8D&PID_([0-9A-F]{4})(?:&|\\)', instance, re.IGNORECASE)
            if match and match.group(1).lower() in MTK_MODES:
                modes.add('0e8d:'+match.group(1).lower())
        return tuple(sorted(modes))
    raise UsbInventoryError('El inventario USB cambió repetidamente; vuelve a intentar con el cable desconectado.')


def wait_for_usb_absence(probe, cancel_event, emit, timeout_seconds, stable_seconds=1.5, poll_seconds=0.2):
    """Require stable absence before the external motor is allowed to start."""
    started = time.monotonic()
    absent_since = None
    previous_present = None
    last_notice = started
    report = {'status': 'waiting', 'samples': 0, 'present_seen': False, 'usb_device_opened': False}
    emit('stage', 'Comprobando desconexión USB. El motor todavía no se ha iniciado.\n')
    while True:
        if cancel_event.is_set():
            report['status'] = 'cancelled'
            return report
        if time.monotonic()-started >= timeout_seconds:
            report['status'] = 'timeout'
            return report
        try:
            present = bool(probe())
        except Exception as error:
            report.update(status='inventory_error', error_type=type(error).__name__)
            return report
        report['samples'] += 1
        now = time.monotonic()
        if present:
            report['present_seen'] = True
            absent_since = None
            if previous_present is not True:
                emit('stage', 'MediaTek sigue conectado. Desconecta el cable del A05; el motor está detenido.\n')
        else:
            if absent_since is None:
                absent_since = now
                emit('stage', 'USB MediaTek ausente. Mantén el cable desconectado mientras se prepara el motor.\n')
            if now-absent_since >= stable_seconds:
                report.update(status='absent', stable_absence_seconds=round(now-absent_since, 3))
                return report
        previous_present = present
        if now-last_notice >= 5:
            last_notice = now
            emit('stage', f'Esperando desconexión USB: {int(now-started)}s transcurridos; el motor todavía no se ha iniciado.\n')
        cancel_event.wait(poll_seconds)
