"""
Servidor web del separador de PDF.
Sube un PDF combinado, lo separa en documentos individuales y los entrega
sueltos para descarga (sin zip comprimido, para que sea rapido).
"""
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from separar_pdf import separar

BASE_DIR = Path(__file__).parent
SALIDAS_DIR = BASE_DIR / "salidas"
SALIDAS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Separador de PDF")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/separar")
async def separar_endpoint(archivo: UploadFile = File(...)):
    if not archivo.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos .pdf")

    job_id = uuid.uuid4().hex[:8]
    job_dir = SALIDAS_DIR / job_id
    job_dir.mkdir(parents=True)

    entrada = job_dir / "original.pdf"
    with open(entrada, "wb") as f:
        shutil.copyfileobj(archivo.file, f)

    carpeta_salida = job_dir / "separado"
    try:
        archivos = separar(entrada, carpeta_salida)
    except Exception as e:
        raise HTTPException(500, f"No se pudo separar el PDF: {e}")

    return {
        "job_id": job_id,
        "archivos": [a.name for a in archivos],
    }


@app.get("/descargar/{job_id}/{nombre}")
def descargar(job_id: str, nombre: str):
    nombre_seguro = Path(nombre).name
    ruta = SALIDAS_DIR / job_id / "separado" / nombre_seguro
    if not ruta.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    return FileResponse(ruta, filename=nombre_seguro, media_type="application/pdf")


@app.get("/descargar_zip/{job_id}")
def descargar_zip(job_id: str):
    carpeta = SALIDAS_DIR / job_id / "separado"
    if not carpeta.is_dir():
        raise HTTPException(404, "Lote no encontrado")

    zip_path = SALIDAS_DIR / job_id / "documentos.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:  # sin comprimir = rapido
        for pdf in sorted(carpeta.glob("*.pdf")):
            zf.write(pdf, pdf.name)

    return FileResponse(zip_path, filename="documentos.zip", media_type="application/zip")
