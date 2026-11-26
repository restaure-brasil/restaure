"""Utilitários comuns: códigos SIGEF e aproximação do mapa."""
import re

from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def extrair_codigos(texto):
    """Pega todos os códigos de parcela de qualquer texto (links, logs, planilhas)."""
    codigos = []
    for c in _UUID.findall(texto or ""):
        c = c.lower()
        if c not in codigos:
            codigos.append(c)
    return codigos


def aproximar(canvas, camadas):
    destino = canvas.mapSettings().destinationCrs()
    ext = QgsRectangle()
    ext.setMinimal()
    for cam in camadas:
        tr = QgsCoordinateTransform(cam.crs(), destino, QgsProject.instance())
        try:
            ext.combineExtentWith(tr.transformBoundingBox(cam.extent()))
        except Exception:
            pass
    if not ext.isNull() and ext.width() > 0:
        ext.scale(1.15)
        canvas.setExtent(ext)
        canvas.refresh()
