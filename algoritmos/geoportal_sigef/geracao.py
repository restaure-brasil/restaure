"""Gera as camadas Parcelas, Vértices e Limites a partir dos polígonos encontrados."""
import os

from qgis.core import (QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsField, QgsFields, QgsGeometry,
                       QgsPointXY, QgsProject,
                       QgsRectangle, QgsSpatialIndex,
                       QgsVectorFileWriter, QgsVectorLayer)

from ...compat import TIPO_DECIMAL as T_DBL, TIPO_TEXTO as T_STR

NOMES_CAMADA = {"vertice": "Vértices", "limite": "Limites (Geoportal)",
                "parcela": "Parcelas (Geoportal)"}
A_DEFINIR = "Limite a definir"
NOMES_GPKG = {"vertice": "vertices", "limite": "limites", "parcela": "parcelas"}
CAMPOS_NOME = ("nome_area", "denominacao", "nome", "denominaca")


def _curto(cod):
    return cod[:8] if len(cod) == 36 and cod.count("-") == 4 else cod


def _nova(tipo_geom, nome, crs, campos):
    cam = QgsVectorLayer(tipo_geom, nome, "memory")
    cam.setCrs(crs)
    cam.dataProvider().addAttributes(campos)
    cam.updateFields()
    return cam


def _aneis(geom):
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    for poly in polys:
        for anel in poly:
            pts = list(anel)
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            if len(pts) >= 3:
                yield pts


def _nome_parcela(reg):
    attrs = {k.lower(): v for k, v in reg["attrs"].items()}
    for c in CAMPOS_NOME:
        v = attrs.get(c)
        if v not in (None, "") and str(v) != "NULL":
            return str(v)
    return reg["codigo"]


def _camada_parcelas(regs, crs):
    campos = QgsFields()
    usados = set()
    for n in ("parcela_co", "origem"):
        campos.append(QgsField(n, T_STR))
        usados.add(n)
    for r in regs:
        for f in r["fields"]:
            n = f.name().lower()
            if n in usados or n in ("fid", "gml_id"):
                continue
            campos.append(QgsField(f))
            usados.add(n)
    cam = _nova("MultiPolygon", NOMES_CAMADA["parcela"], crs, campos)
    feicoes = []
    for r in regs:
        ft = QgsFeature(cam.fields())
        g = QgsGeometry(r["geom"])
        g.convertToMultiType()
        ft.setGeometry(g)
        attrs = {k.lower(): v for k, v in r["attrs"].items()}
        for i, f in enumerate(cam.fields()):
            if f.name().lower() in attrs:
                ft.setAttribute(i, attrs[f.name().lower()])
        ft["parcela_co"] = r["codigo"]
        ft["origem"] = r["origem"]
        feicoes.append(ft)
    cam.dataProvider().addFeatures(feicoes)
    return cam


def _dist_seg(p, a, b):
    """Distância de p ao segmento ab e posição (0..1) da projeção."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return ((p.x() - a.x()) ** 2 + (p.y() - a.y()) ** 2) ** 0.5, 0.0
    t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / L2
    tc = min(1.0, max(0.0, t))
    qx, qy = a.x() + tc * dx, a.y() + tc * dy
    return ((p.x() - qx) ** 2 + (p.y() - qy) ** 2) ** 0.5, t


def _camada_limites(regs, crs, vizinhos=None, tol_m=3.0):
    """Limites de cada parcela, divididos onde muda o confrontante.
    O confrontante é a parcela vizinha que encosta no trecho; sem vizinha
    cadastrada, o trecho fica como 'Limite a definir'."""
    campos = QgsFields()
    for n, t in (("parcela_co", T_STR), ("parcela", T_STR), ("confrontante", T_STR),
                 ("confr_codigo", T_STR), ("situacao", T_STR), ("comprim_m", T_DBL)):
        campos.append(QgsField(n, t))
    cam = _nova("LineString", NOMES_CAMADA["limite"], crs, campos)

    da = QgsDistanceArea()
    da.setSourceCrs(crs, QgsProject.instance().transformContext())
    da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7019")
    # tolerância de encosto entre parcelas (levantamentos diferentes raramente
    # coincidem ao centímetro): metros convertidos para graus no SIRGAS 2000
    tol = tol_m / 111320.0 if crs.isGeographic() else tol_m

    # todas as parcelas conhecidas (importadas + vizinhas), sem repetir
    todas = {}
    for r in list(regs) + list(vizinhos or []):
        todas.setdefault(r["codigo"], r)
    lista = list(todas.values())
    indice = QgsSpatialIndex()
    bordas, verts = {}, {}
    for i, r in enumerate(lista):
        f = QgsFeature()
        f.setId(i)
        f.setGeometry(r["geom"])
        indice.addFeature(f)
        bordas[i] = QgsGeometry(r["geom"].constGet().boundary())
        verts[i] = [p for anel in _aneis(r["geom"]) for p in anel]

    feicoes = []
    for r in regs:
        nome = _nome_parcela(r)
        for pts in _aneis(r["geom"]):
            trechos = []  # (ponto_a, ponto_b, nome_viz, cod_viz)
            for k in range(len(pts)):
                a, b = pts[k], pts[(k + 1) % len(pts)]
                ret = QgsRectangle(a, b)
                ret.grow(tol)
                cand = [j for j in indice.intersects(ret) if lista[j]["codigo"] != r["codigo"]]
                # quebra o lado nos vértices das vizinhas que caem sobre ele
                cortes = {0.0, 1.0}
                for j in cand:
                    for p in verts[j]:
                        d, t = _dist_seg(p, a, b)
                        if d <= tol and 0.0 < t < 1.0:
                            cortes.add(t)
                cortes = sorted(cortes)
                for t0, t1 in zip(cortes, cortes[1:]):
                    if t1 - t0 < 1e-9:
                        continue
                    p0 = QgsPointXY(a.x() + t0 * (b.x() - a.x()), a.y() + t0 * (b.y() - a.y()))
                    p1 = QgsPointXY(a.x() + t1 * (b.x() - a.x()), a.y() + t1 * (b.y() - a.y()))
                    meio = QgsGeometry.fromPointXY(
                        QgsPointXY((p0.x() + p1.x()) / 2, (p0.y() + p1.y()) / 2))
                    # vizinha que encosta no trecho ou que se sobrepõe a ele
                    viz = [j for j in cand if bordas[j].distance(meio) <= tol
                           or lista[j]["geom"].contains(meio)]
                    nomes_v = sorted({_nome_parcela(lista[j]) for j in viz})
                    cods_v = sorted({lista[j]["codigo"] for j in viz})
                    trechos.append((p0, p1, "; ".join(nomes_v), "; ".join(cods_v)))
            if not trechos:
                continue
            # junta trechos seguidos com o mesmo confrontante
            grupos = []
            for p0, p1, nv, cv in trechos:
                if grupos and grupos[-1]["k"] == (nv, cv):
                    grupos[-1]["pts"].append(p1)
                else:
                    grupos.append({"k": (nv, cv), "pts": [p0, p1]})
            if len(grupos) > 1 and grupos[0]["k"] == grupos[-1]["k"]:
                ult = grupos.pop()
                grupos[0]["pts"] = ult["pts"] + grupos[0]["pts"][1:]
            for g in grupos:
                nv, cv = g["k"]
                geom = QgsGeometry.fromPolylineXY(g["pts"])
                ft = QgsFeature(cam.fields())
                ft.setGeometry(geom)
                ft.setAttributes([r["codigo"], nome, nv or A_DEFINIR, cv or None,
                                  "definido" if nv else "a definir",
                                  round(da.measureLength(geom), 2)])
                feicoes.append(ft)
    cam.dataProvider().addFeatures(feicoes)
    return cam


def gerar_camadas(regs, tipos, vizinhos=None, tol_m=3.0):
    if not regs:
        return {}
    crs = regs[0]["crs"]
    ctx = QgsProject.instance().transformContext()
    unicos = {}
    for r in regs:
        if r["crs"] != crs:
            g = QgsGeometry(r["geom"])
            g.transform(QgsCoordinateTransform(r["crs"], crs, ctx))
            r["geom"] = g
        unicos.setdefault(r["codigo"], r)
    regs = list(unicos.values())
    saida = {}
    if "parcela" in tipos:
        saida["parcela"] = _camada_parcelas(regs, crs)
    if "limite" in tipos:
        viz = []
        for v in vizinhos or []:
            if v["crs"] != crs:
                g = QgsGeometry(v["geom"])
                g.transform(QgsCoordinateTransform(v["crs"], crs, ctx))
                v = dict(v, geom=g)
            viz.append(v)
        saida["limite"] = _camada_limites(regs, crs, viz, tol_m)
    return saida


def _acao(nome):
    try:
        return getattr(QgsVectorFileWriter.ActionOnExistingFile, nome)
    except AttributeError:
        return getattr(QgsVectorFileWriter, nome)


def _sem_erro_escrita(codigo):
    try:
        ok = QgsVectorFileWriter.WriterError.NoError
    except AttributeError:
        ok = QgsVectorFileWriter.NoError
    return codigo == ok


def salvar_gpkg(camadas, caminho, log):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    ctx = QgsProject.instance().transformContext()
    salvas = {}
    for tipo, cam in camadas.items():
        op = QgsVectorFileWriter.SaveVectorOptions()
        op.driverName = "GPKG"
        op.fileEncoding = "UTF-8"
        op.layerName = NOMES_GPKG[tipo]
        op.actionOnExistingFile = _acao(
            "CreateOrOverwriteLayer" if os.path.exists(caminho) else "CreateOrOverwriteFile")
        if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):      # QGIS 3.20+
            res = QgsVectorFileWriter.writeAsVectorFormatV3(cam, caminho, ctx, op)
        else:
            res = QgsVectorFileWriter.writeAsVectorFormatV2(cam, caminho, ctx, op)
        if _sem_erro_escrita(res[0]):
            nova = QgsVectorLayer("{}|layername={}".format(caminho, op.layerName), cam.name(), "ogr")
            salvas[tipo] = nova if nova.isValid() else cam
        else:
            log("Não salvei '{}' no GeoPackage ({}); ficou como camada temporária."
                .format(cam.name(), res[1]), True)
            salvas[tipo] = cam
    return salvas


def _campo_existente(cam, opcoes):
    nomes = {f.name().lower(): f.name() for f in cam.fields()}
    for o in opcoes:
        if o in nomes:
            return nomes[o]
    return None


def _campo_por_valor(cam, padrao):
    """Primeiro campo cujo primeiro valor preenchido casa com o padrão."""
    import re
    rx = re.compile(padrao, re.I)
    for f in cam.fields():
        for ft in cam.getFeatures():
            v = str(ft[f.name()] or "").strip()
            if v and v != "NULL":
                if rx.match(v):
                    return f.name()
                break
    return None


def _estilizar(tipo, cam, origem):
    from . import estilo
    try:
        if tipo == "vertice":
            campo = "vertice"
            if origem == "sigef":
                campo = _campo_por_valor(cam, r"^[A-Z0-9]+-[A-Z]-\d+$") or \
                    _campo_existente(cam, ("vertice", "codigo")) or "parcela_co"
            estilo.vertices(cam, campo)
        elif tipo == "limite":
            if origem == "ods":
                estilo.limites(cam, "\"confrontan\" || '\\n' || \"tipo\"", "tipo")
            elif origem == "sigef":
                tipo_c = _campo_por_valor(cam, r"^L[AN]\d+$")
                conf = next((f.name() for f in cam.fields()
                             if "confr" in f.name().lower() or "descr" in f.name().lower()), None)
                partes = ['coalesce("{}", \'\')'.format(c) for c in (conf, tipo_c) if c]
                rot = " || '\\n' || ".join(partes) if partes else "''"
                estilo.limites(cam, rot, tipo_c)
            else:
                estilo.limites_confrontacao(cam)
        else:
            if origem == "ods":
                estilo.parcelas(cam, 'coalesce("denominacao", "nome")')
            else:
                c = _campo_existente(cam, ("nome_area", "denominacao", "denominaca",
                                           "nome", "parcela_co", "parcela_codigo"))
                rot = '"{}"'.format(c) if c else "''"
                # cor por status (semitransparente); sem campo de status, estilo padrão
                if not estilo.parcelas_por_status(cam, rot):
                    estilo.parcelas(cam, rot)
    except Exception:
        pass


def _acao_sigef(cam):
    """Ação 'Abrir no SIGEF' nas feições com código de parcela."""
    from qgis.core import QgsAction
    campo = _campo_existente(cam, ("parcela_co", "qrcode", "parcela_codigo"))
    if not campo:
        return
    try:
        tipo = QgsAction.ActionType.OpenUrl
    except AttributeError:
        try:
            from qgis.core import Qgis
            tipo = Qgis.AttributeActionType.OpenUrl
        except AttributeError:
            tipo = QgsAction.OpenUrl
    acao = QgsAction(tipo, "Abrir no SIGEF",
                     "https://sigef.incra.gov.br/geo/parcela/detalhe/[% \"{}\" %]/".format(campo))
    acao.setActionScopes({"Feature", "Canvas"})
    cam.actions().addAction(acao)


def adicionar_ao_projeto(camadas, nome_grupo, origem="geoportal"):
    projeto = QgsProject.instance()
    raiz = projeto.layerTreeRoot()
    grupo = raiz.insertGroup(0, nome_grupo)
    adicionadas = []
    for tipo in ("vertice", "limite", "parcela"):
        cam = camadas.get(tipo)
        if cam is None:
            continue
        _estilizar(tipo, cam, origem)
        try:
            _acao_sigef(cam)
        except Exception:
            pass
        projeto.addMapLayer(cam, False)
        grupo.addLayer(cam)
        adicionadas.append(cam)
    return adicionadas
