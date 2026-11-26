import math
import os
import tempfile
import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDialog, QGroupBox,
                                 QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                                 QPushButton,
                                 QTextBrowser, QVBoxLayout, QDoubleSpinBox)
from qgis.PyQt.QtGui import QIcon
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsProject, QgsRectangle, QgsSettings)
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox

from ...compat import FILTRO_CAMADA_POLIGONO
from .. import PASTA_PLUGIN
from . import cache, geoportal, geracao, ods, sigef

NOME = "Geodatabase SIGEF - Importar dados do Geoportal"
ICONE = os.path.join(PASTA_PLUGIN, "docs", "images", "icon_geoportal.png")

CHAVE = "restaure_sigef/"
DIAS_AVISO = 30      # base do Brasil mais velha que isso gera o aviso ao abrir
ADIAR_DIAS = 7       # se o usuário disser "não", só avisa de novo depois disso
AREA_GRANDE = 16.0   # graus²: acima disso, pede confirmação antes de baixar


def versao_plugin():
    """Versão do Restaure (metadata.txt do plugin)."""
    try:
        with open(os.path.join(PASTA_PLUGIN, "metadata.txt"), encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("version="):
                    return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def _modo(nome):
    try:
        return getattr(QgsFileWidget.StorageMode, nome)
    except AttributeError:
        return getattr(QgsFileWidget, nome)


def _titulo(texto):
    lb = QLabel(texto)
    f = QFont()
    f.setBold(True)
    lb.setFont(f)
    return lb


class DialogoImportarSigef(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._cancelar = False
        s = QgsSettings()
        self.setWindowTitle("{}  (Restaure v{})".format(NOME, versao_plugin()))
        self.setWindowIcon(QIcon(ICONE))
        self.resize(600, 720)
        lay = QVBoxLayout(self)

        # ===== Geoportal do INCRA =====
        geo = QGroupBox("Parcelas SIGEF – Geoportal do INCRA")
        v = QVBoxLayout(geo)
        v.addWidget(_titulo("Área de Códigos SIGEF"))
        self.txt = QPlainTextEdit()
        self.txt.setMinimumHeight(90)
        v.addWidget(self.txt)
        self.lbl_qtd = QLabel("0 código(s) encontrado(s)")
        v.addWidget(self.lbl_qtd)
        self.txt.textChanged.connect(self._contar)

        v.addWidget(_titulo("Área de referência (quando não houver códigos)"))
        self.cb_camada = QgsMapLayerComboBox()
        self.cb_camada.setFilters(FILTRO_CAMADA_POLIGONO)
        try:
            self.cb_camada.setAllowEmptyLayer(True, "Área visível do mapa")
        except TypeError:
            self.cb_camada.setAllowEmptyLayer(True)
        self.cb_camada.setLayer(None)
        self.cb_camada.setToolTip(
            "Camada de polígonos que define ONDE buscar as parcelas "
            "(ex.: limite do município, da fazenda, de um assentamento).\n"
            "Sem camada escolhida, vale a área que está visível na tela do mapa.")
        v.addWidget(self.cb_camada)

        h = QHBoxLayout()
        h.addWidget(QLabel("Gerar:"))
        self.chk_parcela = QCheckBox("Parcelas")
        self.chk_limite = QCheckBox("Limites com confrontantes")
        for c in (self.chk_parcela, self.chk_limite):
            c.setChecked(True)
            h.addWidget(c)
        h.addStretch()
        v.addLayout(h)
        h = QHBoxLayout()
        h.addWidget(QLabel("Tolerância para encontrar o confrontante:"))
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.1, 20.0)
        self.sp_tol.setDecimals(1)
        self.sp_tol.setSingleStep(0.5)
        self.sp_tol.setSuffix(" m")
        self.sp_tol.setValue(float(s.value(CHAVE + "tol_m", 3.0)))
        self.sp_tol.setToolTip("Distância máxima entre os limites de duas parcelas para "
                               "considerá-las confrontantes. Aumente se aparecer 'Limite a "
                               "definir' entre parcelas vizinhas; diminua se pegar parcelas "
                               "do outro lado de estradas.")
        h.addWidget(self.sp_tol)
        h.addStretch()
        v.addLayout(h)
        self.chk_zoom = QCheckBox("Aproximar às parcelas encontradas (busca por código)")
        self.chk_zoom.setChecked(s.value(CHAVE + "zoom", True, type=bool))
        v.addWidget(self.chk_zoom)

        self.btn_geo = QPushButton("Importar do Geoportal")
        self.btn_geo.setMinimumHeight(30)
        v.addWidget(self.btn_geo)
        lay.addWidget(geo)

        # ===== Base salva (cache) =====
        base = QGroupBox("Base salva (usada quando o Geoportal estiver fora do ar)")
        v = QVBoxLayout(base)
        self.lbl_base = QLabel()
        self.lbl_base.setWordWrap(True)
        self.lbl_base.setToolTip(cache.caminho())
        v.addWidget(self.lbl_base)
        h = QHBoxLayout()
        self.btn_base_area = QPushButton("Baixar só a região")
        self.btn_base_area.setToolTip(
            "Baixa para a base salva todas as parcelas da região.\n\n"
            "Região = a camada escolhida em \"Área de referência\" (acima) ou, "
            "se não houver camada escolhida, a área visível do mapa.")
        self.btn_base_br = QPushButton("Baixar o Brasil inteiro")
        self.btn_base_br.setToolTip("Baixa todas as parcelas do SIGEF. Demora e ocupa "
                                    "vários GB; pode ser interrompido e retomado.")
        h.addWidget(self.btn_base_area)
        h.addWidget(self.btn_base_br)
        v.addLayout(h)
        lay.addWidget(base)
        self._atualizar_base()

        # ===== Planilhas ODS =====
        ods_box = QGroupBox("Planilhas ODS do SIGEF")
        v = QVBoxLayout(ods_box)
        v.addWidget(QLabel("Planilhas .ods ou ZIP com várias planilhas:"))
        self.arq_ods = QgsFileWidget()
        self.arq_ods.setStorageMode(_modo("GetMultipleFiles"))
        self.arq_ods.setFilter("Planilhas SIGEF (*.ods *.zip)")
        self.arq_ods.setDialogTitle("Selecione planilhas ODS do SIGEF ou ZIPs com planilhas")
        v.addWidget(self.arq_ods)
        self.btn_ods = QPushButton("Importar planilhas ODS")
        self.btn_ods.setMinimumHeight(30)
        v.addWidget(self.btn_ods)
        lay.addWidget(ods_box)

        # ===== Saída =====
        self.chk_gpkg = QCheckBox("Salvar em GeoPackage na pasta:")
        self.chk_gpkg.setChecked(s.value(CHAVE + "gpkg", True, type=bool))
        lay.addWidget(self.chk_gpkg)
        self.pasta = QgsFileWidget()
        self.pasta.setStorageMode(_modo("GetDirectory"))
        self.pasta.setFilePath(self._pasta_padrao())
        lay.addWidget(self.pasta)

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setEnabled(False)
        lay.addWidget(self.btn_cancel)

        self.log = QTextBrowser()
        lay.addWidget(self.log)

        rodape = QLabel(
            'Restaure · <a href="https://wa.me/5569993512657">Para treinamentos fale conosco</a>'
            ' · <a href="https://www.instagram.com/restaure_br">Instagram</a>'
            ' · <a href="https://www.linkedin.com/company/restaure-br">LinkedIn</a>')
        rodape.setOpenExternalLinks(True)
        lay.addWidget(rodape)

        self.btn_geo.clicked.connect(self.importar_geoportal)
        self.btn_ods.clicked.connect(self.importar_ods)
        self.btn_base_area.clicked.connect(lambda: self.baixar_base(False))
        self.btn_base_br.clicked.connect(lambda: self.baixar_base(True))
        self.btn_cancel.clicked.connect(self._pedir_cancelamento)

    # ---------- utilitários ----------
    def _pasta_padrao(self):
        casa = QgsProject.instance().homePath()
        if casa:
            return os.path.join(casa, "SIGEF")
        return os.path.join(tempfile.gettempdir(), "database_sigef")

    def _contar(self):
        n = len(sigef.extrair_codigos(self.txt.toPlainText()))
        self.lbl_qtd.setText("{} código(s) encontrado(s)".format(n))

    def _salvar_config(self):
        s = QgsSettings()
        s.setValue(CHAVE + "gpkg", self.chk_gpkg.isChecked())
        s.setValue(CHAVE + "zoom", self.chk_zoom.isChecked())
        s.setValue(CHAVE + "tol_m", self.sp_tol.value())

    def _msg(self, texto, erro=False):
        cor = "#c0392b" if erro else "inherit"
        self.log.append('<span style="color:{}">{}</span>'.format(cor, texto))
        QApplication.processEvents()

    def _ocupado(self, sim):
        for b in (self.btn_geo, self.btn_ods, self.btn_base_area, self.btn_base_br):
            b.setEnabled(not sim)
        self.btn_cancel.setEnabled(sim)
        if sim:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _pedir_cancelamento(self):
        self._cancelar = True
        self._msg("Cancelando…", True)

    def _referencia(self):
        """Retângulo (lon/lat SIRGAS 2000), geometria e descrição da área de referência."""
        destino = QgsCoordinateReferenceSystem(geoportal.CRS)
        cam = self.cb_camada.currentLayer()
        if cam is None:
            canvas = self.iface.mapCanvas()
            tr = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), destino,
                                        QgsProject.instance())
            return tr.transformBoundingBox(canvas.extent()), None, "área visível do mapa"
        tr = QgsCoordinateTransform(cam.crs(), destino, QgsProject.instance())
        geoms = []
        for ft in cam.getFeatures():
            if ft.hasGeometry():
                g = QgsGeometry(ft.geometry())
                g.transform(tr)
                geoms.append(g)
        if not geoms:
            raise geoportal.ErroGeoportal("a camada de referência não tem feições")
        geom = QgsGeometry.unaryUnion(geoms)
        return geom.boundingBox(), geom, "camada '{}'".format(cam.name())

    def _salvar_e_adicionar(self, camadas, prefixo, grupo, origem):
        if self.chk_gpkg.isChecked():
            pasta = self.pasta.filePath() or self._pasta_padrao()
            caminho = os.path.join(pasta, "{}_{}.gpkg".format(
                prefixo, time.strftime("%Y%m%d_%H%M%S")))
            camadas = geracao.salvar_gpkg(camadas, caminho, self._msg)
            self._msg("Salvo em: " + caminho)
        return geracao.adicionar_ao_projeto(camadas, grupo, origem=origem)

    # ---------- base salva ----------
    def showEvent(self, evento):
        super().showEvent(evento)
        if not getattr(self, "_base_verificada", False):
            self._base_verificada = True  # só uma vez por sessão do QGIS
            QTimer.singleShot(400, self._verificar_base_antiga)

    def _verificar_base_antiga(self):
        """Ao abrir: se o Brasil inteiro foi baixado há mais de 30 dias, oferece atualizar."""
        if not self.isVisible():
            return
        data = cache.meta_get("download_brasil")
        dias = cache.dias_desde(data) if data else None
        if dias is None or dias < DIAS_AVISO:
            return
        adiado = cache.meta_get("aviso_adiado")
        depois = cache.dias_desde(adiado) if adiado else None
        if depois is not None and depois < ADIAR_DIAS:
            return
        resp = QMessageBox.question(
            self, "Base salva desatualizada",
            "O Brasil inteiro foi baixado para a base salva em {} (há {} dias).\n\n"
            "Atualizar agora? Pode demorar, e você pode cancelar e continuar depois.\n"
            "Se escolher Não, aviso de novo em {} dias.".format(
                cache.formatar(data), int(dias), ADIAR_DIAS))
        if resp == QMessageBox.StandardButton.Yes:
            self.baixar_base(True, confirmar=False)
        else:
            cache.meta_set("aviso_adiado", cache.agora())

    def _atualizar_base(self):
        n, ultima = cache.estatisticas()
        if not n:
            self.lbl_base.setText("Base salva vazia. Ela é preenchida a cada consulta ao "
                                  "Geoportal ou pelos botões abaixo.")
        else:
            texto = "Base salva: {} parcelas".format("{:,}".format(n).replace(",", "."))
            if ultima:
                texto += " (última atualização em {})".format(cache.formatar(ultima))
            self.lbl_base.setText(texto + ".")

    def baixar_base(self, brasil, confirmar=True):
        try:
            if brasil:
                area, descricao = geoportal.BRASIL, "o Brasil inteiro"
                if confirmar:
                    resp = QMessageBox.question(
                        self, "Baixar o Brasil inteiro",
                        "Isso baixa todas as parcelas do SIGEF (milhões) para a base salva. "
                        "Pode levar horas e ocupar vários GB no disco.\n\n"
                        "Você pode cancelar e repetir depois: o que já foi baixado nas "
                        "últimas 24 h não é baixado de novo.\n\nContinuar?")
                    if resp != QMessageBox.StandardButton.Yes:
                        return
            else:
                area, _, descricao = self._referencia()
                w = area.xMaximum() - area.xMinimum()
                h = area.yMaximum() - area.yMinimum()
                if w * h > AREA_GRANDE:
                    km_h = h * 111.0
                    km_w = w * 111.0 * math.cos(math.radians((area.yMinimum() + area.yMaximum()) / 2))
                    resp = QMessageBox.question(
                        self, "Região grande",
                        "A região ({}) tem cerca de {:.0f} × {:.0f} km. Baixar tudo isso "
                        "pode demorar bastante.\n\nDica: escolha em \"Área de referência\" "
                        "uma camada menor (ex.: o município) ou aproxime o mapa.\n\n"
                        "Continuar?".format(descricao, km_w, km_h))
                    if resp != QMessageBox.StandardButton.Yes:
                        return
        except geoportal.ErroGeoportal as e:
            self._msg("✘ " + str(e), True)
            return
        self._cancelar = False
        self._msg("<b>Baixando {} para a base salva…</b>".format(descricao))
        self._ocupado(True)
        try:
            r = geoportal.baixar_regiao(area, self._msg, lambda: self._cancelar,
                                        QApplication.processEvents)
        except geoportal.ErroGeoportal as e:
            self._msg("✘ " + str(e), True)
            return
        finally:
            self._ocupado(False)
            self._atualizar_base()
        completo = not self._cancelar and not r["falhas"] and not r["restantes"]
        if brasil and completo:
            cache.meta_set("download_brasil", cache.agora())
            cache.meta_set("aviso_adiado", "")
        fim = "Interrompido" if self._cancelar else "Concluído"
        self._msg("<b>{}.</b> {} quadrantes baixados, {} pulados (já baixados há menos de "
                  "24 h), {} falharam{}.".format(
                      fim, r["feitos"], r["pulados"], r["falhas"],
                      ", {} na fila".format(r["restantes"]) if r["restantes"] else ""),
                  bool(r["falhas"]))

    # ---------- Geoportal ----------
    def importar_geoportal(self):
        codigos = sigef.extrair_codigos(self.txt.toPlainText())
        tipos = [t for t, c in (("parcela", self.chk_parcela), ("limite", self.chk_limite))
                 if c.isChecked()]
        if not tipos:
            self._msg("Marque Parcelas e/ou Limites.", erro=True)
            return
        self._salvar_config()
        self._cancelar = False

        def cancelado():
            return self._cancelar
        cam_ref = self.cb_camada.currentLayer()
        regs, vizinhos, faltam = [], [], []
        self._ocupado(True)
        try:
            if codigos:
                self._msg("<b>Buscando {} parcela(s) no Geoportal…</b>".format(len(codigos)))
                regs = geoportal.buscar_codigos(codigos, self._msg, cancelado)
                achados = {r["codigo"] for r in regs}
                faltam = [c for c in codigos if c not in achados]
                if regs and "limite" in tipos and not self._cancelar:
                    self._msg("Buscando as parcelas vizinhas para os confrontantes…")
                    vizinhos = geoportal.buscar_vizinhas(regs, self._msg, cancelado)
            else:
                area, geom, descricao = self._referencia()
                self._msg("<b>Importando as parcelas da {}…</b>".format(descricao))
                # busca com uma pequena margem para achar os confrontantes da borda,
                # mas importa só as parcelas que tocam a área de referência
                ampla = QgsRectangle(area)
                ampla.grow(0.002)
                todas = geoportal.buscar_area(ampla, self._msg, cancelado)
                alvo = geom if geom is not None else QgsGeometry.fromRect(area)
                regs = [r for r in todas if r["geom"].intersects(alvo)]
                vizinhos = todas
        except geoportal.ErroGeoportal as e:
            self._msg("✘ " + str(e), True)
            return
        finally:
            self._ocupado(False)
            self._atualizar_base()

        n = len({r["codigo"] for r in regs})
        if codigos:
            self._msg("Encontradas <b>{} de {}</b> parcelas.".format(n, len(codigos)))
            if faltam:
                self._msg("Não encontradas ({}):<br>{}".format(len(faltam), "<br>".join(faltam)),
                          True)
                if geoportal.usou_base_salva():
                    self._msg("Essas parcelas não estão na base salva (nunca foram "
                              "consultadas com o Geoportal no ar).", True)
                else:
                    self._msg("Podem ser parcelas canceladas, substituídas ou ainda não "
                              "publicadas no Geoportal.", True)
        else:
            self._msg("Encontradas <b>{}</b> parcelas na área.".format(n))
        if not regs or self._cancelar:
            return

        camadas = geracao.gerar_camadas(regs, tipos, vizinhos, self.sp_tol.value())
        if "limite" in camadas:
            a_definir = sum(1 for f in camadas["limite"].getFeatures()
                            if f["situacao"] == "a definir")
            if a_definir:
                self._msg("{} trecho(s) de limite sem parcela vizinha cadastrada: "
                          "'Limite a definir'.".format(a_definir))
        adicionadas = self._salvar_e_adicionar(
            camadas, "geoportal", "SIGEF – Geoportal ({} parcelas) {}{}".format(
                n, time.strftime("%H:%M"),
                " - base salva" if geoportal.usou_base_salva() else ""), "geoportal")

        # zoom: por código, às parcelas; por área, mantém a área de referência
        if codigos:
            if adicionadas and self.chk_zoom.isChecked():
                sigef.aproximar(self.iface.mapCanvas(), adicionadas)
        elif cam_ref is not None:
            canvas = self.iface.mapCanvas()
            tr = QgsCoordinateTransform(cam_ref.crs(), canvas.mapSettings().destinationCrs(),
                                        QgsProject.instance())
            ext = tr.transformBoundingBox(cam_ref.extent())
            ext.scale(1.05)
            canvas.setExtent(ext)
            canvas.refresh()
        self._msg("<b>Concluído.</b>")

    # ---------- ODS ----------
    def importar_ods(self):
        arquivos = [a for a in QgsFileWidget.splitFilePaths(self.arq_ods.filePath()) if a]
        if not arquivos:
            self._msg("Escolha as planilhas ODS ou o ZIP com as planilhas.", erro=True)
            return
        self._salvar_config()
        self._cancelar = False
        self._msg("<b>Lendo planilhas ODS…</b>")
        self._ocupado(True)
        try:
            perimetros = ods.coletar(arquivos, self._msg, lambda: self._cancelar)
            if not perimetros or self._cancelar:
                return
            camadas = ods.gerar_camadas(perimetros, ["vertice", "limite", "parcela"], self._msg)
        finally:
            self._ocupado(False)
        adicionadas = self._salvar_e_adicionar(
            camadas, "planilhas_ods", "Planilhas ODS ({} parcelas) {}".format(
                len(perimetros), time.strftime("%H:%M")), "ods")
        if adicionadas:
            sigef.aproximar(self.iface.mapCanvas(), adicionadas)
        self._msg("<b>Concluído.</b>")
