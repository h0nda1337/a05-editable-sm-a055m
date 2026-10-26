# A05 Editable · SM-A055M

Herramienta para Windows con interfaz, diagnóstico USB y código Python editable para el **Samsung Galaxy A05 SM-A055M**. Incluye las correcciones de inicialización, sincronización y registro que permitieron completar el flujo FRP en un teléfono con **binario G / revisión 16**.

**Versión verificada: 1.0.8.** El propietario confirmó el resultado en el teléfono el **25 de octubre de 2026**, después de que el motor reportara la operación completada. Es un caso confirmado; no se ha comprobado compatibilidad con otros modelos, revisiones o versiones de firmware.

El perfil realiza un borrado. Úsalo únicamente en equipos propios o con autorización y con los datos importantes respaldados.

## Qué incluye esta publicación

- Código de la ventana, controlador, diagnóstico separado y parches del motor.
- Perfil del SM-A055M utilizado en la prueba y configuración inicial.
- Preparador de componentes, constructor local y 67 pruebas simuladas.
- [Datos del teléfono y evidencia resumida](docs/TELEFONO_Y_RESULTADO.md), [explicación de los cambios](docs/CAMBIOS_DEL_MOTOR.md) y [créditos completos](CREDITOS.md).

Esta publicación distribuye fuentes. Para reproducir el motor exacto del caso verificado debes aportar tu copia local de `mtk.exe` y ejecutar el constructor. No hay un EXE listo para usar en el ZIP de fuentes de GitHub. El preparador comprueba la huella del motor y extrae localmente sus dos DLL USB; el repositorio no contiene el ejecutable del proveedor, loaders, payloads, volcados del teléfono ni registros originales.

## Teléfono probado

| Dato | Valor del caso verificado |
| --- | --- |
| Modelo | Samsung Galaxy A05 **SM-A055M** |
| CSC | **TCE**, Telcel México, informado por el propietario |
| Binario | **G / 16 (B16)**, informado por el propietario |
| Chip identificado | `0x0707`, MT6768/MT6769 según el motor |
| Modo inicial observado | Preloader; USB `0e8d:2000`, respuesta `FE = 03` |
| Sistema de la PC | Windows 11, 64 bits |
| Conexión utilizada | Puerto USB 2.0 |
| Android / compilación / parche | No registrados; no se inventa una equivalencia |

No confundir el SM-A055M con el SM-A055F: este último no se probó. El estado completo anterior y los límites de la evidencia están en [la ficha técnica](docs/TELEFONO_Y_RESULTADO.md).

## Preparar el programa

### Requisitos

1. Windows de 64 bits y **Python 3.12** con Tkinter. El desarrollo se verificó con Python 3.12.10.
2. Controlador MediaTek y **UsbDk** instalados. Consulta las [instrucciones oficiales de MTKClient para Windows](https://github.com/bkerler/mtkclient/blob/main/README-WINDOWS.md) y [UsbDk](https://github.com/daynix/UsbDk). El programa no instala ni cambia controladores.
3. Una copia local del motor exacto procedente de la instalación analizada de PrimeToolX 26.7.2, en `_internal\mobs\mtk.exe`. La instalación 27.0.1 del caso utilizó ese mismo componente restaurado desde 26.7.2.

Huella SHA-256 requerida del motor original:

```text
65f5fb3dd7880e25a6db1dec0d55a120da666ccbeceed2bac6bcdfd22724d635
```

El nombre del archivo o el banner «Public V2.0.1» no bastan para identificarlo. El preparador rechaza una huella diferente; no se ha validado sustituirlo por la versión actual de MTKClient.

### Construcción local

Descarga y extrae las fuentes, o clona este repositorio:

```powershell
git clone https://github.com/h0nda1337/a05-editable-sm-a055m.git
cd a05-editable-sm-a055m
.\RECONSTRUIR.cmd "C:\ruta\PrimeToolX26.7.2\_internal\mobs\mtk.exe"
```

El archivo CMD crea `.venv`, instala las dependencias fijadas, importa el motor verificado, construye `motor/mtk_traza.exe`, compila el diagnóstico y la ventana, y actualiza las huellas de `configuracion.json`. No abre USB ni inicia la función del teléfono durante la construcción.

Al terminar, abre **`A05_Editable_1_0_8.exe`** en la raíz del proyecto. Conserva `configuracion.json` y las carpetas `motor`, `perfiles` y `fuente` junto al ejecutable. Para reconstruir después de editar código, ejecuta `RECONSTRUIR.cmd` sin argumentos.

También puedes ejecutar la ventana desde las fuentes una vez preparados los componentes:

```powershell
.\.venv\Scripts\python.exe -B fuente\a05_app.py
```

El motor original y los componentes generados quedan ignorados por Git. La GPL de este proyecto no cambia las condiciones de los componentes que cada usuario aporte por separado; consulta [CREDITOS.md](CREDITOS.md).

## Procedimiento de conexión y ejecución

1. **Cierra PrimeToolX y las demás herramientas que accedan al teléfono.** Deja abierto únicamente A05 Editable.
2. **Desconecta el cable.** Si acabas de usar «Consultar estado USB» o el A05 quedó en Preloader/BROM, utiliza el reinicio forzado que ya tengas comprobado para ese teléfono. Deja que llegue a Android y apágalo normalmente desde Android. Mantén el cable desconectado.
3. Abre la versión **1.0.8**. El estado inicial debe indicar que la consulta y la función del A05 están disponibles.
4. Mantén activada **«Omitir inicialización del watchdog (skipwdt)»**. El caso verificado utilizó `--skipwdt 1` y una espera de conexión de **120 segundos**.
5. Pulsa directamente **«Ejecutar FRP del A05»**, sin realizar antes otra consulta USB en esa misma sesión del teléfono.
6. Espera el aviso **«El motor está esperando conexión. Conecta ahora el A05 apagado una sola vez»**. Entonces conecta el teléfono apagado al puerto USB 2.0. La captura acepta Preloader; no es necesario conseguir BROM manualmente para este flujo. MTKClient indica que Preloader se presenta al conectar apagado sin pulsar botones; consulta su documentación oficial si tu equipo no aparece. No se registró una combinación física exacta de botones para el caso publicado.
7. **Mantén el cable conectado durante sincronización, inicialización, carga del DA y operación.** Hay avisos de fase y tiempo cada cinco segundos. Los 120 segundos corresponden a la espera USB; no son un plazo máximo de la operación ni una estimación de finalización.
8. Espera el resultado final. En la sesión verificada el motor confirmó acceso a eMMC, completó el borrado al **100 %**, informó éxito y terminó con **código 0**. La interfaz conserva el texto «resultado de FRP no confirmado» porque no inspecciona por sí sola el estado del teléfono después de arrancar.
9. Reinicia el A05 y comprueba el resultado durante la configuración con conexión a Internet. **Esta verificación la hizo el propietario y confirmó que funcionó.**

La consulta USB es una herramienta de diagnóstico separada. Una desaparición del dispositivo del inventario de Windows confirma ausencia USB, pero no demuestra que el protocolo del teléfono haya reiniciado. Hubo consultas exitosas seguidas de eco A0 en intentos posteriores; no se atribuye cada eco exclusivamente a ese estado sin una comparación adicional.

## Qué significan los avisos

| Mensaje | Interpretación y acción |
| --- | --- |
| `SYNC_ECHO` | Llegó el mismo byte enviado, en vez del complemento esperado. La versión 1.0.8 detiene la sesión con código 2. Reinicia el teléfono como se describe arriba y empieza directamente la operación; no reconectes repetidamente durante un intento activo. |
| `Configuration not set` | En los intentos observados el cliente seleccionó la configuración USB y continuó. La línea por sí sola no identifica el error final. |
| `Auth file is required` / errores intermedios USB | El caso verificado también mostró mensajes intermedios antes de continuar. Revisa las etapas posteriores y el resultado final; no se garantiza que toda aparición sea recuperable. |
| `0xC0000005` | Cierre nativo. Existió antes de limitar el bucle; no se volvió a observar en el intento exitoso. No prueba daño de hardware ni determina por sí solo la causa interna. |
| `FRP no confirmado`, código 0 | El proceso terminó. La verificación final se realiza en el teléfono, no mediante ese código de salida. |
| Error de huella o componente faltante | Revisa la copia original y vuelve a ejecutar el constructor. No desactives la comprobación de integridad. |

Los registros locales se abren con **«Abrir registros locales»** y se guardan bajo `%LOCALAPPDATA%\A05Editable\registros`. Pueden contener identificadores únicos del dispositivo. Revisa y anonimiza su contenido antes de adjuntarlos a una incidencia pública.

## Pruebas y desarrollo

```powershell
# 67 pruebas locales, tras importar el motor original:
.\.venv\Scripts\python.exe -B tools\comprobar.py

# 40 pruebas independientes del motor, usadas también en CI:
py -3.12 -B tools\comprobar.py --sin-motor

# 28 pruebas adicionales del diagnóstico, sin cargar libusb ni abrir USB:
.\.venv\Scripts\python.exe -B fuente\diagnostico_usb.py --self-test
```

Las pruebas simuladas no ejecutan operaciones sobre un teléfono. La CI pública no dispone del motor local y no sustituye la validación física de un modelo. Los cambios públicos de empaquetado y documentación no añaden compatibilidad con otros dispositivos.

## Créditos y licencia

Proyecto y prueba física: **[h0nda1337](https://github.com/h0nda1337)**, con asistencia de Codex para el diagnóstico, programación y documentación.

El motor utilizado es **[MTKClient de B. Kerler y sus colaboradores](https://github.com/bkerler/mtkclient)**. Las transiciones, exploits, payloads, controladores de seguridad y operación que aparecen en el registro pertenecen al motor original; no son una implementación nueva de este proyecto. PrimeToolX 26.7.2/27.0.1 fue la instalación local de referencia para el flujo y el perfil.

Código publicado bajo **GPL-3.0-only**. Consulta [LICENSE](LICENSE), [CREDITOS.md](CREDITOS.md), [NOTICE](NOTICE) y [licenses/](licenses/) para atribución y licencias de dependencias.
