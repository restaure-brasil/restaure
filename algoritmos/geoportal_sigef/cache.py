"""Base local (cache) das parcelas consultadas no Geoportal do INCRA.

Cada consulta que dá certo grava (ou atualiza) as parcelas recebidas num banco
SQLite no perfil do QGIS. Se o Geoportal estiver fora do ar, o plugin lê daqui
e informa no log a data em que cada parcela foi baixada.
"""
import json
import os
import sqlite3
import time
import zlib
from contextlib import closing

from qgis.core import QgsApplication

LOTE_SQL = 500


def caminho():
    pasta = os.path.join(QgsApplication.qgisSettingsDirPath(), "restaure_sigef")
    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, "cache_geoportal.sqlite")


def _abrir():
    con = sqlite3.connect(caminho(), timeout=10)
    con.execute(
        "CREATE TABLE IF NOT EXISTS parcelas ("
        "codigo TEXT PRIMARY KEY, xmin REAL, ymin REAL, xmax REAL, ymax REAL, "
        "atualizado TEXT NOT NULL, crs TEXT, feicao BLOB NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS quadrantes ("
                "chave TEXT PRIMARY KEY, atualizado TEXT NOT NULL)")
    return con


def _caixa(geom):
    """Envelope (xmin, ymin, xmax, ymax) de uma geometria GeoJSON."""
    xs, ys = [], []
    pilha = [geom.get("coordinates") if isinstance(geom, dict) else None]
    while pilha:
        c = pilha.pop()
        if not isinstance(c, (list, tuple)) or not c:
            continue
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            pilha.extend(c)
    if not xs:
        return (None, None, None, None)
    return (min(xs), min(ys), max(xs), max(ys))


def _chave_codigo(props, campo):
    """Nome do campo do código nas propriedades (tolera maiúsculas e variações)."""
    if campo in props:
        return campo
    for k in props:
        if k.lower() == str(campo).lower():
            return k
    for k in props:
        if "parcela" in k.lower() and "cod" in k.lower():
            return k
    return campo


def salvar(dados, campo_codigo):
    """Grava/atualiza no cache as feições de uma resposta GeoJSON do Geoportal.
    Devolve quantas feições a resposta tinha."""
    resposta = json.loads(dados.decode("utf-8", "ignore"))
    feicoes = resposta.get("features") or []
    crs = json.dumps(resposta["crs"]) if resposta.get("crs") else None
    agora = time.strftime("%Y-%m-%dT%H:%M:%S")
    chave = _chave_codigo((feicoes[0].get("properties") or {}) if feicoes else {},
                          campo_codigo)
    linhas = []
    for f in feicoes:
        props = f.get("properties") or {}
        cod = str(props.get(chave) or "").strip().lower()
        if not cod or cod == "null":
            continue
        xmin, ymin, xmax, ymax = _caixa(f.get("geometry"))
        bruto = json.dumps(f, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        linhas.append((cod, zlib.compress(bruto, 6), crs, xmin, ymin, xmax, ymax, agora))
    if linhas:
        with closing(_abrir()) as con:
            with con:
                con.executemany(
                    "INSERT OR REPLACE INTO parcelas "
                    "(codigo, feicao, crs, xmin, ymin, xmax, ymax, atualizado) "
                    "VALUES (?,?,?,?,?,?,?,?)", linhas)
                con.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES (?,?)",
                            ("ultima_gravacao", agora))
    return len(feicoes)


def _colecao(linhas):
    """(GeoJSON em bytes, lista de datas) a partir de linhas (feicao, crs, atualizado)."""
    if not linhas:
        return None, []
    crs = next((c for _, c, _ in linhas if c), None)
    texto = '{"type":"FeatureCollection"'
    if crs:
        texto += ',"crs":' + crs
    feicoes = [zlib.decompress(f).decode("utf-8") if isinstance(f, (bytes, bytearray))
               else f for f, _, _ in linhas]
    texto += ',"features":[' + ",".join(feicoes) + "]}"
    return texto.encode("utf-8"), [a for _, _, a in linhas]


def por_codigos(codigos):
    """Parcelas do cache pelos códigos. Devolve (GeoJSON ou None, datas)."""
    linhas = []
    try:
        with closing(_abrir()) as con:
            for i in range(0, len(codigos), LOTE_SQL):
                bloco = [c.lower() for c in codigos[i:i + LOTE_SQL]]
                sql = ("SELECT feicao, crs, atualizado FROM parcelas WHERE codigo IN ({})"
                       .format(",".join("?" * len(bloco))))
                linhas += con.execute(sql, bloco).fetchall()
    except sqlite3.Error:
        return None, []
    return _colecao(linhas)


def por_area(xmin, ymin, xmax, ymax, limite):
    """Parcelas do cache cujo envelope toca o retângulo."""
    try:
        with closing(_abrir()) as con:
            linhas = con.execute(
                "SELECT feicao, crs, atualizado FROM parcelas "
                "WHERE xmax >= ? AND xmin <= ? AND ymax >= ? AND ymin <= ? LIMIT ?",
                (xmin, xmax, ymin, ymax, limite)).fetchall()
    except sqlite3.Error:
        return None, []
    return _colecao(linhas)


def _formatar(iso):
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return iso


def descrever_datas(datas):
    """'22/11/2026 14:30' ou 'de 20/11/2026 09:10 a 22/11/2026 14:30'."""
    if not datas:
        return "data desconhecida"
    menor, maior = _formatar(min(datas)), _formatar(max(datas))
    return menor if menor == maior else "{} a {}".format(menor, maior)


def estatisticas():
    """(quantidade de parcelas, data da última gravação). Não varre a tabela inteira."""
    try:
        with closing(_abrir()) as con:
            n = con.execute("SELECT COUNT(*) FROM parcelas").fetchone()[0]
            r = con.execute("SELECT valor FROM meta WHERE chave = 'ultima_gravacao'").fetchone()
        return n, (r[0] if r else None)
    except sqlite3.Error:
        return 0, None


def quadrante_recente(chave, horas):
    """True se o quadrante foi baixado por completo nas últimas `horas` horas."""
    limite = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - horas * 3600))
    try:
        with closing(_abrir()) as con:
            return con.execute("SELECT 1 FROM quadrantes WHERE chave = ? AND atualizado >= ?",
                               (chave, limite)).fetchone() is not None
    except sqlite3.Error:
        return False


def marcar_quadrante(chave):
    with closing(_abrir()) as con:
        with con:
            con.execute("INSERT OR REPLACE INTO quadrantes (chave, atualizado) VALUES (?,?)",
                        (chave, time.strftime("%Y-%m-%dT%H:%M:%S")))


def meta_get(chave):
    try:
        with closing(_abrir()) as con:
            r = con.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def meta_set(chave, valor):
    try:
        with closing(_abrir()) as con:
            with con:
                con.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES (?,?)",
                            (chave, valor))
    except sqlite3.Error:
        pass


def agora():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def dias_desde(iso):
    """Dias (com fração) desde uma data gravada pelo cache; None se ilegível."""
    try:
        return (time.time() - time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (TypeError, ValueError):
        return None


def formatar(iso):
    return _formatar(iso)
