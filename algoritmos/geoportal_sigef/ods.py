"""Leitura das planilhas ODS do SIGEF: uma, várias ou centenas dentro de ZIPs."""
import io
import os
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDistanceArea, QgsFeature, QgsField, QgsGeometry, QgsLineString, QgsMultiPolygon, QgsPoint,
                       QgsPointXY, QgsPolygon, QgsProject, QgsVectorLayer)

from ...compat import TIPO_DECIMAL as T_DBL, TIPO_INTEIRO as T_INT, TIPO_TEXTO as T_STR

NS = {"t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
      "f": "urn:oasis:names:tc:opendocument:xmlns:form:1.0",
      "o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0"}


def _q(p, n):
    return "{%s}%s" % (NS[p], n)


class ErroOds(Exception):
    pass


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower().strip()


# ---------- leitura das células ----------
def _texto_p(el):
    partes = [el.text or ""]
    for ch in el:
        tag = ch.tag.split("}")[-1]
        if tag == "s":
            partes.append(" " * int(ch.get(_q("x", "c"), "1")))
        elif tag in ("tab", "line-break"):
            partes.append(" ")
        else:
            partes.append(_texto_p(ch))
        partes.append(ch.tail or "")
    return "".join(partes)


def _valor(cel):
    v = cel.get(_q("o", "value"))  # número com precisão total
    if v is not None:
        return v.strip()
    textos = (_texto_p(p).strip() for p in cel.findall(_q("x", "p")))
    return " ".join(t for t in textos if t)


def _celulas(linha, expandir=False, maximo=16):
    out = []
    for c in linha.findall(_q("t", "table-cell")):
        v = _valor(c)
        n = int(c.get(_q("t", "number-columns-repeated"), "1")) if expandir else 1
        out.extend([v] * max(0, min(n, maximo - len(out))))
        if len(out) >= maximo:
            break
    return out


# ---------- números e coordenadas ----------
def _num(s, padrao=0.0):
    """Número de uma célula, aceitando vírgula, unidade ou texto junto (ex.: '0,012 m')."""
    s = (s or "").strip()
    try:
        return float(s.replace(",", "."))
    except ValueError:
        m = re.search(r"-?\d+(?:[.,]\d+)?", s)
        return float(m.group().replace(",", ".")) if m else padrao


def _dms(s):
    s = (s or "").strip()
    try:
        return float(s.replace(",", "."))
    except ValueError:
        pass
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", s)]
    if not nums:
        raise ValueError(s)
    nums += [0.0] * (3 - len(nums))
    v = nums[0] + nums[1] / 60 + nums[2] / 3600
    if re.search(r"[SWO]\s*$", s.upper()) or s.startswith("-"):
        v = -v
    return v


def _crs(src, mc, hemisf):
    if "geogr" in _norm(src):
        return QgsCoordinateReferenceSystem("EPSG:4674")
    m = re.search(r"\d+", mc or "")
    if not m:
        raise ErroOds("meridiano central não encontrado")
    fuso = round((183 - abs(int(m.group()))) / 6)
    norte = _norm(hemisf).startswith("n")
    return QgsCoordinateReferenceSystem("EPSG:%d" % ((31954 if norte else 31960) + fuso))


# ---------- abas ----------
def _meridiano(raiz):
    lb = raiz.find(".//f:listbox[@f:name='lstMeridianos']", NS)
    if lb is None:
        return None
    for op in lb.findall("f:option", NS):
        if op.get(_q("f", "current-selected")) == "true":
            return op.get(_q("f", "label"))
    return None


_REGRAS = [
    ("denominacao", lambda r: "denomina" in r),
    ("nat_serv", lambda r: "natureza" in r and "servi" in r),
    ("natureza", lambda r: "natureza" in r),
    ("situacao", lambda r: "situa" in r),
    ("cpf_cnpj", lambda r: "cpf" in r or "cnpj" in r),
    ("sncr", lambda r: "sncr" in r or ("codigo" in r and "imovel" in r)),
    ("cns", lambda r: "cns" in r or "cartorio" in r),
    ("matricula", lambda r: "matricula" in r),
    ("municipio", lambda r: "municipio" in r),
    ("pessoa", lambda r: "pessoa" in r),
    ("nome", lambda r: r.startswith("nome")),
]


def _identificacao(tab):
    ident = {}
    for linha in tab.findall(_q("t", "table-row")):
        cel = [c for c in _celulas(linha, expandir=True, maximo=10) if c]
        if len(cel) < 2:
            continue
        rot = _norm(cel[0])
        for chave, teste in _REGRAS:
            if teste(rot):
                if chave != "cpf_cnpj" or re.search(r"\d", cel[1]):
                    ident.setdefault(chave, cel[1])
                break
    return ident


def _perimetro(tab, aba, mc, log, arquivo):
    cab, linhas = {}, []
    for i, linha in enumerate(tab.findall(_q("t", "table-row"))):
        if i <= 10:
            cel = _celulas(linha)
            def g(c, cel=cel):
                return cel[c] if len(cel) > c else ""
            if i == 2:
                cab["nome"] = g(1)
            elif i == 4:
                cab["lado"] = g(1)
            elif i == 8:
                cab["src"], cab["mc"], cab["hemisf"] = g(1), g(3), g(5)
            continue
        cel = _celulas(linha, expandir=True)
        if cel and cel[0]:
            linhas.append((cel + [""] * 12)[:12])
    if not linhas:
        return None
    crs = _crs(cab.get("src", ""), mc or cab.get("mc"), cab.get("hemisf", ""))
    geo = crs.isGeographic()
    vertices = []
    for c in linhas:
        try:
            x = _dms(c[1]) if geo else float(c[1].replace(",", "."))
            y = _dms(c[3]) if geo else float(c[3].replace(",", "."))
        except ValueError:
            if "-" in c[0]:
                log("  ⚠ {} ({}): vértice {} ignorado, coordenada ilegível"
                    .format(arquivo, aba, c[0]), True)
            continue
        vertices.append({
            "codigo": c[0], "x": x, "y": y, "z": _num(c[5], None),
            "sx": _num(c[2], None), "sy": _num(c[4], None), "sz": _num(c[6], None),
            "metodo": c[7], "tipo": c[8], "cns": c[9], "matricula": c[10],
            "confrontante": c[11]})
    if len(vertices) < 3:
        return None
    if all(v["sx"] is None and v["z"] is None for v in vertices):
        log("  ⚠ {} ({}): sigmas e altitude não encontrados. Primeira linha lida: {}"
            .format(arquivo, aba, " | ".join(linhas[0])), True)
    nome = cab.get("nome", "")
    return {"arquivo": arquivo, "aba": "{} - {}".format(aba, nome) if nome else aba,
            "crs": crs, "vertices": vertices}


def ler_ods(dados, arquivo, log):
    try:
        z = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        raise ErroOds("não é uma planilha ODS válida")
    with z:
        try:
            if b"encryption-data" in z.read("META-INF/manifest.xml"):
                raise ErroOds("planilha com senha de abertura")
        except KeyError:
            pass
        try:
            raiz = ET.fromstring(z.read("content.xml"))
        except (KeyError, ET.ParseError):
            raise ErroOds("conteúdo ilegível")
    mc = _meridiano(raiz)
    ident, perimetros = {}, []
    for tab in raiz.iter(_q("t", "table")):
        aba = tab.get(_q("t", "name"), "")
        if "identifica" in _norm(aba):
            ident = _identificacao(tab)
        elif "perimetro" in _norm(aba):
            p = _perimetro(tab, aba, mc, log, arquivo)
            if p:
                perimetros.append(p)
    if not perimetros:
        raise ErroOds("nenhum perímetro com vértices (fora do modelo SIGEF?)")
    for p in perimetros:
        p["ident"] = ident
    return perimetros


# ---------- coleta de arquivos ----------
def _ods_de_zip(dados, saida, nivel=0):
    with zipfile.ZipFile(io.BytesIO(dados)) as z:
        for m in z.namelist():
            base = os.path.basename(m)
            if not base or m.startswith("__MACOSX") or base.startswith((".~lock", "~$")):
                continue
            if base.lower().endswith(".ods"):
                saida.append((base, z.read(m)))
            elif base.lower().endswith(".zip") and nivel < 2:
                _ods_de_zip(z.read(m), saida, nivel + 1)


def coletar(caminhos, log, cancelado):
    planilhas = []
    for c in caminhos:
        try:
            with open(c, "rb") as f:
                dados = f.read()
            if c.lower().endswith(".zip"):
                antes = len(planilhas)
                _ods_de_zip(dados, planilhas)
                log("{}: {} planilha(s)".format(os.path.basename(c), len(planilhas) - antes))
            else:
                planilhas.append((os.path.basename(c), dados))
        except (OSError, zipfile.BadZipFile) as e:
            log("✘ {}: {}".format(os.path.basename(c), e), True)
    perimetros, erros = [], 0
    for i, (nome, dados) in enumerate(planilhas, 1):
        if cancelado():
            break
        try:
            perimetros += ler_ods(dados, nome, log)
        except ErroOds as e:
            erros += 1
            log("✘ {}: {}".format(nome, e), True)
        if i % 20 == 0:
            log("  {} de {} planilhas lidas…".format(i, len(planilhas)))
    log("Lidas {} planilha(s): {} perímetro(s), {} com erro.".format(
        len(planilhas), len(perimetros), erros))
    return perimetros


# ---------- camadas ----------
def _nova(tipo, nome, crs, campos):
    cam = QgsVectorLayer(tipo, nome, "memory")
    cam.setCrs(crs)
    cam.dataProvider().addAttributes([QgsField(n, t) for n, t in campos])
    cam.updateFields()
    return cam


def gerar_camadas(perimetros, tipos, log):
    crs = perimetros[0]["crs"]
    ctx = QgsProject.instance().transformContext()
    tol = 1e-7 if crs.isGeographic() else 0.01
    da = QgsDistanceArea()
    da.setSourceCrs(crs, ctx)
    da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7019")

    v_cam = _nova("PointZ", "Vértices ODS", crs, [
        ("vertice", T_STR), ("tipo_verti", T_STR), ("metodo_pos", T_STR),
        ("coord_x", T_DBL), ("coord_y", T_DBL), ("altitude", T_DBL),
        ("sigma_x", T_DBL), ("sigma_y", T_DBL), ("sigma_z", T_DBL),
        ("arquivos", T_STR), ("conflito", T_STR)])
    l_cam = _nova("LineStringZ", "Limites ODS", crs, [
        ("tipo", T_STR), ("confrontan", T_STR), ("cns", T_STR), ("matricula", T_STR),
        ("de", T_STR), ("para", T_STR), ("comprim_m", T_DBL),
        ("parcela", T_STR), ("arquivo", T_STR)])
    p_cam = _nova("MultiPolygonZ", "Parcelas ODS", crs, [
        ("arquivo", T_STR), ("perimetro", T_STR), ("nome", T_STR),
        ("denominacao", T_STR), ("cpf_cnpj", T_STR), ("pessoa", T_STR),
        ("nat_serv", T_STR), ("natureza", T_STR), ("situacao", T_STR),
        ("sncr", T_STR), ("cns", T_STR), ("matricula", T_STR),
        ("municipio", T_STR), ("area_ha", T_DBL), ("n_vertices", T_INT)])

    vert = {}
    lims, pars = [], []
    for p in perimetros:
        tr = None if p["crs"] == crs else QgsCoordinateTransform(p["crs"], crs, ctx)
        idt = p["ident"]
        rotulo = idt.get("denominacao") or idt.get("nome") or p["arquivo"]
        pts, vs = [], p["vertices"]
        for v in vs:
            x, y = v["x"], v["y"]
            if tr:
                q = tr.transform(QgsPointXY(x, y))
                x, y = q.x(), q.y()
            pts.append(QgsPoint(x, y, v["z"] or 0.0))
            entradas = vert.setdefault(v["codigo"], [])
            for e in entradas:
                if abs(e["x"] - x) <= tol and abs(e["y"] - y) <= tol:
                    e["arquivos"].add(p["arquivo"])
                    break
            else:
                entradas.append({"x": x, "y": y, "z": v["z"], "v": v,
                                 "arquivos": {p["arquivo"]}})
        n = len(pts)

        # limites: junta lados seguidos com o mesmo tipo/confrontante
        def chave(i, vs=vs):
            return (vs[i]["tipo"], vs[i]["cns"], vs[i]["matricula"], vs[i]["confrontante"])
        grupos = []
        for i in range(n):
            j = (i + 1) % n
            if grupos and grupos[-1]["k"] == chave(i):
                grupos[-1]["pts"].append(pts[j])
                grupos[-1]["para"] = vs[j]["codigo"]
            else:
                grupos.append({"k": chave(i), "pts": [pts[i], pts[j]],
                               "de": vs[i]["codigo"], "para": vs[j]["codigo"]})
        if len(grupos) > 1 and grupos[0]["k"] == grupos[-1]["k"]:
            g = grupos.pop()
            grupos[0]["pts"] = g["pts"] + grupos[0]["pts"][1:]
            grupos[0]["de"] = g["de"]
        for g in grupos:
            ft = QgsFeature(l_cam.fields())
            geom = QgsGeometry(QgsLineString(g["pts"]))
            ft.setGeometry(geom)
            tipo, cns, mat, conf = g["k"]
            ft.setAttributes([tipo, conf, cns, mat, g["de"], g["para"],
                              round(da.measureLength(geom), 3), rotulo, p["arquivo"]])
            lims.append(ft)

        # parcela
        mp = QgsMultiPolygon()
        mp.addGeometry(QgsPolygon(QgsLineString(pts + [pts[0]])))
        geom = QgsGeometry(mp)
        ft = QgsFeature(p_cam.fields())
        ft.setGeometry(geom)
        ft.setAttributes([p["arquivo"], p["aba"], idt.get("nome"), idt.get("denominacao"),
                          idt.get("cpf_cnpj"), idt.get("pessoa"), idt.get("nat_serv"),
                          idt.get("natureza"), idt.get("situacao"), idt.get("sncr"),
                          idt.get("cns"), idt.get("matricula"), idt.get("municipio"),
                          round(da.measureArea(geom) / 10000, 4), n])
        pars.append(ft)

    vfs, conflitos = [], 0
    for cod, entradas in vert.items():
        conflito = "sim" if len(entradas) > 1 else ""
        if conflito:
            conflitos += 1
            log("⚠ Vértice {} com coordenadas diferentes em: {}".format(
                cod, " × ".join(", ".join(sorted(e["arquivos"])) for e in entradas)), True)
        for e in entradas:
            v = e["v"]
            ft = QgsFeature(v_cam.fields())
            ft.setGeometry(QgsGeometry(QgsPoint(e["x"], e["y"], e["z"] or 0.0)))
            partes = cod.strip().split("-")
            tipo_v = partes[-2].strip().upper() if len(partes) >= 3 else ""
            ft.setAttributes([cod, tipo_v, v["metodo"],
                              round(e["x"], 8 if crs.isGeographic() else 3),
                              round(e["y"], 8 if crs.isGeographic() else 3), e["z"],
                              v["sx"], v["sy"], v["sz"],
                              ", ".join(sorted(e["arquivos"]))[:254], conflito])
            vfs.append(ft)
    if conflitos:
        log("{} vértice(s) com coordenadas diferentes entre planilhas "
            "(campo conflito = sim).".format(conflitos), True)

    v_cam.dataProvider().addFeatures(vfs)
    l_cam.dataProvider().addFeatures(lims)
    p_cam.dataProvider().addFeatures(pars)
    todas = {"vertice": v_cam, "limite": l_cam, "parcela": p_cam}
    return {t: c for t, c in todas.items() if t in tipos}
