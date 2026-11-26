# -*- coding: utf-8 -*-
"""Entradas da Caixa de Ferramentas que abrem uma janela ou painel do Restaure
em vez do formulário padrão do Processing.

A subclasse define name/displayName/group/... e `alvo()`, que devolve a função
que abre a ferramenta (de um objeto que vive enquanto o plugin está carregado)
ou None quando não há interface (ex.: qgis_process). Sem parâmetros, o QGIS roda o
algoritmo direto ao dar duplo clique; o Lancador cobre os caminhos que pedem
um formulário (lote, localizador)."""
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsProcessingAlgorithm

from ..compat import OCULTO_NO_MODELADOR, SEM_THREAD


class Lancador(QDialog):
    """No lugar do formulário: ao ser exibido, só chama `abrir`."""

    def __init__(self, abrir, parent=None):
        super().__init__(parent)
        self._abrir_ferramenta = abrir
        self._feito = False

    def _abrir(self):
        if not self._feito:
            self._feito = True
            self._abrir_ferramenta()

    def show(self):
        self._abrir()

    def setVisible(self, visivel):
        if visivel:
            self._abrir()
        else:
            super().setVisible(False)

    def exec(self):
        self._abrir()
        return 0

    exec_ = exec

    def setParameters(self, *_):
        pass


class FerramentaInterativa(QgsProcessingAlgorithm):
    """Algoritmo sem parâmetros que só abre a ferramenta interativa."""

    MENSAGEM_SEM_INTERFACE = "Esta ferramenta só abre com a interface do QGIS."

    def alvo(self):
        """Função que abre a ferramenta, ou None sem interface."""
        raise NotImplementedError

    def _abrir_depois(self):
        """Abre na próxima volta do laço de eventos. False se não há interface."""
        abrir = self.alvo()
        if abrir is None:
            return False
        QTimer.singleShot(0, abrir)
        return True

    def flags(self):
        return super().flags() | SEM_THREAD | OCULTO_NO_MODELADOR

    def initAlgorithm(self, config=None):
        pass

    def createCustomParametersWidget(self, parent=None):
        return Lancador(self._abrir_depois, parent)

    def processAlgorithm(self, parameters, context, feedback):
        if not self._abrir_depois():
            feedback.reportError(self.MENSAGEM_SEM_INTERFACE)
        return {}
