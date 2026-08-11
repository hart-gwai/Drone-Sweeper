# -*- coding: utf-8 -*-
"""
Drone Sweeper dialog UI (pure PyQt5 — no .ui compiler required).
"""

from __future__ import annotations

from qgis.core import QgsMapLayer, QgsMapLayerProxyModel, QgsProject, QgsWkbTypes
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class DroneSweeperDialog(QDialog):
    """Dockable-style dialog for configuring and launching a flight-plan run."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Drone Sweeper")
        self.setMinimumWidth(420)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()
        self._populate_layers()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        title = QLabel("Drone Sweeper — SAR Flight Plan Generator")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        root.addWidget(title)

        subtitle = QLabel(
            "Generate area-search grids and OSM linear features, then export "
            "DJI Pilot 2–compatible KML files beside your saved project."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #555;")
        root.addWidget(subtitle)

        form_box = QGroupBox("Inputs")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignRight)

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.layer_combo.setAllowEmptyLayer(False)
        form.addRow("Input polygon layer:", self.layer_combo)

        self.grid_size = QDoubleSpinBox()
        self.grid_size.setRange(1.0, 10000.0)
        self.grid_size.setDecimals(1)
        self.grid_size.setSingleStep(5.0)
        self.grid_size.setValue(50.0)
        self.grid_size.setSuffix(" m")
        self.grid_size.setToolTip("Grid cell size in meters (e.g. 50 for 50×50 m cells).")
        form.addRow("Grid size:", self.grid_size)

        root.addWidget(form_box)

        osm_box = QGroupBox("OpenStreetMap features")
        osm_layout = QVBoxLayout(osm_box)
        self.chk_trails = QCheckBox("Paths & Trails (path, footway, track, steps, cycleway…)")
        self.chk_trails.setChecked(True)
        self.chk_waterways = QCheckBox("Waterways & Streams (stream, river, canal, drain)")
        self.chk_waterways.setChecked(True)
        osm_layout.addWidget(self.chk_trails)
        osm_layout.addWidget(self.chk_waterways)
        root.addWidget(osm_box)

        tip = QLabel(
            "Output is written next to the project file under:\n"
            "<polygon>/<Flight Plan_…>/<Area Search_…> and <Linear Search_…>"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666; font-size: 11px;")
        root.addWidget(tip)

        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("Generate & Export Flight Plans")
        self.btn_run.setDefault(True)
        self.btn_run.setMinimumHeight(34)
        self.btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(self.btn_run, stretch=1)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

        self.btn_cancel.clicked.connect(self.reject)

    def _populate_layers(self) -> None:
        """Refresh combo; QgsMapLayerComboBox tracks the project automatically."""
        # Prefer currently selected polygon layer if any
        layer = self.layer_combo.currentLayer()
        if layer is None:
            for lyr in QgsProject.instance().mapLayers().values():
                if (
                    lyr.type() == QgsMapLayer.VectorLayer
                    and hasattr(lyr, "geometryType")
                    and lyr.geometryType() == QgsWkbTypes.PolygonGeometry
                ):
                    self.layer_combo.setLayer(lyr)
                    break

    def selected_layer(self):
        return self.layer_combo.currentLayer()

    def grid_size_meters(self) -> float:
        return float(self.grid_size.value())

    def fetch_trails(self) -> bool:
        return self.chk_trails.isChecked()

    def fetch_waterways(self) -> bool:
        return self.chk_waterways.isChecked()

    def validate_inputs(self) -> bool:
        layer = self.selected_layer()
        if layer is None:
            QMessageBox.warning(
                self,
                "Drone Sweeper",
                "Please select an input polygon layer.",
            )
            return False
        if layer.featureCount() == 0:
            QMessageBox.warning(
                self,
                "Drone Sweeper",
                "The selected layer has no features.",
            )
            return False
        if not self.fetch_trails() and not self.fetch_waterways():
            # Allowed — grid-only run
            pass
        if self.grid_size_meters() <= 0:
            QMessageBox.warning(self, "Drone Sweeper", "Grid size must be greater than zero.")
            return False
        return True
