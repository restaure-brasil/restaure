"""Estilo Restaure: vértices âmbar, limites por confrontante em tons de terra
e verde, parcelas verde-claro."""
import unicodedata

from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsRendererCategory, QgsRuleBasedRenderer,
                       QgsSingleSymbolRenderer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayerSimpleLabeling)


# paleta própria do Restaure para os limites
PALETA = ["#1b7f5c", "#b5651d", "#2e86ab", "#8e44ad", "#c0392b", "#d4a017",
          "#16a085", "#6d4c41", "#3949ab", "#e67e22", "#558b2f", "#ad1457"]


# escala a partir da qual os rótulos aparecem (ex.: 1:2.500 ou mais perto)
ESCALA_VERTICES = 2500
ESCALA_LIMITES = 10000


def _ao_redor_do_ponto():
    try:
        from qgis.core import Qgis
        return Qgis.LabelPlacement.OrderedPositionsAroundPoint
    except AttributeError:
        return QgsPalLayerSettings.OrderedPositionsAroundPoint


def _rotulo(cam, expr, tamanho=9, cor="#1e3d2f", contorno="white", buffer=1.0,
            escala=0, ponto=False):
    s = QgsPalLayerSettings()
    s.fieldName = expr
    s.isExpression = True
    s.enabled = True
    if escala:
        # só mostra o rótulo com o mapa mais aproximado que essa escala
        s.scaleVisibility = True
        s.minimumScale = escala
        s.maximumScale = 0
    if ponto:
        # rótulo ao lado do vértice, afastado do símbolo
        s.placement = _ao_redor_do_ponto()
        s.dist = 1.2
    fmt = QgsTextFormat()
    fonte = QFont("Arial", int(tamanho))
    fonte.setBold(True)
    fmt.setFont(fonte)
    fmt.setSize(float(tamanho))
    fmt.setColor(QColor(cor))
    b = QgsTextBufferSettings()
    b.setEnabled(True)
    b.setSize(float(buffer))
    b.setColor(QColor(contorno))
    fmt.setBuffer(b)
    s.setFormat(fmt)
    cam.setLabeling(QgsVectorLayerSimpleLabeling(s))
    cam.setLabelsEnabled(True)


COR_VERTICE = "#f2b705"

# tipo do vértice pelo código do SIGEF (XXXX-M-0000): marco, ponto, virtual
TIPOS_VERTICE = [
    ("M", "Marco (M)", "square", "#f2b705", "3.0"),
    ("P", "Ponto (P)", "circle", "#2e86ab", "2.2"),
    ("V", "Virtual (V)", "circle", "#ecf0f1", "2.0"),
]


def _marcador(cor, tamanho="2.2", contorno="#1e3d2f", largura="0.4", forma="circle"):
    return QgsMarkerSymbol.createSimple({
        "name": forma, "color": cor, "outline_color": contorno,
        "outline_style": "solid", "outline_width": largura, "size": tamanho,
        "size_unit": "MM"})


def vertices(cam, campo="vertice"):
    tem_conflito = cam.fields().indexOf("conflito") >= 0
    tem_tipo = cam.fields().indexOf("tipo_verti") >= 0
    if not tem_conflito and not tem_tipo:
        cam.setRenderer(QgsSingleSymbolRenderer(_marcador(COR_VERTICE)))
    else:
        raiz = QgsRuleBasedRenderer.Rule(None)
        sem_conflito = ""
        if tem_conflito:
            # coordenadas diferentes entre planilhas: vermelho, maior e por cima
            alerta = _marcador("#e00000", "3.6", "white", "0.6")
            alerta.symbolLayer(0).setRenderingPass(2)
            raiz.appendChild(QgsRuleBasedRenderer.Rule(
                alerta, 0, 0, "\"conflito\" = 'sim'", "Conflito de coordenadas"))
            sem_conflito = "coalesce(\"conflito\", '') <> 'sim' AND "
        if tem_tipo:
            for letra, nome, forma, cor, tamanho in TIPOS_VERTICE:
                sym = _marcador(cor, tamanho, forma=forma)
                if letra == "M":
                    sym.symbolLayer(0).setRenderingPass(1)  # marcos por cima
                raiz.appendChild(QgsRuleBasedRenderer.Rule(
                    sym, 0, 0,
                    "{}upper(trim(\"tipo_verti\")) = '{}'".format(sem_conflito, letra), nome))
        raiz.appendChild(QgsRuleBasedRenderer.Rule(
            _marcador("#95a5a6", "2.0"), 0, 0, "", "Outros", "", True))
        rend = QgsRuleBasedRenderer(raiz)
        rend.setUsingSymbolLevels(True)
        cam.setRenderer(rend)
    _rotulo(cam, '"{}"'.format(campo), 8, escala=ESCALA_VERTICES, ponto=True)


# cor fixa por tipo de limite do SIGEF: artificiais (LA) em tons quentes,
# naturais (LN) em azuis e verdes
CORES_TIPO = {
    "LA1": "#b5651d", "LA2": "#e67e22", "LA3": "#c0392b", "LA4": "#8e44ad",
    "LA5": "#d4a017", "LA6": "#6d4c41", "LA7": "#ad1457",
    "LN1": "#2e86ab", "LN2": "#16a085", "LN3": "#1b7f5c", "LN4": "#3949ab",
    "LN5": "#558b2f", "LN6": "#00838f",
}


def _linha(cor):
    sym = QgsLineSymbol.createSimple({"width": "0.6", "width_unit": "MM"})
    sym.setColor(QColor(cor))
    return sym


def limites(cam, expr_rotulo, campo_tipo=None):
    """Com campo de tipo (LA1, LN1…), uma cor por tipo; sem ele, cor única."""
    if campo_tipo and cam.fields().indexOf(campo_tipo) >= 0:
        valores = set()
        for f in cam.getFeatures():
            v = f[campo_tipo]
            valores.add("" if v is None or str(v) == "NULL" else str(v).strip().upper())
        cats = []
        for v in sorted(valores):
            cats.append(QgsRendererCategory(v, _linha(CORES_TIPO.get(v, "#7f8c8d")),
                                            v or "Sem tipo"))
        cam.setRenderer(QgsCategorizedSymbolRenderer(
            'upper(trim("{}"))'.format(campo_tipo), cats))
    else:
        cam.setRenderer(QgsSingleSymbolRenderer(_linha("#1b5e3a")))
    _rotulo(cam, expr_rotulo, 8, escala=ESCALA_LIMITES)


def limites_confrontacao(cam):
    """Limites do Geoportal: com confrontante em azul (diferente do verde das
    parcelas), a definir em laranja tracejado."""
    definido = QgsLineSymbol.createSimple({"width": "0.8", "width_unit": "MM"})
    definido.setColor(QColor("#00a2e8"))
    a_definir = QgsLineSymbol.createSimple({"width": "0.7", "width_unit": "MM",
                                            "line_style": "dash"})
    a_definir.setColor(QColor("#e67e22"))
    cats = [QgsRendererCategory("definido", definido, "Com confrontante"),
            QgsRendererCategory("a definir", a_definir, "Limite a definir")]
    cam.setRenderer(QgsCategorizedSymbolRenderer("situacao", cats))
    _rotulo(cam, '"confrontante"', 8, cor="#0b4f75", escala=ESCALA_LIMITES)


def parcelas(cam, expr_rotulo):
    sym = QgsFillSymbol.createSimple({
        "color": "168,213,186,90", "outline_color": "#1b5e3a",
        "outline_width": "0.5", "outline_width_unit": "MM"})
    cam.setRenderer(QgsSingleSymbolRenderer(sym))
    _rotulo(cam, expr_rotulo, 10, "#1b5e3a", "white", 1.2)


# ---------- parcelas coloridas por status ----------
# (trecho normalizado do valor, ex.: TITULACAO, EM TITULACAO, APROVACAO_FISCAL, ANALISE_FISCAL, nome, cor, significado)
STATUS_PARCELA = [
    ("TITULA", "EM TITULAÇÃO", "#f39c12", "posse com geo aprovado"),
    ("REGISTRAD", "REGISTRADA", "#2e86ab", "área com geo registrado na matrícula"),
    ("CERTIFICAD", "CERTIFICADA", "#27ae60", "área titulada com geo aprovado"),
    ("FISCAL", "ANALISE_FISCAL", "#e74c3c", "em fiscalização"),
]
ALFA_STATUS = 100   # 0-255: preenchimento meio transparente
COR_STATUS_OUTRO = "#95a5a6"


def _chave_status(valor):
    """Valor sem acento, maiúsculo e com '_' virando espaço (Em Titulação -> EM TITULACAO)."""
    if valor is None or str(valor) == "NULL":
        return ""
    t = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode()
    return " ".join(t.upper().replace("_", " ").split())


def _info_status(valor):
    k = _chave_status(valor)
    for prefixo, nome, cor, desc in STATUS_PARCELA:
        if prefixo in k:
            return nome, cor, desc
    return None


def campo_status(cam):
    """Campo da camada cujos valores mais casam com os status conhecidos."""
    melhor, pontos = None, 0
    for f in cam.fields():
        n = 0
        vistos = set()
        for ft in cam.getFeatures():
            v = ft[f.name()]
            if v in vistos:
                continue
            vistos.add(v)
            if _info_status(v):
                n += 1
            if len(vistos) > 200:
                break
        if n > pontos:
            melhor, pontos = f.name(), n
    return melhor


def _poligono_status(cor):
    c = QColor(cor)
    sym = QgsFillSymbol.createSimple({
        "color": "{},{},{},{}".format(c.red(), c.green(), c.blue(), ALFA_STATUS),
        "outline_color": cor, "outline_width": "0.6", "outline_width_unit": "MM"})
    return sym


def parcelas_por_status(cam, expr_rotulo):
    """Cor e legenda por status, preenchimento semitransparente.
    Devolve False se a camada não tem campo de status (fica o estilo padrão)."""
    campo = campo_status(cam)
    if not campo:
        return False
    valores = []
    for ft in cam.getFeatures():
        v = ft[campo]
        v = "" if v is None or str(v) == "NULL" else str(v)
        if v and v not in valores:
            valores.append(v)
    cats = []
    for v in sorted(valores, key=lambda x: [i[1] for i in STATUS_PARCELA].index(
            (_info_status(x) or ("", ""))[0]) if _info_status(x) else 99):
        info = _info_status(v)
        if info:
            nome, cor, desc = info
            cats.append(QgsRendererCategory(v, _poligono_status(cor),
                                            "{} - {}".format(v.upper(), desc)))
        else:
            cats.append(QgsRendererCategory(v, _poligono_status(COR_STATUS_OUTRO), v))
    cats.append(QgsRendererCategory("", _poligono_status(COR_STATUS_OUTRO), "Sem status"))
    cam.setRenderer(QgsCategorizedSymbolRenderer(campo, cats))
    _rotulo(cam, expr_rotulo, 10, "#1e3d2f", "white", 1.2)
    return True
