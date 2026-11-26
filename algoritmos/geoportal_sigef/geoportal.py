"""Consulta ao novo Geoportal do INCRA (GeoNode/GeoServer, WFS público, sem login)."""
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtNetwork import QNetworkRequest
from . import cache
from qgis.core import (QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem, QgsFeedback,
                       QgsGeometry, QgsRectangle, QgsSettings, QgsVectorLayer)

# servidores conhecidos do Geoportal (o primeiro que responder é usado e lembrado)
SERVIDORES = [
    "https://geonodeincra.serpro.gov.br/geoserver/ows",
    "https://geoportal.incra.gov.br/geoserver/ows",
]
CRS = "EPSG:4674"            # SIRGAS 2000 geográfico
CAMPO_PADRAO = "parcela_codigo"
LOTE_CODIGOS = 40            # códigos por consulta
MAX_AREA = 20000             # parcelas por consulta de área
PAUSA_S = 0.5                # pausa curta entre consultas
CHAVE = "restaure_sigef/geoportal_"


class ErroGeoportal(Exception):
    pass


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _sem_erro(codigo):
    try:
        ok = QgsBlockingNetworkRequest.ErrorCode.NoError
    except AttributeError:
        ok = QgsBlockingNetworkRequest.NoError
    return codigo == ok


class Cancelado(Exception):
    """O usuário clicou em Cancelar durante uma consulta."""


# funções da janela: manter a interface respondendo e saber se foi cancelado
_ganchos = {"tick": QApplication.processEvents, "cancelado": lambda: False}


def _baixar(url, params):
    """Baixa a URL fora da thread da interface: o QGIS continua respondendo
    enquanto o Geoportal demora, e o botão Cancelar interrompe a consulta."""
    completo = url + ("&" if "?" in url else "?") + urlencode(params)
    req = QNetworkRequest(QUrl.fromEncoded(completo.encode("ascii")))
    req.setRawHeader(b"User-Agent", b"QGIS DataBase SIGEF (Restaure)")
    feedback = QgsFeedback()
    resp = {}

    def trabalho():
        try:
            rede = QgsBlockingNetworkRequest()
            resp["erro"] = rede.get(req, True, feedback)
            resp["dados"] = bytes(rede.reply().content())
            resp["msg"] = rede.errorMessage()
        except Exception as e:  # nunca deixa a thread morrer sem resposta
            resp["excecao"] = e

    fio = threading.Thread(target=trabalho, daemon=True)
    fio.start()
    while fio.is_alive():
        _ganchos["tick"]()
        if _ganchos["cancelado"]() and not feedback.isCanceled():
            feedback.cancel()
        fio.join(0.05)
    if feedback.isCanceled():
        raise Cancelado()
    if "excecao" in resp:
        raise ErroGeoportal(str(resp["excecao"]) or "falha de conexão com o Geoportal")
    dados = resp.get("dados", b"")
    if not _sem_erro(resp.get("erro")) and not dados:
        raise ErroGeoportal(resp.get("msg") or "falha de conexão com o Geoportal")
    return dados


def _verificar(dados, contexto):
    inicio = dados[:4000]
    if b"ServiceException" in inicio or b"ExceptionReport" in inicio:
        texto = re.sub(r"<[^>]+>", " ", dados[:3000].decode("utf-8", "ignore"))
        raise ErroGeoportal("o Geoportal recusou a consulta ({}): {}".format(
            contexto, " ".join(texto.split())[:300]))
    if b"<html" in inicio.lower():
        m = re.search(rb"<title>(.*?)</title>", inicio, re.I | re.S)
        titulo = m.group(1).decode("utf-8", "ignore").strip() if m else "página HTML"
        raise ErroGeoportal("o Geoportal devolveu uma página em vez de dados ({}): {}"
                            .format(contexto, titulo))


# ---------- descoberta do servidor e da camada ----------
def _camadas_wfs(url):
    dados = _baixar(url, {"service": "WFS", "version": "1.0.0", "request": "GetCapabilities"})
    _verificar(dados, "lista de camadas")
    try:
        raiz = ET.fromstring(dados)
    except ET.ParseError:
        raise ErroGeoportal("resposta ilegível ao listar as camadas")
    camadas = []
    for el in raiz.iter():
        if el.tag.split("}")[-1] != "FeatureType":
            continue
        nome = titulo = ""
        for ch in el:
            t = ch.tag.split("}")[-1]
            if t == "Name":
                nome = (ch.text or "").strip()
            elif t == "Title":
                titulo = (ch.text or "").strip()
        if nome:
            camadas.append((nome, titulo))
    return camadas


def _escolher_camada(camadas):
    """Procura a camada 'Sigef - todas parcelas'."""
    for regra in (
            lambda n, t: "todas parcelas" in _norm(t),
            lambda n, t: "todas parcelas" in _norm(n),
            lambda n, t: "sigef" in _norm(t + " " + n) and "parcela" in _norm(t + " " + n)):
        for nome, titulo in camadas:
            if regra(nome, titulo):
                return nome, titulo
    return None, None


def conectar(log):
    """Devolve (url, camada, campo_codigo), descobrindo e guardando na primeira vez."""
    s = QgsSettings()
    url, camada = s.value(CHAVE + "url", ""), s.value(CHAVE + "camada", "")
    campo = s.value(CHAVE + "campo", CAMPO_PADRAO)
    if url and camada:
        return url, camada, campo
    erros = []
    for url in SERVIDORES:
        try:
            nome, titulo = _escolher_camada(_camadas_wfs(url))
        except ErroGeoportal as e:
            erros.append(str(e))
            continue
        if nome:
            log("Geoportal do INCRA: camada '{}' ({}).".format(titulo or nome, nome))
            s.setValue(CHAVE + "url", url)
            s.setValue(CHAVE + "camada", nome)
            s.setValue(CHAVE + "campo", CAMPO_PADRAO)
            return url, nome, CAMPO_PADRAO
        erros.append("camada de parcelas do SIGEF não encontrada em " + url)
    raise ErroGeoportal("não consegui conectar ao Geoportal do INCRA. " + "; ".join(erros))


def esquecer_conexao():
    s = QgsSettings()
    for k in ("url", "camada", "campo"):
        s.remove(CHAVE + k)


# ---------- leitura ----------
def _campo_codigo(fields, preferido):
    nomes = {f.name().lower(): f.name() for f in fields}
    if preferido.lower() in nomes:
        return nomes[preferido.lower()]
    for n_low, n in nomes.items():
        if "parcela" in n_low and "cod" in n_low:
            return n
    return None


def _ler(dados, contexto, pasta, salvar=True):
    _verificar(dados, contexto)
    caminho = os.path.join(pasta, "geoportal_{}.geojson".format(uuid.uuid4().hex[:8]))
    with open(caminho, "wb") as f:
        f.write(dados)
    cam = QgsVectorLayer(caminho, "geoportal", "ogr")
    if not cam.isValid():
        if b'"features"' in dados[:2000] and b'"features":[]' in dados.replace(b" ", b"")[:4000]:
            return None
        raise ErroGeoportal("não consegui ler a resposta do Geoportal ({})".format(contexto))
    if salvar:  # atualiza a base local; falha no cache nunca atrapalha a consulta
        try:
            campo = _campo_codigo(cam.fields(), QgsSettings().value(CHAVE + "campo", CAMPO_PADRAO))
            if campo:
                cache.salvar(dados, campo)
        except Exception:
            pass
    return cam


def _registros(cam, campo_pref):
    campo = _campo_codigo(cam.fields(), campo_pref)
    crs = cam.crs() if cam.crs().isValid() else QgsCoordinateReferenceSystem(CRS)
    nomes = [f.name() for f in cam.fields()]
    regs = []
    for ft in cam.getFeatures():
        if not ft.hasGeometry():
            continue
        cod = str(ft[campo] or "").strip().lower() if campo else ""
        if not cod or cod == "null":
            cod = "sem_codigo_{}".format(ft.id())
        regs.append({
            "codigo": cod,
            "geom": QgsGeometry(ft.geometry()),
            "attrs": dict(zip(nomes, ft.attributes())),
            "fields": cam.fields(),
            "crs": crs,
            "origem": "Geoportal INCRA",
        })
    return regs


def _consulta(url, camada, extra, contexto, pasta):
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature",
              "typeName": camada, "outputFormat": "application/json", "srsName": CRS}
    params.update(extra)
    cam = _ler(_baixar(url, params), contexto, pasta)
    return cam


def _descobrir_campo(url, camada, pasta):
    """Lê uma parcela para descobrir o nome do campo do código."""
    cam = _consulta(url, camada, {"maxFeatures": "1"}, "estrutura da camada", pasta)
    campo = _campo_codigo(cam.fields(), CAMPO_PADRAO) if cam is not None else None
    if not campo:
        raise ErroGeoportal("não encontrei o campo do código da parcela na camada")
    QgsSettings().setValue(CHAVE + "campo", campo)
    return campo


def _buscar_codigos_online(codigos, log, cancelado):
    url, camada, campo = conectar(log)
    pasta = tempfile.mkdtemp(prefix="database_sigef_geoportal_")
    try:  # confere o campo do código com uma consulta pequena
        _consulta(url, camada, {"CQL_FILTER": "{} IN ('{}')".format(campo, codigos[0]),
                                "maxFeatures": "1"}, "teste do campo", pasta)
    except ErroGeoportal:
        novo = _descobrir_campo(url, camada, pasta)
        if novo == campo:
            raise
        campo = novo
    regs = []
    for i in range(0, len(codigos), LOTE_CODIGOS):
        if cancelado():
            break
        bloco = codigos[i:i + LOTE_CODIGOS]
        filtro = "{} IN ({})".format(campo, ",".join("'{}'".format(c) for c in bloco))
        cam = _consulta(url, camada, {"CQL_FILTER": filtro,
                                      "maxFeatures": str(len(bloco) * 10)},
                        "busca por código", pasta)
        if cam is not None:
            regs += _registros(cam, campo)
        if len(codigos) > LOTE_CODIGOS:
            log("  {} de {} códigos consultados…".format(min(i + LOTE_CODIGOS, len(codigos)),
                                                         len(codigos)))
        time.sleep(PAUSA_S)
    alvo = set(codigos)
    return [r for r in regs if r["codigo"] in alvo]


def _buscar_area_online(area, log, cancelado):
    """Todas as parcelas que tocam o retângulo (lon/lat SIRGAS 2000)."""
    url, camada, campo = conectar(log)
    if cancelado():
        return []
    pasta = tempfile.mkdtemp(prefix="database_sigef_geoportal_")
    bbox = "{:.8f},{:.8f},{:.8f},{:.8f}".format(
        area.xMinimum(), area.yMinimum(), area.xMaximum(), area.yMaximum())
    cam = _consulta(url, camada, {"bbox": bbox, "maxFeatures": str(MAX_AREA)},
                    "busca por área", pasta)
    if cam is None:
        return []
    if cam.featureCount() >= MAX_AREA:
        log("A área tem {} parcelas ou mais; só as primeiras vieram. Reduza a área."
            .format(MAX_AREA), True)
    return _registros(cam, campo)


# ---------- base salva (cache) quando o Geoportal não responde ----------
_estado = {"offline": False, "avisado": False}


def usou_base_salva():
    """True se, na última busca, o Geoportal não respondeu e valeu a base salva."""
    return _estado["offline"]


def _registros_salvos(colecao, datas, motivo, log, extra=""):
    """Transforma o GeoJSON do cache em registros, avisando a data da base no log."""
    if not colecao:
        return None
    pasta = tempfile.mkdtemp(prefix="database_sigef_cache_")
    cam = _ler(colecao, "base salva", pasta, salvar=False)
    if cam is None:
        return None
    regs = _registros(cam, QgsSettings().value(CHAVE + "campo", CAMPO_PADRAO))
    quando = cache.descrever_datas(datas)
    for r in regs:
        r["origem"] = "Geoportal INCRA (base salva de {})".format(quando)
    _estado["offline"] = True
    if not _estado["avisado"]:
        _estado["avisado"] = True
        log("⚠ Geoportal indisponível ({}). Usei a <b>base salva de {}</b>.{}"
            .format(motivo, quando, extra), True)
    return regs


def buscar_codigos(codigos, log, cancelado):
    _estado["offline"] = _estado["avisado"] = False
    _ganchos["cancelado"] = cancelado
    try:
        return _buscar_codigos_online(codigos, log, cancelado)
    except Cancelado:
        return []
    except ErroGeoportal as e:
        colecao, datas = cache.por_codigos(codigos)
        regs = _registros_salvos(colecao, datas, str(e)[:160], log)
        if regs is None:
            raise ErroGeoportal("{} Essas parcelas também não estão na base salva."
                                .format(e))
        alvo = set(codigos)
        return [r for r in regs if r["codigo"] in alvo]


def buscar_area(area, log, cancelado, reiniciar=True, log_base=None):
    """Parcelas que tocam o retângulo; sem Geoportal, usa a base salva."""
    if reiniciar:
        _estado["offline"] = _estado["avisado"] = False
    log_base = log_base or log
    extra = (" Ela só tem parcelas já consultadas antes, então pode faltar alguma "
             "da área.")

    def salvas(motivo):
        colecao, datas = cache.por_area(area.xMinimum(), area.yMinimum(),
                                        area.xMaximum(), area.yMaximum(), MAX_AREA)
        return _registros_salvos(colecao, datas, motivo, log_base, extra)

    _ganchos["cancelado"] = cancelado
    if _estado["offline"]:  # já sabemos que está fora do ar: nem tenta a rede
        return salvas("") or []
    try:
        return _buscar_area_online(area, log, cancelado)
    except Cancelado:
        return []
    except ErroGeoportal as e:
        regs = salvas(str(e)[:160])
        if regs is None:
            raise ErroGeoportal("{} Também não há parcelas dessa área na base salva."
                                .format(e))
        return regs


def buscar_vizinhas(regs, log, cancelado):
    """Parcelas em volta das encontradas, só para descobrir os confrontantes."""
    if not regs:
        return []
    margem = 0.002  # ~200 m
    total = QgsRectangle()
    total.setMinimal()
    for r in regs:
        total.combineExtentWith(r["geom"].boundingBox())
    if total.width() * total.height() <= 0.25:
        areas = [total]
    else:  # parcelas espalhadas: uma consulta por parcela
        areas = [r["geom"].boundingBox() for r in regs]
    vizinhas = {}
    for a in areas:
        if cancelado():
            break
        a = QgsRectangle(a)
        a.grow(margem)
        for r in buscar_area(a, lambda *x: None, cancelado, reiniciar=False, log_base=log):
            vizinhas.setdefault(r["codigo"], r)
        time.sleep(PAUSA_S)
    return list(vizinhas.values())


# ---------- carga em massa da base salva ----------
BRASIL = QgsRectangle(-74.1, -34.0, -28.8, 5.4)   # lon/lat SIRGAS 2000
LADO_INICIAL = 2.0          # graus; quadrantes cheios são divididos em 4
LADO_MINIMO = 0.01          # ~1 km: abaixo disso não divide mais
LIMITE_QUADRANTE = 5000     # parcelas por consulta
RETOMAR_HORAS = 24          # quadrantes baixados há menos que isso são pulados
FALHAS_SEGUIDAS = 5


def _quadrantes(area, lado):
    x0, y0, x1, y1 = area.xMinimum(), area.yMinimum(), area.xMaximum(), area.yMaximum()
    nx = max(1, int(math.ceil((x1 - x0) / lado - 1e-9)))
    ny = max(1, int(math.ceil((y1 - y0) / lado - 1e-9)))
    return [(x0 + i * lado, y0 + j * lado,
             min(x0 + (i + 1) * lado, x1), min(y0 + (j + 1) * lado, y1))
            for j in range(ny) for i in range(nx)]


def _dividir(q):
    xm, ym = (q[0] + q[2]) / 2, (q[1] + q[3]) / 2
    return [(q[0], q[1], xm, ym), (xm, q[1], q[2], ym),
            (q[0], ym, xm, q[3]), (xm, ym, q[2], q[3])]


def _baixar_quadrante(url, camada, campo, q):
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature",
              "typeName": camada, "outputFormat": "application/json", "srsName": CRS,
              "bbox": "{:.8f},{:.8f},{:.8f},{:.8f}".format(*q),
              "maxFeatures": str(LIMITE_QUADRANTE)}
    dados = _baixar(url, params)
    _verificar(dados, "carga da base salva")
    try:
        return cache.salvar(dados, campo)
    except ValueError:
        raise ErroGeoportal("resposta ilegível do Geoportal (carga da base salva)")


def baixar_regiao(area, log, cancelado, tick=lambda: None):
    """Baixa todas as parcelas de um retângulo (lon/lat) para a base salva, em
    quadrantes que se dividem sozinhos onde há muitas parcelas. Pode ser
    interrompida e repetida: quadrantes baixados nas últimas 24 h são pulados."""
    _ganchos["cancelado"] = cancelado
    try:
        url, camada, campo = conectar(log)
    except Cancelado:
        return {"feitos": 0, "pulados": 0, "falhas": 0, "parcelas": 0, "restantes": 0}
    pilha = list(reversed(_quadrantes(area, LADO_INICIAL)))
    feitos = pulados = falhas = parcelas = seguidas = 0
    while pilha:
        tick()
        if cancelado():
            break
        q = pilha.pop()
        chave = "{:.5f},{:.5f},{:.5f},{:.5f}".format(*q)
        if cache.quadrante_recente(chave, RETOMAR_HORAS):
            pulados += 1
            continue
        n = None
        for tentativa in range(1, 4):
            try:
                n = _baixar_quadrante(url, camada, campo, q)
                seguidas = 0
                break
            except Cancelado:
                break
            except ErroGeoportal as e:
                erro = e
                time.sleep(2 * tentativa)
                tick()
        if n is None and cancelado():
            pilha.append(q)  # não terminou: continua na fila
            break
        if n is None:
            falhas += 1
            seguidas += 1
            log("  quadrante {} falhou: {}".format(chave, str(erro)[:140]), True)
            if seguidas >= FALHAS_SEGUIDAS:
                raise ErroGeoportal("o Geoportal parou de responder; repita depois para "
                                    "continuar de onde parou.")
            continue
        parcelas += n
        if n >= LIMITE_QUADRANTE and (q[2] - q[0]) > LADO_MINIMO:
            pilha.extend(reversed(_dividir(q)))
        else:
            if n >= LIMITE_QUADRANTE:
                log("  quadrante {} tem {}+ parcelas e não dá para dividir mais; "
                    "pode faltar alguma.".format(chave, LIMITE_QUADRANTE), True)
            cache.marcar_quadrante(chave)
        feitos += 1
        if feitos % 10 == 0:
            log("  {} quadrantes baixados, {} na fila · {} parcelas recebidas".format(
                feitos, len(pilha), "{:,}".format(parcelas).replace(",", ".")))
        time.sleep(PAUSA_S)
    return {"feitos": feitos, "pulados": pulados, "falhas": falhas,
            "parcelas": parcelas, "restantes": len(pilha)}
