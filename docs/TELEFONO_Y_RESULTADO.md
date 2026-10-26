# Teléfono y resultado del caso publicado

Fecha de confirmación del propietario: **25 de octubre de 2026, Ciudad de México**. La sesión exitosa fue registrada entre 18:26 y 18:27 locales. El resultado final lo confirmó el propietario después de la ejecución, no un detector automático de FRP.

## Datos y estado anteriores

| Dato | Valor | Procedencia |
| --- | --- | --- |
| Fabricante / modelo | Samsung Galaxy A05 / SM-A055M | Informado por el propietario |
| CSC | TCE, Telcel México | Ficha aportada |
| Binario | G / 16 (B16) | Ficha aportada |
| FRP | Activo antes del procedimiento | Ficha aportada |
| OEM Lock | ON (L) | Ficha aportada |
| KG State | PRENORMAL (00) | Ficha aportada |
| MTK Secure Boot | ENABLE (0) | Ficha aportada |
| Secure Download | ENABLE | Ficha aportada |
| Knox Warranty Void | 0x0 | Ficha aportada; no hay una medición posterior publicada |
| Download Mode | Funcional | Ficha aportada |
| Android, compilación completa y parche | No registrados | No se deducen a partir del binario |

## Observaciones del protocolo

- Chip: `0x0707`; el motor lo identifica como MT6768/MT6769.
- HW subcode: `0x8A00`; HW version posterior: `0xCA00`; SW version: `0x0000`.
- Modo inicial confirmado: **Preloader**, respuesta `FE = 03`, VID/PID `0e8d:2000`.
- Configuración inicial consultada: `0x00000005`, estado `0x0000`; SBC y DAA activos, SLA desactivado.
- Durante el flujo original se observó una transición a BROM y configuración `0xE5`, seguida por carga DA.
- Memoria eMMC: tamaño de usuario reportado `0xE8F800000`, aproximadamente 62,54 GB decimales / 58,24 GiB. Es el valor reportado por el motor, no una afirmación de capacidad comercial.
- Conexión de prueba: USB 2.0, Windows 11 x64. La consulta reportó `Administrador: False`; no se deduce de ello cómo se instalaron los controladores.

## Resultado observado

La versión 1.0.8 completó tres sincronizaciones sin fallo registrado, cargó ambas etapas DA, accedió a eMMC y mostró un borrado al 100 %, mensaje de formateo satisfactorio y salida **0**. El propietario confirmó posteriormente que funcionó.

La sesión incluyó errores intermedios de control USB, timeout y mensajes UsbDk. Después de algunos de esos avisos el motor continuó; su sola presencia no describe el resultado final. No se observó el cierre nativo anterior en esa sesión exitosa.

## Límites

Es un resultado en un equipo concreto. No demuestra compatibilidad con todos los firmwares G, con el SM-A055F, con otros CSC o con versiones actuales del motor upstream. Los cambios no incluyen una implementación nueva de los controladores de seguridad del motor.

Se omiten ME_ID, SOC_ID, CID/serial eMMC, IMEI, rutas personales, capturas y registros originales. Son identificadores del ejemplar o datos personales y no hacen falta para documentar el modelo y el procedimiento.
