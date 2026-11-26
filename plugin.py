# -*- coding: utf-8 -*-
from qgis.core import QgsApplication
from .provider import RestaureProvider


class RestaurePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.geoportal = None
        self.painel = None

    def initProcessing(self):
        self.provider = RestaureProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        from .algoritmos.geoportal_sigef import MenuGeoportal
        self.geoportal = MenuGeoportal(self.iface)      # submenu Georreferenciamento primeiro
        self.geoportal.initGui()
        from .algoritmos.medidas_ao_vivo import PainelMedidas
        self.painel = PainelMedidas(self.iface)
        self.painel.initGui()

    def unload(self):
        if self.geoportal:
            self.geoportal.unload()
            self.geoportal = None
        if self.painel:
            self.painel.unload()
            self.painel = None
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
