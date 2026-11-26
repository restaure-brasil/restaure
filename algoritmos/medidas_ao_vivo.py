# -*- coding: utf-8 -*-
"""Painel de medidas ao vivo (Restaure › 3. Ferramentas úteis).

Caixinha flutuante sobre o mapa com a área (ha) e o perímetro (km), em tempo real:
  • linha 1: no SRC escolhido (UTM automático, SRC da camada ou qualquer outro);
  • linha 2: no SGL – Sistema Geodésico Local (como o SIGEF);
  • a diferença entre as duas.

Este arquivo é a ÚNICA fonte das regras de altitude (z) do Restaure:
  • a COLUNA "z" vale mais que o Z da geometria (é mais fácil de editar);
  • z vazio (NaN) ou 0 conta como "sem altitude";
  • ponto e vértice estão "no mesmo lugar" até TOL_M (1 cm);
  • coluna e geometria são diferentes acima de DIF_M (meio milímetro).

Seções: regras de z › cálculo no SGL › aviso de camadas sem z › motor ›
caixinha › menu e entrada na Caixa de Ferramentas.
"""
import math
import os
from functools import partial

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
    QgsFeatureRequest, QgsField, QgsGeometry, QgsPoint, QgsPointXY, QgsProject,
    QgsSpatialIndex, QgsVectorLayer, QgsVertexId, QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox, QgsProjectionSelectionDialog
from qgis.PyQt.QtCore import QObject, QSettings, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QVBoxLayout,
)

from ..compat import (
    FILTRO_CAMADA_PONTO, GEOM_LINHA, GEOM_POLIGONO, GEOM_PONTO, TIPO_DECIMAL, TIPO_INTEIRO, TIPO_TEXTO,
)
from . import PASTA_PLUGIN
from .lancador import FerramentaInterativa

try:                                          # Qt6: QAction fica em QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

NOME = "Painel de medidas ao vivo"
ICONE = os.path.join(PASTA_PLUGIN, "docs", "images", "icon_medidas.png")
CFG = "medidas_ao_vivo/"      # chave do QSettings (mesma da versão avulsa: mantém as escolhas)
LIMITE_TOTAL = 20000          # acima disso, sem seleção, não mede a camada inteira
LIMITE_VERTICES = 300000      # por camada, para o aviso de z não travar

CAMPO_Z = "z"
TOL_M = 0.01                  # vértice "no mesmo lugar" do ponto: até 1 cm
DIF_M = 0.0005                # diferença coluna x geometria considerada (meio milímetro)
EPSG_SIRGAS = 4674            # SIRGAS 2000 geográfico: base do SGL


def fmt(v, casas, sinal=False):
    if v is None:
        return "–"
    txt = ("{:%s,.%df}" % ("+" if sinal else "", casas)).format(v)
    return txt.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_int(n):
    return "{:,}".format(n).replace(",", ".")


def _sirgas():
    return QgsCoordinateReferenceSystem.fromEpsgId(EPSG_SIRGAS)


# ================================================================ regras de z
def z_valido(z):
    return z is not None and isinstance(z, (int, float)) and not math.isnan(z) and z != 0


def valor_coluna(f, idx):
    if idx < 0:
        return float("nan")
    try:
        return float(f.attribute(idx))
    except (TypeError, ValueError):
        return float("nan")


def z_geometria(geom):
    """Z do (primeiro) vértice da geometria; NaN se a geometria não tiver Z."""
    if geom is None or geom.isEmpty():
        return float("nan")
    p = geom.constGet()
    if not QgsWkbTypes.hasZ(p.wkbType()):
        return float("nan")
    for v in geom.vertices():
        return v.z()
    return float("nan")


def tolerancia(crs):
    return TOL_M / 111320.0 if crs.isGeographic() else TOL_M


def vertices_com_id(abst):
    out = []
    vid = QgsVertexId()
    ok, v = abst.nextVertex(vid)
    while ok:
        out.append((QgsVertexId(vid.part, vid.ring, vid.vertex), v.x(), v.y(), v.z()))
        ok, v = abst.nextVertex(vid)
    return out


def geometria_com_z(geom, z):
    """Cópia da geometria com o Z de todos os vértices trocado."""
    g = QgsGeometry(geom)
    abst = g.get()
    if not QgsWkbTypes.hasZ(abst.wkbType()):
        abst.addZValue(z)
    for vid, x, y, _ in vertices_com_id(abst):
        abst.moveVertex(vid, QgsPoint(x, y, z))
    return g


def conferir_z(camada, campo=CAMPO_Z):
    """Compara a coluna de altitude com o Z da geometria numa camada de pontos.
    Devolve dict: total, diferentes [fid], so_coluna, so_geometria, sem_altitude,
    campo (nome achado ou None), tem_z."""
    idx = camada.fields().lookupField(campo)
    tem_z = QgsWkbTypes.hasZ(camada.wkbType())
    r = {"total": 0, "diferentes": [], "so_coluna": 0, "so_geometria": 0, "sem_altitude": 0,
         "campo": camada.fields().at(idx).name() if idx >= 0 else None, "tem_z": tem_z}
    for f in camada.getFeatures():
        r["total"] += 1
        zc = valor_coluna(f, idx)
        zg = z_geometria(f.geometry()) if tem_z else float("nan")
        c, g = z_valido(zc), z_valido(zg)
        if c and g and abs(zc - zg) > DIF_M:
            r["diferentes"].append(f.id())
        elif c and not g:
            r["so_coluna"] += 1
        elif g and not c:
            r["so_geometria"] += 1
        elif not c and not g:
            r["sem_altitude"] += 1
    return r


def criar_coluna(camada, campo=CAMPO_Z, sobrescrever=False):
    """Cria (se faltar) a coluna de altitude e preenche com o Z da geometria.
    Deixa a camada em edição, sem salvar. Devolve (criada, preenchidos)."""
    if not camada.isEditable():
        camada.startEditing()
    criada = False
    idx = camada.fields().lookupField(campo)
    if idx < 0:
        camada.addAttribute(QgsField(campo, TIPO_DECIMAL, "double", 20, 3))
        camada.updateFields()
        idx = camada.fields().lookupField(campo)
        criada = True
    n = 0
    camada.beginEditCommand("Medidas ao vivo: preencher coluna {}".format(campo))
    for f in camada.getFeatures():
        zg = z_geometria(f.geometry())
        if not z_valido(zg):
            continue
        zc = valor_coluna(f, idx)
        if z_valido(zc) and (not sobrescrever or abs(zc - zg) <= DIF_M):
            continue
        camada.changeAttributeValue(f.id(), idx, round(zg, 3))
        n += 1
    camada.endEditCommand()
    return criada, n


def pontos_seguem_coluna(camada, campo=CAMPO_Z):
    """Atualiza o Z de todos os pontos com o valor da coluna (a coluna vale)."""
    idx = camada.fields().lookupField(campo)
    if idx < 0 or not QgsWkbTypes.hasZ(camada.wkbType()):
        return 0
    if not camada.isEditable():
        camada.startEditing()
    n = 0
    camada.beginEditCommand("Medidas ao vivo: Z dos pontos pela coluna")
    for f in camada.getFeatures():
        zc = valor_coluna(f, idx)
        if not z_valido(zc):
            continue
        zg = z_geometria(f.geometry())
        if z_valido(zg) and abs(zg - zc) <= DIF_M:
            continue
        camada.changeGeometry(f.id(), geometria_com_z(f.geometry(), zc))
        n += 1
    camada.endEditCommand()
    return n


class IndicePontos:
    """Altitudes de uma camada de pontos (vale a coluna; sem ela, o Z da geometria) no
    SRC de destino, para achar o ponto no mesmo lugar de um vértice (até TOL_M).
    Guarda também os pontos com coluna diferente do Z da geometria (divergentes)."""

    def __init__(self, pontos, crs_destino, campo=CAMPO_Z):
        self.idx = QgsSpatialIndex()
        self.dados = {}
        self.divergentes = []
        self.tol = tolerancia(crs_destino)
        k = pontos.fields().lookupField(campo)
        ct = None
        if pontos.crs() != crs_destino:
            ct = QgsCoordinateTransform(pontos.crs(), crs_destino, QgsProject.instance())
        for f in pontos.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            zc = valor_coluna(f, k)
            zg = z_geometria(g)
            if z_valido(zc) and z_valido(zg) and abs(zc - zg) > DIF_M:
                self.divergentes.append(f.id())
            for v in g.vertices():            # vale também para multiponto
                z = zc if z_valido(zc) else (v.z() if v.is3D() else float("nan"))
                if not z_valido(z):
                    continue
                xy = QgsPointXY(v.x(), v.y())
                if ct is not None:
                    xy = ct.transform(xy)
                n = len(self.dados) + 1
                pf = QgsFeature(n)
                pf.setGeometry(QgsGeometry.fromPointXY(xy))
                self.idx.addFeature(pf)
                self.dados[n] = (xy, z)

    def z_em(self, x, y):
        cand = self.idx.nearestNeighbor(QgsPointXY(x, y), 1, self.tol)
        if cand:
            xy, z = self.dados[cand[0]]
            if xy.distance(QgsPointXY(x, y)) <= self.tol:
                return z
        return None


def atualizar_alvos(pontos, alvos, campo=CAMPO_Z):
    """Põe nos vértices das camadas `alvos` (polígonos/linhas com Z) o Z do ponto que
    está no mesmo lugar. Deixa em edição, sem salvar. Devolve a lista de dicts por
    camada e a lista de vértices sem ponto [(camada, fid, n_vertice, x, y, crs)]."""
    relatorio, sem_ponto = [], []
    for alvo in alvos:
        info = {"camada": alvo.name(), "vertices": 0, "atualizados": 0,
                "iguais": 0, "sem_ponto": 0, "sem_z_camada": False}
        if not QgsWkbTypes.hasZ(alvo.wkbType()):
            info["sem_z_camada"] = True
            relatorio.append(info)
            continue
        indice = IndicePontos(pontos, alvo.crs(), campo)
        if not alvo.isEditable():
            alvo.startEditing()
        alvo.beginEditCommand("Medidas ao vivo: Z pelos pontos")
        for f in alvo.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            g2 = QgsGeometry(g)
            abst = g2.get()
            mudou = False
            vistos = {}
            k = 0
            for vid, x, y, z in vertices_com_id(abst):
                chave = (x, y)
                if chave not in vistos:        # o fechamento do anel conta uma vez só
                    k += 1
                    vistos[chave] = k
                    info["vertices"] += 1
                novo = vistos[chave] == k
                zn = indice.z_em(x, y)
                if zn is None:
                    if novo and not z_valido(z):
                        info["sem_ponto"] += 1
                        sem_ponto.append((alvo.name(), f.id(), vistos[chave], x, y, alvo.crs()))
                    continue
                if z_valido(z) and abs(z - zn) <= DIF_M:
                    if novo:
                        info["iguais"] += 1
                    continue
                abst.moveVertex(vid, QgsPoint(x, y, zn))
                mudou = True
                if novo:
                    info["atualizados"] += 1
            if mudou:
                alvo.changeGeometry(f.id(), g2)
        alvo.endEditCommand()
        relatorio.append(info)
    return relatorio, sem_ponto


def camada_sem_ponto(sem_ponto):
    """Camada temporária com os vértices sem ponto correspondente (e sem Z)."""
    if not sem_ponto:
        return None
    crs = sem_ponto[0][5]
    lyr = QgsVectorLayer("Point?crs={}".format(crs.authid() or crs.toWkt()),
                         "Vértices sem ponto (Medidas ao vivo)", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("camada", TIPO_TEXTO), QgsField("feicao", TIPO_INTEIRO),
                      QgsField("n_vertice", TIPO_INTEIRO)])
    lyr.updateFields()
    feats = []
    for cam, fid, k, x, y, c in sem_ponto:
        p = QgsPointXY(x, y)
        if c != crs:
            p = QgsCoordinateTransform(c, crs, QgsProject.instance()).transform(p)
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(p))
        f.setAttributes([cam, fid, k])
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


# ================================================================ cálculo no SGL
GRS80_A = 6378137.0
GRS80_F = 1 / 298.257222101
GRS80_E2 = GRS80_F * (2 - GRS80_F)


def _geocentrico(lon, lat, h):
    fi, la = math.radians(lat), math.radians(lon)
    n = GRS80_A / math.sqrt(1 - GRS80_E2 * math.sin(fi) ** 2)
    return ((n + h) * math.cos(fi) * math.cos(la),
            (n + h) * math.cos(fi) * math.sin(la),
            (n * (1 - GRS80_E2) + h) * math.sin(fi))


def _geodesico(x, y, z):
    """Cartesianas geocêntricas -> (lon, lat) em graus (GRS80, iterativo)."""
    la = math.atan2(y, x)
    p = math.hypot(x, y)
    fi = math.atan2(z, p * (1 - GRS80_E2))
    for _ in range(10):
        n = GRS80_A / math.sqrt(1 - GRS80_E2 * math.sin(fi) ** 2)
        h = p / math.cos(fi) - n
        fi = math.atan2(z, p * (1 - GRS80_E2 * n / (n + h)))
    return math.degrees(la), math.degrees(fi)


def para_sgl(pontos):
    """[(lon, lat, h)] -> [(e, n)] no SGL. Como no Manual Técnico de Posicionamento do
    INCRA: a origem é a MÉDIA DAS COORDENADAS CARTESIANAS GEOCÊNTRICAS dos vértices, e as
    coordenadas locais vêm da rotação do vetor (X-X0, Y-Y0, Z-Z0) pela latitude e longitude
    da origem; a área é calculada com (e, n)."""
    geo = [_geocentrico(lon, lat, h) for lon, lat, h in pontos]
    k = len(geo)
    x0 = sum(g[0] for g in geo) / k
    y0 = sum(g[1] for g in geo) / k
    z0 = sum(g[2] for g in geo) / k
    lon0, lat0 = _geodesico(x0, y0, z0)
    fi0, la0 = math.radians(lat0), math.radians(lon0)
    sf, cf, sl, cl = math.sin(fi0), math.cos(fi0), math.sin(la0), math.cos(la0)
    out = []
    for x, y, z in geo:
        dx, dy, dz = x - x0, y - y0, z - z0
        out.append((-sl * dx + cl * dy, -sf * cl * dx - sf * sl * dy + cf * dz))
    return out


def _area_anel(pts):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _comp(pts, fechado):
    seq = pts + pts[:1] if fechado else pts
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(seq, seq[1:]))


def _pontos(curva, sem_z=None, fonte_z=None):
    """Vértices (lon, lat, h). Z da geometria → fonte_z (camada de vértices). Os que
    ficam sem z voltam com h = None (preenchido depois em medir_sgl) e contam em sem_z."""
    out = []
    for v in curva.vertices():
        z = v.z() if v.is3D() else None
        if not z_valido(z):
            z = fonte_z(v.x(), v.y()) if fonte_z is not None else None
        if z is None and sem_z is not None:
            sem_z[0] += 1
        out.append((v.x(), v.y(), z))
    return out


def _preencher_z(aneis, z_fixo=None):
    """Vértices sem z recebem a MÉDIA dos vértices da própria feição que têm z; se
    nenhum tiver, a altitude fixa (se definida) ou 0."""
    validos = [p[2] for anel, _ in aneis for p in anel if p[2] is not None]
    if validos:
        h = sum(validos) / len(validos)
    else:
        h = z_fixo if z_fixo is not None else 0.0
    return [([(x, y, h if z is None else z) for x, y, z in anel], sinal) for anel, sinal in aneis]


def medir_sgl(g_geo, poligono, sem_z=None, fonte_z=None, z_fixo=None):
    """g_geo em SIRGAS 2000 geográfico. Uma origem (SGL) por feição. Altitude de cada
    vértice: Z da geometria → camada de vértices → média da feição → altitude fixa → 0."""
    aneis = []
    for parte in g_geo.constParts():
        if poligono:
            aneis.append((_pontos(parte.exteriorRing(), sem_z, fonte_z), 1))
            for k in range(parte.numInteriorRings()):
                aneis.append((_pontos(parte.interiorRing(k), sem_z, fonte_z), -1))
        else:
            aneis.append((_pontos(parte, sem_z, fonte_z), 0))
    aneis = _preencher_z(aneis, z_fixo)
    todos = [p for anel, _ in aneis for p in (anel[:-1] if poligono and len(anel) > 1 else anel)]
    if len(todos) < 2:
        return (0.0 if poligono else None), 0.0
    sgl = para_sgl(todos)
    area = perim = 0.0
    k = 0
    for anel, sinal in aneis:
        n = len(anel) - 1 if poligono and len(anel) > 1 else len(anel)
        pts = sgl[k:k + n]
        k += n
        if poligono:
            area += sinal * _area_anel(pts)
            perim += _comp(pts, True)
        else:
            perim += _comp(pts, False)
    return (area if poligono else None), perim


def _perimetro_plano(g):
    total = 0.0
    for parte in g.constParts():
        anel = parte.exteriorRing()
        if anel is not None:
            total += anel.length()
        for k in range(parte.numInteriorRings()):
            total += parte.interiorRing(k).length()
    return total


def utm_automatico(lyr, geoms):
    tr = QgsCoordinateTransform(lyr.crs(), _sirgas(), QgsProject.instance())
    g = next((x for x in geoms if x is not None and not x.isEmpty()), None)
    c = tr.transform(g.boundingBox().center() if g is not None else lyr.extent().center())
    zona = int((c.x() + 180) / 6) + 1
    return QgsCoordinateReferenceSystem.fromEpsgId((31960 if c.y() < 0 else 31954) + zona)


# ================================================================ aviso de camadas sem z
MARCADO = Qt.CheckState.Checked
DESMARCADO = Qt.CheckState.Unchecked
PAPEL_ID = Qt.ItemDataRole.UserRole
MARCAVEL = Qt.ItemFlag.ItemIsUserCheckable
HABILITADO = Qt.ItemFlag.ItemIsEnabled


def diagnosticar(lyr):
    """Situação de z de uma camada. Devolve dict ou None (sem problema)."""
    if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
        return None
    gt = QgsWkbTypes.geometryType(lyr.wkbType())
    if gt not in (GEOM_PONTO, GEOM_LINHA, GEOM_POLIGONO):
        return None
    tem_z = QgsWkbTypes.hasZ(lyr.wkbType())
    if gt == GEOM_PONTO:
        r = conferir_z(lyr)
        if r["campo"] is None and not tem_z:
            return {"camada": lyr, "tipo": "2d", "texto": "pontos 2D, sem coluna z",
                    "preenchivel": False}
        partes = []
        if r["diferentes"]:
            partes.append("%s com coluna z ≠ Z (vale a coluna)" % _fmt_int(len(r["diferentes"])))
        if tem_z and r["so_coluna"]:
            partes.append("%s com z só na coluna" % _fmt_int(r["so_coluna"]))
        if r["campo"] and r["so_geometria"]:
            partes.append("%s com z só na geometria" % _fmt_int(r["so_geometria"]))
        if r["sem_altitude"]:
            partes.append("%s sem z nenhum" % _fmt_int(r["sem_altitude"]))
        if not partes:
            return None
        return {"camada": lyr, "tipo": "pontos",
                "texto": "; ".join(partes) + " (de %s pontos)" % _fmt_int(r["total"]),
                "preenchivel": bool(r["diferentes"] or r["so_coluna"] or r["so_geometria"])}
    if not tem_z:
        return {"camada": lyr, "tipo": "2d", "texto": "camada 2D: não guarda z",
                "preenchivel": False}
    total = sem = 0
    for f in lyr.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        for v in g.vertices():
            total += 1
            if not z_valido(v.z()):
                sem += 1
        if total > LIMITE_VERTICES:
            break
    if not sem:
        return None
    mais = "+" if total > LIMITE_VERTICES else ""
    return {"camada": lyr, "tipo": "vertices",
            "texto": "%s%s de %s%s vértices sem z" % (_fmt_int(sem), mais, _fmt_int(total), mais),
            "preenchivel": True}


def listar_sem_z(camadas_extra=None):
    """Camadas do projeto com problema de z. Camadas 2D só entram se vierem em
    camadas_extra (as que o painel mede), para não listar mapas de fundo."""
    extra = [c for c in (camadas_extra or []) if c is not None]
    ids_extra = {c.id() for c in extra}
    out, vistos = [], set()
    for lyr in extra + list(QgsProject.instance().mapLayers().values()):
        if not isinstance(lyr, QgsVectorLayer) or lyr.id() in vistos:
            continue
        vistos.add(lyr.id())
        d = diagnosticar(lyr)
        if d is None or (d["tipo"] == "2d" and lyr.id() not in ids_extra):
            continue
        out.append(d)
    return out


class DialogoCamadasSemZ(QDialog):
    def __init__(self, iface, itens, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.itens = itens
        self.setWindowTitle("Camadas sem z – %s" % NOME)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Estas camadas têm vértices sem altitude (z vazio ou 0). O cálculo continua; "
            "nesses vértices a precisão do SGL diminui.\nMarque as que quer preencher ou "
            "clique em Ignorar. Este aviso só volta quando o painel for fechado e aberto "
            "de novo."))
        self.lista = QListWidget()
        for i, d in enumerate(itens):
            it = QListWidgetItem("%s — %s" % (d["camada"].name(), d["texto"]))
            it.setData(PAPEL_ID, i)
            if d["preenchivel"]:
                it.setFlags(HABILITADO | MARCAVEL)
                it.setCheckState(DESMARCADO)
            else:
                it.setFlags(HABILITADO)
                it.setToolTip("Não dá para preencher aqui: converta a camada para 3D ou "
                              "gere o z a partir de um MDE.")
            self.lista.addItem(it)
        lay.addWidget(self.lista)

        hb = QHBoxLayout()
        hb.addWidget(QLabel("Polígonos e linhas recebem o z dos pontos de:"))
        self.cb_pontos = QComboBox()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and \
                    QgsWkbTypes.geometryType(lyr.wkbType()) == GEOM_PONTO:
                self.cb_pontos.addItem(lyr.name(), lyr.id())
        i = self.cb_pontos.findText("vertice")
        if i >= 0:
            self.cb_pontos.setCurrentIndex(i)
        hb.addWidget(self.cb_pontos, 1)
        lay.addLayout(hb)
        lay.addWidget(QLabel("Pontos: a coluna z e o Z da geometria ficam iguais (vale a "
                             "coluna; o que estiver vazio é preenchido pelo outro)."))
        self.resultado = QLabel("")
        self.resultado.setWordWrap(True)
        lay.addWidget(self.resultado)

        caixa = QDialogButtonBox()
        self.bt_preencher = QPushButton("Preencher marcadas")
        self.bt_ignorar = QPushButton("Ignorar")
        caixa.addButton(self.bt_preencher, QDialogButtonBox.ButtonRole.AcceptRole)
        caixa.addButton(self.bt_ignorar, QDialogButtonBox.ButtonRole.RejectRole)
        self.bt_preencher.clicked.connect(self.preencher)
        self.bt_ignorar.clicked.connect(self.reject)
        lay.addWidget(caixa)

    def marcados(self):
        out = []
        for k in range(self.lista.count()):
            it = self.lista.item(k)
            if it.checkState() == MARCADO:
                out.append(self.itens[it.data(PAPEL_ID)])
        return out

    def preencher(self):
        alvos = self.marcados()
        if not alvos:
            self.resultado.setText("Nenhuma camada marcada.")
            return
        msgs = []
        pontos = QgsProject.instance().mapLayer(self.cb_pontos.currentData()) \
            if self.cb_pontos.count() else None
        linhas_poligonos = [d["camada"] for d in alvos if d["tipo"] == "vertices"]
        for d in alvos:
            if d["tipo"] == "pontos":
                lyr = d["camada"]
                _, n_col = criar_coluna(lyr, sobrescrever=False)
                n_geo = pontos_seguem_coluna(lyr)
                lyr.triggerRepaint()
                msgs.append("%s: %d coluna(s) preenchida(s) pelo Z, %d ponto(s) com o Z da "
                            "coluna." % (lyr.name(), n_col, n_geo))
        if linhas_poligonos:
            if pontos is None:
                msgs.append("Escolha a camada de pontos para preencher polígonos e linhas.")
            else:
                rel, sem = atualizar_alvos(pontos, linhas_poligonos)
                for i in rel:
                    msgs.append("%s: %s vértice(s) receberam z de '%s'; %s sem ponto "
                                "correspondente." % (i["camada"], _fmt_int(i["atualizados"]),
                                                     pontos.name(), _fmt_int(i["sem_ponto"])))
                cam = camada_sem_ponto(sem)
                if cam is not None:
                    QgsProject.instance().addMapLayer(cam)
                    msgs.append("Vértices sem ponto: camada '%s'." % cam.name())
                for lyr in linhas_poligonos:
                    lyr.triggerRepaint()
        msgs.append("As camadas ficaram em edição: confira e salve.")
        self.resultado.setText("\n".join(msgs))
        self.bt_preencher.setEnabled(False)
        self.bt_ignorar.setText("Fechar")


class AvisoZSessao:
    """Mostra o diálogo no máximo uma vez até reiniciar() (fechar o painel)."""

    def __init__(self, iface):
        self.iface = iface
        self.mostrado = False
        self.dialogo = None

    def reiniciar(self):
        self.mostrado = False

    def verificar(self, camadas_extra=None):
        if self.mostrado:
            return False
        itens = listar_sem_z(camadas_extra)
        if not itens:
            return False
        self.mostrado = True
        self.dialogo = DialogoCamadasSemZ(self.iface, itens)
        self.dialogo.setModal(False)
        self.dialogo.show()
        return True


# ================================================================ motor
class Motor(QObject):
    """Acompanha a camada a medir e recalcula a cada alteração."""
    SINAIS = ("editingStarted", "editingStopped", "layerModified", "selectionChanged",
              "afterRollBack", "dataChanged", "geometryChanged", "featureAdded", "featureDeleted")
    SINAIS_VERTICES = ("featureAdded", "featureDeleted", "geometryChanged",
                       "attributeValueChanged", "afterRollBack", "dataChanged")

    def __init__(self, iface, ao_calcular):
        super().__init__()
        self.iface = iface
        self.ao_calcular = ao_calcular
        self.camada = None
        self.ativo = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.calcular)
        iface.currentLayerChanged.connect(self.reavaliar)
        self._indice = None              # (id da camada de vértices, IndicePontos)
        self._cam_vert = None
        self._ultimo_editando = None     # id da camada que entrou em edição por último
        self._vigiadas = {}              # id -> camada com editingStarted/Stopped ligados
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._vigiar)
        self._vigiar(list(prj.mapLayers().values()))

    # escolha do SRC: "utm" (automático), "camada" ou authid (ex.: "EPSG:31980")
    @property
    def src(self):
        return QSettings().value(CFG + "src", "utm")

    @src.setter
    def src(self, valor):
        QSettings().setValue(CFG + "src", valor)
        self.agendar()

    # ---- fontes opcionais de z para o SGL
    @property
    def z_vertices(self):
        """id da camada de pontos usada para buscar o z ('' = desligado)."""
        return QSettings().value(CFG + "z_vertices", "")

    @z_vertices.setter
    def z_vertices(self, lid):
        QSettings().setValue(CFG + "z_vertices", lid or "")
        self._indice = None
        self.agendar()

    @property
    def z_fixo(self):
        v = QSettings().value(CFG + "z_fixo", "")
        try:
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None

    @z_fixo.setter
    def z_fixo(self, valor):
        QSettings().setValue(CFG + "z_fixo", "" if valor is None else valor)
        self.agendar()

    def _sujar_indice(self, *_):
        self._indice = None
        self.agendar()

    def camada_vertices(self):
        lid = self.z_vertices
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        return lyr if isinstance(lyr, QgsVectorLayer) else None

    def _indice_vertices(self):
        """IndicePontos da camada de vértices em SIRGAS 2000 (ou None)."""
        lyr = self.camada_vertices()
        if lyr is not self._cam_vert:                      # liga/desliga avisos de mudança
            for cam, ligar in ((self._cam_vert, False), (lyr, True)):
                if cam is None:
                    continue
                for nome in self.SINAIS_VERTICES:
                    sinal = getattr(cam, nome, None)
                    if sinal is not None:
                        try:
                            (sinal.connect if ligar else sinal.disconnect)(self._sujar_indice)
                        except (TypeError, RuntimeError):
                            pass
            self._cam_vert = lyr
            self._indice = None
        if lyr is None:
            return None
        if self._indice is None or self._indice[0] != lyr.id():
            self._indice = (lyr.id(), IndicePontos(lyr, _sirgas()))
        return self._indice[1]

    def _conectar(self, lyr, ligar):
        for nome in self.SINAIS:
            sinal = getattr(lyr, nome, None)
            if sinal is None:
                continue
            try:
                (sinal.connect if ligar else sinal.disconnect)(self.agendar)
            except (TypeError, RuntimeError):
                pass

    @staticmethod
    def _mensuravel(lyr):
        try:
            return isinstance(lyr, QgsVectorLayer) and lyr.isValid() and \
                lyr.geometryType() in (GEOM_POLIGONO, GEOM_LINHA)
        except RuntimeError:
            return False

    def _vigiar(self, camadas):
        """Liga editingStarted/Stopped das camadas: entrar ou sair de edição muda o alvo."""
        for lyr in camadas or []:
            if not self._mensuravel(lyr) or lyr.id() in self._vigiadas:
                continue
            fn = partial(self._entrou_edicao, lyr)
            lyr.editingStarted.connect(fn)
            lyr.editingStopped.connect(self.reavaliar)
            self._vigiadas[lyr.id()] = (lyr, fn)

    def _entrou_edicao(self, lyr):
        try:
            self._ultimo_editando = lyr.id()
        except RuntimeError:
            return
        self.reavaliar()

    def alvo(self):
        """Camada a medir: a ativa, se estiver em edição; senão a camada em edição (a
        última que entrou em edição); se nenhuma estiver em edição, a camada ativa."""
        ativa = self.iface.activeLayer()
        if self._mensuravel(ativa) and ativa.isEditable():
            return ativa
        editando = [c for c in QgsProject.instance().mapLayers().values()
                    if self._mensuravel(c) and c.isEditable()]
        if editando:
            pref = [c for c in editando if c.id() == self._ultimo_editando]
            return (pref or editando)[0]
        return ativa if self._mensuravel(ativa) else None

    def reavaliar(self, *_):
        novo = self.alvo()
        if novo is not self.camada:
            self.trocar_camada(novo)
        else:
            self.agendar()

    def trocar_camada(self, lyr):
        if self.camada is not None:
            try:
                self._conectar(self.camada, False)
            except RuntimeError:
                pass
        self.camada = lyr if self._mensuravel(lyr) else None
        if self.camada is not None:
            self._conectar(self.camada, True)
        self.agendar()

    def agendar(self, *_):
        if self.ativo:
            self.timer.start()

    def _medidor_src(self, lyr, geoms):
        prj = QgsProject.instance()
        escolha = self.src
        if escolha == "camada":
            crs = lyr.crs()
        elif escolha == "utm":
            crs = utm_automatico(lyr, geoms)
        else:
            crs = QgsCoordinateReferenceSystem(escolha)
            if not crs.isValid():
                crs = utm_automatico(lyr, geoms)
        poligono = lyr.geometryType() == GEOM_POLIGONO
        if crs.isGeographic():                 # SRC em graus: mede no elipsoide desse SRC
            da = QgsDistanceArea()
            da.setSourceCrs(lyr.crs(), prj.transformContext())
            da.setEllipsoid(crs.ellipsoidAcronym() or "GRS80")

            def medir(g):
                if poligono:
                    return da.measureArea(g), da.measurePerimeter(g)
                return None, da.measureLength(g)
            return medir, "%s (elipsoidal)" % crs.authid()
        tr = QgsCoordinateTransform(lyr.crs(), crs, prj)

        def medir(g):
            g = QgsGeometry(g)
            g.transform(tr)
            if poligono:
                return g.area(), _perimetro_plano(g)
            return None, g.length()
        rotulo = crs.authid()
        if escolha == "utm" and "zone" in crs.description():
            rotulo = "UTM %s" % crs.description().split("zone")[-1].strip()
        return medir, rotulo

    def _medidor_sgl(self, lyr):
        tr = QgsCoordinateTransform(lyr.crs(), _sirgas(), QgsProject.instance())
        poligono = lyr.geometryType() == GEOM_POLIGONO
        indice = self._indice_vertices()
        fonte = indice.z_em if indice is not None else None
        fixo = self.z_fixo        # a altitude fixa entra depois da média da feição

        def medir(g, sem_z=None):
            g = QgsGeometry(g)
            g.transform(tr)
            return medir_sgl(g, poligono, sem_z, fonte, fixo)
        return medir, indice

    def calcular(self):
        lyr = self.camada
        if lyr is None:
            return self.ao_calcular({"camada": None})
        try:
            nome = lyr.name()
        except RuntimeError:
            self.camada = None
            return self.ao_calcular({"camada": None})
        res = {"camada": nome, "editando": lyr.isEditable(),
               "poligono": lyr.geometryType() == GEOM_POLIGONO, "erro": None}
        if not lyr.crs().isValid():
            res["erro"] = "camada sem SRC definido"
            return self.ao_calcular(res)
        req = QgsFeatureRequest().setNoAttributes()
        feats = [(f.id(), f.geometry()) for f in lyr.getSelectedFeatures(req) if f.hasGeometry()]
        res["modo"] = "selecao" if feats else "total"
        if not feats and lyr.isEditable():
            # em edição: as feições criadas ou alteradas nesta edição, sem precisar selecionar
            buf = lyr.editBuffer()
            if buf is not None:
                ids = set(buf.addedFeatures().keys()) | set(buf.changedGeometries().keys())
                ids -= set(buf.deletedFeatureIds())
                if ids:
                    r2 = QgsFeatureRequest().setFilterFids(list(ids)).setNoAttributes()
                    feats = [(f.id(), f.geometry()) for f in lyr.getFeatures(r2) if f.hasGeometry()]
                    if feats:
                        res["modo"] = "editadas"
        res["n"] = len(feats)
        res["selecao"] = res["modo"] == "selecao"
        if not feats:
            res["n"] = lyr.featureCount()
            if res["n"] > LIMITE_TOTAL:
                res["erro"] = "camada grande: selecione as feições"
                return self.ao_calcular(res)
            feats = [(f.id(), f.geometry()) for f in lyr.getFeatures(req) if f.hasGeometry()]
        geoms = [g for _, g in feats]
        res["sem_z"] = []
        try:
            m_src, rot = self._medidor_src(lyr, geoms)
            m_sgl, indice = self._medidor_sgl(lyr)
            a = p = 0.0
            for g in geoms:
                x, y = m_src(g)
                a += x or 0.0
                p += y or 0.0
            res["src"] = (a / 10000.0, p / 1000.0)
            a = p = 0.0
            for fid, g in feats:
                cont = [0]
                x, y = m_sgl(g, cont)
                a += x or 0.0
                p += y or 0.0
                if cont[0]:
                    res["sem_z"].append(fid)
            res["sgl"] = (a / 10000.0, p / 1000.0)
            res["z_divergente"] = bool(indice is not None and indice.divergentes)
            res["camada_obj"] = lyr
            res["rotulo_src"] = rot
        except Exception as e:                  # transformação impossível etc.
            res["erro"] = str(e)[:60]
        self.ao_calcular(res)

    def encerrar(self):
        try:
            self.iface.currentLayerChanged.disconnect(self.reavaliar)
        except (TypeError, RuntimeError):
            pass
        try:
            QgsProject.instance().layersAdded.disconnect(self._vigiar)
        except (TypeError, RuntimeError):
            pass
        for lyr, fn in self._vigiadas.values():
            try:
                lyr.editingStarted.disconnect(fn)
                lyr.editingStopped.disconnect(self.reavaliar)
            except (TypeError, RuntimeError):
                pass
        self._vigiadas = {}
        if self.camada is not None:
            try:
                self._conectar(self.camada, False)
            except RuntimeError:
                pass


# ================================================================ caixinha
MONO = "font-family:Consolas,'DejaVu Sans Mono',monospace;font-weight:bold;"


class MiniMedidor(QFrame):
    """Caixinha compacta (tipo cronômetro) sobre o mapa; arrastável."""

    def __init__(self, iface):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.motor = Motor(iface, self.mostrar)
        self.aviso_z = AvisoZSessao(iface)
        self.ultimo = {}
        self.setObjectName("miniMedidor")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#miniMedidor{background:rgba(16,44,39,228);border-radius:10px;"
            "border:1px solid rgba(255,255,255,70)}"
            "QLabel{background:transparent;color:white}")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Arraste para mover • clique direito: escolher o SRC, copiar, fechar")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 8)
        lay.setSpacing(2)
        self.lb_cam = QLabel()                 # nome da camada medida, bem pequeno
        self.lb_cam.setStyleSheet("color:#8FB3AC;font-size:9px")
        lay.addWidget(self.lb_cam)
        self.lb_t = QLabel()
        self.lb_t.setStyleSheet("color:#BFD3CF;font-size:10px")
        lay.addWidget(self.lb_t)
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(0)
        self.cab_a, self.cab_p = QLabel("ha"), QLabel("km")
        for k, lb in enumerate((QLabel(""), self.cab_a, self.cab_p)):
            lb.setStyleSheet("color:#8FB3AC;font-size:10px")
            g.addWidget(lb, 0, k, Qt.AlignmentFlag.AlignRight)
        self.celulas = {}
        linhas = (("src", "#FFFFFF"), ("sgl", "#F5C04A"), ("dif", "#BFD3CF"))
        for linha, (chave, cor) in enumerate(linhas, 1):
            rot = QLabel()
            rot.setStyleSheet("color:%s;font-size:11px" % cor)
            a, p = QLabel(), QLabel()
            tam = 13 if chave == "dif" else 19
            for lb in (a, p):
                lb.setStyleSheet(MONO + "font-size:%dpx;color:%s" % (tam, cor))
            g.addWidget(rot, linha, 0, Qt.AlignmentFlag.AlignLeft)
            g.addWidget(a, linha, 1, Qt.AlignmentFlag.AlignRight)
            g.addWidget(p, linha, 2, Qt.AlignmentFlag.AlignRight)
            self.celulas[chave] = (rot, a, p)
        lay.addLayout(g)

        self._arrasto = None
        self._posicionado = False
        self.ao_fechar = None
        self.hide()

    # -------------------------------------------------------- exibição
    def showEvent(self, e):
        super().showEvent(e)
        if not self._posicionado:
            self._direita, self._topo = self.canvas.width() - 15, 15
            self._posicionado = True
        self.motor.ativo = True
        self.motor.trocar_camada(self.motor.alvo())
        self._encaixar()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.motor.ativo = False
        self.aviso_z.reiniciar()          # fechou: o aviso de z pode voltar na próxima vez
        if self.ao_fechar:
            self.ao_fechar()

    def _aviso_sessao(self, res):
        """Uma vez por sessão (até fechar o painel): lista as camadas sem z."""
        if self.aviso_z.mostrado or not (res.get("sem_z") or res.get("z_divergente")):
            return
        self.aviso_z.verificar([res.get("camada_obj"), self.motor.camada_vertices()])

    def _escrever(self, chave, rotulo, area, perim):
        rot, a, p = self.celulas[chave]
        rot.setText(rotulo)
        a.setText(area)
        p.setText(perim)

    def mostrar(self, res):
        self.ultimo = res
        try:
            self._aviso_sessao(res)
        except Exception:
            pass
        poligono = res.get("poligono", True)
        self.cab_a.setVisible(poligono)
        for _, a, _ in self.celulas.values():
            a.setVisible(poligono)
        if not res.get("camada"):
            self.lb_cam.setText("")
            self.lb_cam.setVisible(False)
            self.lb_t.setText("Ative uma camada de polígonos ou linhas")
            self._vazio()
        else:
            modo = res.get("modo")
            if modo == "selecao":
                qual = "%d selecionada(s)" % res["n"]
            elif modo == "editadas":
                qual = "%d editada(s)" % res["n"]
            else:
                qual = "total (%d)" % res.get("n", 0)
            ed = " <span style='color:#FF6B6B'>● editando</span>" if res.get("editando") else ""
            nome = str(res["camada"])
            curto = nome if len(nome) <= 42 else nome[:40] + "…"
            self.lb_cam.setText("camada: " + curto)
            self.lb_cam.setToolTip(nome)
            self.lb_cam.setVisible(True)
            self.lb_t.setText("%s%s" % (qual, ed))
            if res.get("erro"):
                self._vazio()
                self.celulas["src"][0].setText(res["erro"])
            else:
                (a1, p1), (a2, p2) = res["src"], res["sgl"]
                da = a1 - a2
                pct = (da / a2 * 100.0) if a2 else None
                dif_a = fmt(da, 4, True)
                if poligono and pct is not None:
                    dif_a = "%s (%s%%)" % (dif_a, fmt(pct, 2, True))
                self._escrever("src", res["rotulo_src"], fmt(a1, 4), fmt(p1, 3))
                self._escrever("sgl", "SGL", fmt(a2, 4), fmt(p2, 3))
                self._escrever("dif", "diferença", dif_a, fmt(p1 - p2, 3, True))
        self._encaixar()

    def _vazio(self):
        for chave, rotulo in (("src", "SRC"), ("sgl", "SGL"), ("dif", "diferença")):
            self._escrever(chave, rotulo, "–", "–")

    def _encaixar(self):
        for rot, a, p in self.celulas.values():      # labels recalculam a largura do texto
            for lb in (rot, a, p):
                lb.setMinimumWidth(lb.sizeHint().width())
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()
        if not self._posicionado:
            return
        x = min(max(0, self._direita - self.width()), max(0, self.canvas.width() - self.width()))
        y = min(max(0, self._topo), max(0, self.canvas.height() - self.height()))
        self.move(x, y)

    # -------------------------------------------------------- arrastar / menu
    @staticmethod
    def _global(e):
        return e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._arrasto = self._global(e) - self.pos()
        elif e.button() == Qt.MouseButton.RightButton:
            self._menu(self._global(e))

    def mouseMoveEvent(self, e):
        if self._arrasto is not None:
            p = self._global(e) - self._arrasto
            x = min(max(0, p.x()), max(0, self.canvas.width() - self.width()))
            y = min(max(0, p.y()), max(0, self.canvas.height() - self.height()))
            self.move(x, y)
            self._direita, self._topo = x + self.width(), y

    def mouseReleaseEvent(self, e):
        self._arrasto = None

    def _menu(self, pos):
        m = QMenu(self)
        atual = self.motor.src
        for rot, valor in (("SIRGAS 2000 / UTM – fuso automático", "utm"),
                           ("SRC da camada", "camada")):
            a = m.addAction(rot)
            a.setCheckable(True)
            a.setChecked(atual == valor)
            a.triggered.connect(lambda _=False, v=valor: setattr(self.motor, "src", v))
        outro = m.addAction("Escolher outro SRC…" if atual in ("utm", "camada")
                            else "Outro SRC: %s…" % atual)
        outro.setCheckable(True)
        outro.setChecked(atual not in ("utm", "camada"))
        outro.triggered.connect(self._escolher_src)
        m.addSeparator()
        mz = m.addMenu("Altitude (z) para o SGL")
        info = mz.addAction("Ordem: Z da geometria → camada de vértices → média da feição → "
                            "altitude fixa → 0")
        info.setEnabled(False)
        cv = self.motor.camada_vertices()
        av = mz.addAction("Buscar z na camada de vértices: %s" % cv.name() if cv is not None
                          else "Buscar z na camada de vértices…")
        av.setCheckable(True)
        av.setChecked(cv is not None)
        av.triggered.connect(self._escolher_vertices)
        zf = self.motor.z_fixo
        af = mz.addAction(("Altitude fixa: %s m" % fmt(zf, 2)) if zf is not None else "Altitude fixa…")
        af.setCheckable(True)
        af.setChecked(zf is not None)
        af.triggered.connect(self._escolher_fixo)
        m.addSeparator()
        m.addAction("Copiar valores", self._copiar)
        m.addAction("Fechar", self.hide)
        m.exec(pos)

    def _escolher_vertices(self, marcado):
        if not marcado:
            self.motor.z_vertices = ""
            return
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle(NOME)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Camada de vértices (pontos) com o z – campo <b>z</b> ou Z da "
                             "geometria (vale o campo).<br>Cada vértice do polígono usa o z do "
                             "ponto coincidente (até %s cm)." % fmt(TOL_M * 100, 0)))
        cb = QgsMapLayerComboBox()
        cb.setFilters(FILTRO_CAMADA_PONTO)
        atual = self.motor.camada_vertices()
        if atual is not None:
            cb.setLayer(atual)
        else:
            for lyr in QgsProject.instance().mapLayersByName("vertice"):
                cb.setLayer(lyr)
                break
        lay.addWidget(cb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() and cb.currentLayer() is not None:
            self.motor.z_vertices = cb.currentLayer().id()

    def _escolher_fixo(self, marcado):
        if not marcado:
            self.motor.z_fixo = None
            return
        atual = self.motor.z_fixo
        valor, ok = QInputDialog.getDouble(
            self.iface.mainWindow(), NOME,
            "Altitude usada quando NENHUM vértice da feição tem z (m).\nSe alguns tiverem, "
            "os que faltam usam a média deles:", atual if atual is not None else 0.0,
            -500.0, 9000.0, 2)
        if ok:
            self.motor.z_fixo = valor

    def _escolher_src(self):
        dlg = QgsProjectionSelectionDialog(self.iface.mainWindow())
        atual = self.motor.src
        if atual not in ("utm", "camada"):
            dlg.setCrs(QgsCoordinateReferenceSystem(atual))
        elif self.motor.camada is not None:
            dlg.setCrs(self.motor.camada.crs())
        if dlg.exec():
            crs = dlg.crs()
            if crs.isValid():
                self.motor.src = crs.authid()

    def _copiar(self):
        r = self.ultimo
        if not r.get("camada") or r.get("erro") or "src" not in r:
            return
        qual = ("%d selecionada(s)" % r["n"]) if r.get("selecao") else ("camada inteira (%d)" % r["n"])
        linhas = ["%s – %s" % (r["camada"], qual)]
        for rot, (a, p) in ((r["rotulo_src"], r["src"]), ("SGL", r["sgl"])):
            area = ("%s ha; " % fmt(a, 4)) if r["poligono"] else ""
            linhas.append("%s: %s%s km" % (rot, area, fmt(p, 3)))
        QApplication.clipboard().setText("\n".join(linhas))
        self.iface.messageBar().pushInfo(NOME, "Valores copiados.")

    def encerrar(self):
        self.motor.encerrar()


# ================================================================ menu e Caixa de Ferramentas
MENU_PLUGIN = "&Restaure"
_PAINEL = None                # PainelMedidas ativo (só existe com a interface do QGIS)


class PainelMedidas:
    """Menu Complementos › Restaure › Ferramentas úteis e a
    caixinha sobre o mapa."""

    def __init__(self, iface):
        self.iface = iface
        self.menu = None
        self.acao = None
        self.mini = None

    def initGui(self):
        global _PAINEL
        mw = self.iface.mainWindow()
        self.acao = QAction(QIcon(ICONE), NOME, mw)
        self.acao.setToolTip("Área (ha) e perímetro (km) em tempo real – SRC escolhido x SGL")
        self.acao.setCheckable(True)
        self.acao.triggered.connect(self.alternar)
        self.menu = QMenu("Ferramentas úteis", mw)
        self.menu.addAction(self.acao)
        self.iface.addPluginToMenu(MENU_PLUGIN, self.menu.menuAction())
        _PAINEL = self

    def alternar(self, ligado):
        if self.mini is None:
            self.mini = MiniMedidor(self.iface)
            self.mini.ao_fechar = lambda: self.acao.setChecked(False)
        self.mini.setVisible(ligado)
        if ligado:
            self.mini.raise_()

    def abrir(self):
        self.acao.setChecked(True)
        self.alternar(True)

    def unload(self):
        global _PAINEL
        _PAINEL = None
        if self.mini is not None:
            self.mini.ao_fechar = None
            self.mini.encerrar()
            self.mini.hide()
            self.mini.deleteLater()
            self.mini = None
        if self.menu is not None:
            self.iface.removePluginMenu(MENU_PLUGIN, self.menu.menuAction())


class MedidasAoVivo(FerramentaInterativa):
    """Entrada na Caixa de Ferramentas: abre o painel em vez do formulário padrão."""

    def name(self):
        return "medidas_ao_vivo"

    def displayName(self):
        return NOME

    def group(self):
        return "3. Ferramentas úteis"

    def groupId(self):
        return "ferramentas_uteis"

    def icon(self):
        return QIcon(ICONE)

    def shortHelpString(self):
        url_logo = QUrl.fromLocalFile(
            os.path.join(PASTA_PLUGIN, "docs", "images", "icon.png")).toString()
        return (
            f'<p><img src="{url_logo}" width="96" height="96"></p>'
            "<p>Caixinha flutuante sobre o mapa com área (ha) e perímetro (km) em tempo real: "
            "SRC escolhido x SGL (como o SIGEF) e a diferença. Escolhe sozinha o que medir: com "
            "uma camada em edição, as feições criadas ou alteradas nessa edição; sem edição, o "
            "total da camada ativa; com seleção, a seleção.</p>"
            "<p>Altitude de cada vértice para o SGL: Z da geometria → camada de vértices (vale a "
            "coluna z; ponto coincidente até 1 cm) → média da feição → altitude fixa → 0. Um "
            "aviso único por sessão lista as camadas sem z, com opção de preencher ou ignorar. "
            "Nada é salvo automaticamente.</p>"
            "<p>Arraste para mover; clique direito para escolher o SRC, a camada de vértices, "
            "copiar ou fechar. Também em Complementos › Restaure › Ferramentas úteis.</p>"
            "<p>Elaborado por Dra. Ranieli dos Anjos de Souza</p>"
            "<p>Contato para treinamento: restaurear@gmail.com</p>"
            '<p><a href="https://wa.me/5569993512657">Fale conosco</a></p>'
        )

    MENSAGEM_SEM_INTERFACE = "O painel de medidas ao vivo só abre com a interface do QGIS."

    def alvo(self):
        return _PAINEL.abrir if _PAINEL is not None else None

    def createInstance(self):
        return MedidasAoVivo()
