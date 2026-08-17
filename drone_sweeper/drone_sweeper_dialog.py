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
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

MODE_AREA = "area"
MODE_LINEAR = "linear"


class DroneSweeperDialog(QDialog):
    """Dialog with separate Area Search and Linear Search modes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Drone Sweeper")
        self.setMinimumWidth(440)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()
        self._populate_layers()
        self._on_mode_changed(0)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        title = QLabel("Drone Sweeper — SAR Flight Plan Generator")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        root.addWidget(title)

        mode_row = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Area Search (Grid)", MODE_AREA)
        self.mode_combo.addItem("Linear Search (OSM trails / waterways)", MODE_LINEAR)
        self.mode_combo.setToolTip(
            "Area Search works offline. Linear Search needs an internet connection "
            "for the OpenStreetMap Overpass API."
        )
        mode_row.addRow("Mode:", self.mode_combo)
        root.addLayout(mode_row)

        # Shared AOI picker
        shared = QGroupBox("Area of interest")
        shared_form = QFormLayout(shared)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.layer_combo.setAllowEmptyLayer(False)
        shared_form.addRow("Input polygon layer:", self.layer_combo)
        root.addWidget(shared)

        self.stack = QStackedWidget()

        # ---- Area Search page ----
        area_page = QWidget()
        area_layout = QVBoxLayout(area_page)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_box = QGroupBox("Area Search — MGRS-aligned grid")
        area_form = QFormLayout(area_box)
        self.grid_size = QDoubleSpinBox()
        self.grid_size.setRange(10.0, 10000.0)
        self.grid_size.setDecimals(0)
        self.grid_size.setSingleStep(10.0)
        self.grid_size.setValue(100.0)
        self.grid_size.setSuffix(" m")
        self.grid_size.setToolTip(
            "Cell size in metres. Must be a multiple of 10 so cells snap to MGRS grid lines. "
            "Default 100 × 100 m."
        )
        area_form.addRow("Grid size:", self.grid_size)
        area_note = QLabel(
            "Grid edges snap to MGRS easting/northing lines (multiples of 10 m). "
            "One KML + one layer: Area Search_<polygon>, with features named A1_<polygon>, … "
            "Potential Hazards (power lines/pylons) are fetched when online."
        )
        area_note.setWordWrap(True)
        area_note.setStyleSheet("color: #555; font-size: 11px;")
        area_form.addRow(area_note)
        area_layout.addWidget(area_box)
        area_layout.addStretch(1)
        self.stack.addWidget(area_page)

        # ---- Linear Search page ----
        linear_page = QWidget()
        linear_layout = QVBoxLayout(linear_page)
        linear_layout.setContentsMargins(0, 0, 0, 0)
        osm_box = QGroupBox("Linear Search — OpenStreetMap features")
        osm_layout = QVBoxLayout(osm_box)
        self.chk_trails = QCheckBox("Paths & Trails (path, footway, track, steps, cycleway…)")
        self.chk_trails.setChecked(True)
        self.chk_waterways = QCheckBox("Waterways & Streams (stream, river, canal, drain)")
        self.chk_waterways.setChecked(True)
        osm_layout.addWidget(self.chk_trails)
        osm_layout.addWidget(self.chk_waterways)
        osm_note = QLabel(
            "Requires internet (Overpass API). If the download fails, Area Search "
            "grids already generated are left untouched — switch mode to generate the grid offline."
        )
        osm_note.setWordWrap(True)
        osm_note.setStyleSheet("color: #555; font-size: 11px;")
        osm_layout.addWidget(osm_note)
        linear_layout.addWidget(osm_box)
        linear_layout.addStretch(1)
        self.stack.addWidget(linear_page)

        root.addWidget(self.stack)

        tip = QLabel(
            "Output next to the project file under Flight Plan_<polygon>/:\n"
            "Area Search_<polygon>.kml\n"
            "Paths and Trails_<polygon>.kml  ·  Waterways and Streams_<polygon>.kml"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666; font-size: 11px;")
        root.addWidget(tip)

        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("Generate Area Search")
        self.btn_run.setDefault(True)
        self.btn_run.setMinimumHeight(34)
        self.btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(self.btn_run, stretch=1)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.btn_cancel.clicked.connect(self.reject)

    def _on_mode_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if self.current_mode() == MODE_AREA:
            self.btn_run.setText("Generate Area Search")
        else:
            self.btn_run.setText("Generate Linear Search")

    def current_mode(self) -> str:
        return self.mode_combo.currentData()

    def _populate_layers(self) -> None:
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
            QMessageBox.warning(self, "Drone Sweeper", "Please select an input polygon layer.")
            return False
        if layer.featureCount() == 0:
            QMessageBox.warning(self, "Drone Sweeper", "The selected layer has no features.")
            return False
        if not layer.crs().isValid():
            QMessageBox.warning(
                self,
                "Drone Sweeper",
                f"Layer “{layer.name()}” has no valid CRS.\n"
                "Right-click the layer → Layer CRS → Set Layer CRS… "
                "(use EPSG:4326 if coordinates are lon/lat).",
            )
            return False

        if self.current_mode() == MODE_AREA:
            size = self.grid_size_meters()
            if size < 10:
                QMessageBox.warning(self, "Drone Sweeper", "Grid size must be at least 10 m.")
                return False
            if abs(size % 10.0) > 1e-6:
                QMessageBox.warning(
                    self,
                    "Drone Sweeper",
                    "Grid size must be a multiple of 10 m so cells line up with MGRS lines.",
                )
                return False
        else:
            if not self.fetch_trails() and not self.fetch_waterways():
                QMessageBox.warning(
                    self,
                    "Drone Sweeper",
                    "Enable at least one OpenStreetMap feature type for Linear Search.",
                )
                return False
        return True
