# Cambios y límites del motor

## Fallo original

La primera captura terminaba en `DeviceClass.rword` con `struct.error: unpack requires a buffer of 2 bytes`, durante la inicialización del watchdog. La lectura subyacente había devuelto una longitud insuficiente. El error no establecía por sí mismo daño de hardware. El caso verificado mantuvo `--skipwdt 1` para omitir ese paso.

## Inicialización de una conexión ya presente

Se reprodujo con objetos simulados que `DaHandler.configure_da` podía abrir USB, omitir `Preloader.init` y regresar por falta de `target_config` cuando no había estado DA guardado. El adaptador inicia el Preloader original una vez en ese caso y restaura el método de conexión antes de hacerlo. Conserva las rutas de puerto serie, estado DA y configuración existente.

## Sincronización limitada

La sincronización original podía repetir A0 indefinidamente ante un eco. Una captura conservó 268.556 respuestas A0, más de un millón de eventos y aproximadamente 224 MB de traza antes de una infracción de acceso. La captura no demuestra la causa interna de esa infracción nativa.

La versión 1.0.8 usa la secuencia del diagnóstico: activación de Preloader y cuatro intercambios con complementos esperados. Cada transferencia tiene timeout de hasta 1.500 ms, un presupuesto total comprobado entre transferencias de 10 segundos y un máximo de 16 intercambios. Eco, paquete vacío, escritura incompleta y error USB terminan con un código concreto. No se acepta el eco como una sincronización válida.

La espera del teléfono está acotada por la configuración USB de la ventana. Un CDC ya abierto también pasa por el handshake. La carga de libusb se fija por ruta y SHA-256 a la misma DLL del diagnóstico y registra versión/ruta; no se atribuye por ello todo fallo anterior a una DLL equivocada.

## Registro y ventana

Se conservan stdout y stderr completos en disco. La consola procesa lotes limitados, limita su cola y texto visible, y conserva eventos de estado y resultado. La traza usa escritura agrupada cada 250 ms y vacía errores inmediatamente. El modo debug general del cliente queda desactivado para evitar inundar la salida; se mantienen los observadores y los avisos cada cinco segundos.

Cada proceso usa temporales propios bajo `%LOCALAPPDATA%/A05Editable/temporal`. En la PC de prueba la ruta temporal global anterior tenía poco espacio. La herramienta no cambia las variables globales ni elimina otros archivos.

## Integridad y procedencia

`reconstruir_motor_traza.py` exige la huella del motor original, conserva su copia y modifica una sola entrada de su archivo PyInstaller: la que envuelve la función principal. Los demás módulos archivados se comprueban byte a byte. Ese hecho no significa que no haya cambios funcionales: el envoltorio instala en ejecución el adaptador de inicialización, el handshake nuevo y los observadores.

Los handlers originales de seguridad, DA, memoria y operación se conservan. Este repositorio no reconstruye todo el motor ni distribuye el ejecutable, loaders o payloads originales.
