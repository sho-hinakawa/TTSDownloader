# Tabletop Simulator — Asset Downloader GUI

Herramienta gráfica (Python + Tkinter) para **descargar assets** de una partida o módulo de Tabletop Simulator:

- Cartas (recortadas desde las hojas)
- Tokens y figurines
- Modelos 3D (`.obj` + texturas)
- Conversión automática OBJ → STL
- PDFs
- Imágenes / tiles / tableros

Funciona en dos modos:

1. **JSON local** — a partir de un archivo `.json` de save / módulo de TTS  
2. **Steam Workshop** — pegando la URL del ítem de Workshop

Interfaz bilingüe (**Español / English**), con detección automática del idioma del sistema.

---

## Características

- Identifica librerías faltantes al arrancar.
- Creación de directorios según el nombre del mod de TTS; si ya existe, se añade un sufijo `_2`, `_3`, …
- Descarga concurrente (hasta 8 hilos) con reintentos y rate-limit.
- Solo se descargan extensiones conocidas: `.jpg`, `.jpeg`, `.png`, `.pdf`, `.obj`. Otros tipos se omiten.
- Recorte automático de cartas front/back desde las hojas.
- Conversión automática de modelos `.obj` a `.stl` al finalizar la descarga en la misma carpeta `model/`.
- Genera un CSV con todas las URLs y nombres de archivo.
- El CSV usa separador `;` y codificación UTF-8 con BOM (abre bien en Excel).
- Comprime las hojas originales en `sheets.zip` y limpia carpetas vacías.
- Barra de progreso, cancelación y registro en tiempo real.
- Nombres de archivo seguros y únicos.
- Si cancelas a mitad de proceso, los archivos ya descargados se conservan.
- Genera un archivo log con el registro de la salida del script.

---

## Requisitos

- **Python 3.8+**
- Librerías **obligatorias**:
  ```bash
  pip install requests Pillow pymongo trimesh
  ```

---

## Estructura de salida

Se crea una carpeta con el nombre del save / título del Workshop:

```
NombreDelMod/
├── cards/                 ← cartas individuales recortadas (front + back)
├── token/                 ← tokens y figurines
├── model/                 ← .obj, texturas y .stl convertidos
├── pdf/                   ← documentos PDF
├── image/                 ← tiles, tableros, imágenes
├── sheets.zip             ← hojas originales comprimidas
├── NombreDelMod_log.txt   ← registro de la ejecución
└── NombreDelMod_urls.csv  ← CSV con las URLs descargadas
```

---
