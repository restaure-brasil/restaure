# -*- coding: utf-8 -*-
"""Geodatabase SIGEF - Importar dados do Geoportal (Restaure › 1. Georreferenciamento).

Janela que busca parcelas certificadas do SIGEF no Geoportal do INCRA (por código
ou por área), gera os limites com os confrontantes, mantém uma base salva para
quando o Geoportal estiver fora do ar e importa planilhas ODS do SIGEF.

Módulos: dialogo (janela), geoportal (WFS do INCRA), cache (base salva SQLite),
ods (leitura das planilhas), geracao (camadas e GeoPackage), estilo, sigef.
"""
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu

try:                                          # Qt6: QAction fica em QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from .. import PASTA_PLUGIN
from ..lancador import FerramentaInterativa

NOME = "Geodatabase SIGEF - Importar dados do Geoportal"
ICONE = os.path.join(PASTA_PLUGIN, "docs", "images", "icon_geoportal.png")
MENU_PLUGIN = "&Restaure"
_MENU = None                  # MenuGeoportal ativo (só existe com a interface do QGIS)


class MenuGeoportal:
    """Entrada em Complementos › Restaure › Georreferenciamento e a janela."""

    def __init__(self, iface):
        self.iface = iface
        self.menu = None
        self.acao = None
        self.dlg = None

    def initGui(self):
        global _MENU
        mw = self.iface.mainWindow()
        self.acao = QAction(QIcon(ICONE), NOME, mw)
        self.acao.setToolTip("Parcelas do SIGEF pelo Geoportal do INCRA, limites com "
                             "confrontantes e planilhas ODS")
        self.acao.triggered.connect(self.abrir)
        self.menu = QMenu("Georreferenciamento", mw)
        self.menu.addAction(self.acao)
        self.iface.addPluginToMenu(MENU_PLUGIN, self.menu.menuAction())
        _MENU = self

    def abrir(self, *_):
        if self.dlg is None:
            from .dialogo import DialogoImportarSigef
            self.dlg = DialogoImportarSigef(self.iface, self.iface.mainWindow())
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def unload(self):
        global _MENU
        _MENU = None
        if self.dlg is not None:
            self.dlg.close()
            self.dlg.deleteLater()
            self.dlg = None
        if self.menu is not None:
            self.iface.removePluginMenu(MENU_PLUGIN, self.menu.menuAction())


class ImportarGeoportalSigef(FerramentaInterativa):
    """Entrada na Caixa de Ferramentas: abre a janela do importador."""

    MENSAGEM_SEM_INTERFACE = "O importador do Geoportal SIGEF só abre com a interface do QGIS."

    def alvo(self):
        return _MENU.abrir if _MENU is not None else None

    def name(self):
        return "importar_geoportal_sigef"

    def displayName(self):
        return NOME

    def group(self):
        return "1. Georreferenciamento"

    def groupId(self):
        return "georreferenciamento"

    def icon(self):
        return QIcon(ICONE)

    def shortHelpString(self):
        url_logo = QUrl.fromLocalFile(
            os.path.join(PASTA_PLUGIN, "docs", "images", "icon.png")).toString()
        return (
            f'<p><img src="{url_logo}" width="96" height="96"></p>'
            "<p>Busca parcelas certificadas do SIGEF no Geoportal do INCRA pelos códigos "
            "(cole links, textos ou planilhas: os códigos são encontrados sozinhos) ou por uma "
            "área de referência (camada de polígonos ou área visível do mapa), e gera as "
            "camadas de parcelas e de limites com os confrontantes.</p>"
            "<p>Cada consulta alimenta uma base salva no perfil do QGIS, usada quando o "
            "Geoportal estiver fora do ar. A base pode ser baixada para uma região ou para o "
            "Brasil inteiro.</p>"
            "<p>Também importa planilhas ODS do SIGEF (uma, várias ou ZIPs) em vértices, limites "
            "e parcelas. As camadas podem ser salvas em GeoPackage.</p>"
            "<p>Também em Complementos › Restaure › Georreferenciamento.</p>"
            "<p>Elaborado por Dra. Ranieli dos Anjos de Souza e Dr. Valdir Moura</p>"
            "<p>Contato para treinamento: restaurear@gmail.com</p>"
            '<p><a href="https://wa.me/5569993512657">Fale conosco</a></p>'
        )

    def createInstance(self):
        return ImportarGeoportalSigef()
