"""
Separador de PDF combinado en documentos individuales.
Detecta inicio de cada documento por palabras clave de encabezado.
Si el PDF no trae texto (escaneo/foto), hace OCR rapido pagina por pagina.
"""
import os
import re
import sys
import time
from pathlib import Path

import fitz  # pymupdf
import pdfplumber
import pytesseract
from pypdf import PdfReader, PdfWriter

TESSERACT_EXE_WINDOWS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(TESSERACT_EXE_WINDOWS).exists():
    # Desarrollo local en Windows: tesseract no esta en PATH, se apunta directo
    # al exe y a los idiomas descargados junto al proyecto.
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE_WINDOWS
    os.environ["TESSDATA_PREFIX"] = str(Path(__file__).parent / "tessdata")
# En Docker/Linux, tesseract-ocr y tesseract-ocr-spa se instalan via apt y
# quedan en el PATH del sistema, no hace falta configurar nada mas.

# Frases que marcan el INICIO de un documento nuevo.
# Se busca SOLO en la parte de arriba de cada pagina (encabezado/logo),
# para no disparar con texto suelto que aparece mas abajo (pie de pagina,
# clausulas legales, etc). Orden = prioridad: las mas especificas primero,
# el comodin "CERTIFICA QUE" de ultimo para no robarle paginas a carnet,
# colpensiones, etc.
VENTANA_ENCABEZADO = 350  # caracteres desde el inicio de la pagina

MARCADORES = [
    (r"CARNET", "carnet_manipulacion"),
    (r"IDENTIFICACION\s+PERSONAL", "cedula"),
    (r"INSTITUCI[OÓ]N\s+EDUCATIVA|CONFIERE\s+A|BACHILLER", "diploma"),
    (r"PROCURADUR", "antecedentes_procuraduria"),
    (r"POLIC[IÍ\.]A\s+NACIONAL", "antecedentes_policia"),
    (r"CONTRALOR", "antecedentes_contraloria"),
    (r"PERSONER[IÍ]A", "antecedentes_personeria"),
    (r"NOTARIA|DECLARACI[OÓ]N\s+EXTRAJ", "declaracion_notarial"),
    (r"SALUD\s*TOTAL", "eps"),
    (r"COLPENSIONES", "colpensiones"),
    (r"HISTORIA\s+LABORAL", "historia_laboral"),
    (r"PORVENIR|FONDO\s+DE\s+CESANT", "cesantias"),
    (r"HOJA\s+DE\s+VIDA", "hoja_de_vida"),
    (r"\bCERTIFICA\s*[:\.]?\s*(QUE)?\b", "certificacion_laboral"),
    (r"^\s*\d{1,2}\s+de\s+\w+\s+(de\s+)?\d{4}", "carta_referencia"),  # carta con fecha al inicio
]
MARCADORES = [(re.compile(p, re.IGNORECASE | re.MULTILINE), n) for p, n in MARCADORES]

# Tipos de documento que repiten su encabezado en cada pagina (reportes
# multi-pagina, p.ej colpensiones o historia laboral). Si el mismo tipo
# aparece en paginas consecutivas, se consideran la MISMA continuacion en
# vez de un documento nuevo.
CONTINUA_SI_SE_REPITE = {"colpensiones", "historia_laboral", "declaracion_notarial"}


def clasificar(texto: str) -> str | None:
    encabezado = texto[:VENTANA_ENCABEZADO]
    for patron, nombre in MARCADORES:
        if patron.search(encabezado):
            return nombre
    # Sin encabezado reconocible (firma escaneada, hoja en blanco, foto
    # borrosa, carta a mano sin fecha legible): se pega al documento
    # anterior en vez de inventar un corte. Mejor unir de mas que separar
    # de mas.
    return None


def ocr_pagina(doc: "fitz.Document", indice: int, zoom: float = 2.0) -> str:
    pagina = doc[indice]
    matriz = fitz.Matrix(zoom, zoom)
    pix = pagina.get_pixmap(matrix=matriz)
    img_bytes = pix.tobytes("png")
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    return pytesseract.image_to_string(img, lang="spa+eng", config="--psm 6")


UMBRAL_TEXTO_VACIO = 20  # menos que esto = se asume pagina-imagen sin texto real


def extraer_textos(pdf_path: Path) -> list[str]:
    textos = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pagina in pdf.pages:
            textos.append(pagina.extract_text() or "")

    # PDFs mixtos: algunas paginas traen texto digital y otras son imagenes
    # pegadas (escaneos) sin capa de texto. Se hace OCR SOLO en esas paginas
    # puntuales, no en todo el documento, para no perder velocidad.
    indices_sin_texto = [i for i, t in enumerate(textos) if len(t.strip()) < UMBRAL_TEXTO_VACIO]
    if indices_sin_texto:
        doc = fitz.open(str(pdf_path))
        for i in indices_sin_texto:
            textos[i] = ocr_pagina(doc, i)
        doc.close()
    return textos


def separar(pdf_path: Path, carpeta_salida: Path) -> list[Path]:
    t0 = time.time()
    textos = extraer_textos(pdf_path)
    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)

    cortes = []  # indice de pagina donde inicia doc nuevo
    nombres = []
    nombre_anterior = None
    for i, texto in enumerate(textos):
        nombre = clasificar(texto)
        if nombre and nombre == nombre_anterior and nombre in CONTINUA_SI_SE_REPITE:
            continue  # misma continuacion del reporte, no es doc nuevo
        if nombre or i == 0:
            cortes.append(i)
            nombres.append(nombre or "documento")
        if nombre:
            nombre_anterior = nombre

    # eliminar cortes consecutivos iguales sin avance real (evita doc de 0 paginas)
    cortes_finales = [cortes[0]]
    nombres_finales = [nombres[0]]
    for c, nm in zip(cortes[1:], nombres[1:]):
        if c != cortes_finales[-1]:
            cortes_finales.append(c)
            nombres_finales.append(nm)

    carpeta_salida.mkdir(parents=True, exist_ok=True)
    archivos = []
    for idx, inicio in enumerate(cortes_finales):
        fin = cortes_finales[idx + 1] if idx + 1 < len(cortes_finales) else n
        writer = PdfWriter()
        for p in range(inicio, fin):
            writer.add_page(reader.pages[p])
        nombre_doc = f"{idx + 1:02d}_{nombres_finales[idx]}.pdf"
        salida = carpeta_salida / nombre_doc
        with open(salida, "wb") as f:
            writer.write(f)
        archivos.append(salida)

    print(f"{len(archivos)} documentos generados en {time.time() - t0:.1f}s -> {carpeta_salida}")
    return archivos


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python separar_pdf.py archivo.pdf [carpeta_salida] [--debug]")
        sys.exit(1)
    entrada = Path(sys.argv[1])
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    salida = Path(args[0]) if args else entrada.parent / f"{entrada.stem}_separado"

    if "--debug" in sys.argv:
        textos = extraer_textos(entrada)
        for i, t in enumerate(textos):
            nombre = clasificar(t)
            print(f"--- pagina {i} -> {nombre} ---")
            print(t[:200].replace(chr(10), " | "))
            print()
        sys.exit(0)

    separar(entrada, salida)
