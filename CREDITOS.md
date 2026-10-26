# Créditos y procedencia

## Trabajo de este proyecto

**h0nda1337**: equipo de prueba, investigación del caso, decisiones del proyecto y confirmación física del resultado. Desarrollo del controlador, diagnóstico, adaptadores, pruebas y documentación con asistencia de Codex.

El código propio publicado comprende la ventana Tkinter, carga y validación de perfil, gestión de procesos y registros, diagnóstico limitado, constructor local y las correcciones de inicialización y handshake. La referencia funcional se obtuvo de una instalación local; esto no es una recuperación completa ni exacta de todo el código fuente del proveedor.

## MTKClient

- Proyecto: [bkerler/mtkclient](https://github.com/bkerler/mtkclient).
- Autor principal identificado en el banner usado: **B. Kerler**; crédito también a todos los colaboradores de MTKClient.
- Licencia del proyecto upstream: [GNU GPL v3](https://github.com/bkerler/mtkclient/blob/main/LICENSE); copia en `licenses/MTKClient-GPL-3.0.txt`.
- El componente usado en el caso anuncia `MTK Flash/Exploit Client Public V2.0.1`, con copyright B. Kerler 2018–2024. Su huella exacta se documenta en README; ese banner no establece qué commit upstream lo produjo.
- De ese componente se estudiaron `DaHandler.configure_da`, `Port.handshake`, `Port.run_handshake`, `Preloader.init` y la capa USB. Las correcciones se aplican mediante un envoltorio al punto de entrada de una copia local.
- Los protocolos completos, loaders, payloads, Kamakiri, acceso DA/eMMC y operación final pertenecen al motor original. No se reclaman como obra nueva ni se redistribuyen en este repositorio.

El [README upstream](https://github.com/bkerler/mtkclient/blob/main/README.md) reconoce, entre otros, **xyzz** por Kamakiri, **Chaosmaster**, **Geert-Jan Kreileman** por GUI/diseño/correcciones y otros colaboradores y autores de técnicas. La GUI Tkinter de este proyecto es propia; no es la GUI upstream. Para la lista actual y las atribuciones de cada componente, consultar el proyecto original.

## PrimeToolX

Se analizaron las instalaciones locales **26.7.2** y **27.0.1**, el registro del modelo SM-A055M y el flujo `VentanaOperacionesMTK.ejecutar_mtk` de `mobs.engine_mediatek` para identificar cómo lanzaban el motor. La interfaz nueva y el controlador se escribieron como fuentes editables; el catálogo completo y los módulos de PrimeToolX no se incluyen.

Se acredita **PrimeToolX** como aplicación de referencia. No se identificó un nombre de autor verificable ni una licencia que permita redistribuir sus componentes propios en los archivos revisados; no se atribuye una autoría o licencia inventada. No hay afiliación ni respaldo oficial de PrimeToolX, Samsung, Google o MediaTek.

## Bibliotecas y herramientas

| Componente | Autores/proyecto | Papel | Licencia / fuente |
| --- | --- | --- | --- |
| PyUSB 1.3.1 | Wander Lairson Costa y colaboradores de PyUSB | API USB del diagnóstico | [BSD-3-Clause](https://github.com/pyusb/pyusb/blob/v1.3.1/LICENSE) |
| libusb | Colaboradores de libusb, incluidos Daniel Drake, Johannes Erdfelt y Nathan Hjelm | Backend USB; se verificó libusb 1.0.27.11882 | [LGPL-2.1-or-later](https://github.com/libusb/libusb/blob/v1.0.27/libusb/core.c); texto en `licenses/libusb-LGPL-2.1.txt` |
| UsbDk | Daynix y colaboradores | Controlador/backend de redirección USB en Windows | [Apache-2.0](https://github.com/daynix/UsbDk/blob/master/LICENSE) |
| Python 3.12 | Python Software Foundation y colaboradores | Runtime y biblioteca estándar | [PSF y licencias históricas incluidas](https://github.com/python/cpython/blob/v3.12.10/LICENSE) |
| Tcl/Tk | Autores de Tcl/Tk | GUI a través de Tkinter | [Tcl](https://github.com/tcltk/tcl/blob/core-8-6-13/license.terms), [Tk](https://github.com/tcltk/tk/blob/core-8-6-13/license.terms) |
| PyInstaller 6.22.3 | PyInstaller Development Team y autores anteriores reconocidos en COPYING | Construcción de ejecutables y formato del archivo analizado | [GPL con excepción de bootloader y condiciones adicionales](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/COPYING.txt) |

Las versiones anteriores son las utilizadas o referenciadas en este caso. No se afirma haber reconstruido la lista completa de dependencias internas del ejecutable del proveedor. Cada componente externo conserva sus condiciones; la GPL de nuestras fuentes no lo relicencia.

## Forma de distribución

Se publican fuentes, configuración, perfil y pruebas. `tools/preparar_componentes.py` importa una copia local del motor cuya huella se conoce y extrae las DLL requeridas; no descarga ejecutables de sitios no verificados. Los binarios generados y los componentes importados quedan excluidos de Git.

Las copias de licencias se incluyen para preservar la atribución y facilitar su consulta. `licenses/SOURCES.json` enlaza los textos originales. Ninguno de los autores upstream se presenta como aval de esta herramienta.
