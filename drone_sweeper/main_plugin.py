# -*- coding: utf-8 -*-
"""
Drone Sweeper — main QGIS plugin controller.
"""

from __future__ import annotations

import os
import traceback

from qgis.core import Qgis, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QProgressDialog

from .drone_sweeper_dialog import DroneSweeperDialog
from .processing_engine import (
    ProcessingError,
    build_output_folders,
    ensure_project_directory,
    export_features_as_individual_kml,
    generate_grid_layer,
    name_linear_features,
    organize_layer_tree,
    sanitize_name,
)
from .osm_fetcher import OsmFetchError, fetch_and_clip_osm


class DroneSweeperPlugin:
    """QGIS plugin entry: toolbar/menu action + run orchestration."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "&Drone Sweeper"
        self.toolbar = None
        self.dialog = None
        self._cancel_requested = False

    # ------------------------------------------------------------------ #
    # QGIS plugin lifecycle
    # ------------------------------------------------------------------ #
    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.toolbar = self.iface.addToolBar("Drone Sweeper")
        self.toolbar.setObjectName("DroneSweeperToolbar")

        action = QAction(icon, "Drone Sweeper", self.iface.mainWindow())
        action.setObjectName("drone_sweeper_run")
        action.setWhatsThis(
            "Generate SAR area grids and OSM linear features; export DJI Pilot 2 KML."
        )
        action.triggered.connect(self.run)
        # Plugins menu (what most users look for) + Vector menu (metadata category)
        self.iface.addPluginToMenu(self.menu, action)
        self.iface.addPluginToVectorMenu(self.menu, action)
        self.toolbar.addAction(action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removePluginVectorMenu(self.menu, action)
            if self.toolbar is not None:
                self.toolbar.removeAction(action)
        if self.toolbar is not None:
            del self.toolbar
            self.toolbar = None
        self.actions.clear()
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    # ------------------------------------------------------------------ #
    # UI entry
    # ------------------------------------------------------------------ #
    def run(self):
        if self.dialog is None:
            self.dialog = DroneSweeperDialog(self.iface.mainWindow())
            self.dialog.btn_run.clicked.connect(self._on_generate)
        else:
            self.dialog._populate_layers()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _on_generate(self):
        if self.dialog is None or not self.dialog.validate_inputs():
            return

        layer = self.dialog.selected_layer()
        grid_size = self.dialog.grid_size_meters()
        fetch_trails = self.dialog.fetch_trails()
        fetch_waterways = self.dialog.fetch_waterways()
        polygon_name = sanitize_name(layer.name())

        self._cancel_requested = False
        progress = QProgressDialog(
            "Preparing…",
            "Cancel",
            0,
            100,
            self.iface.mainWindow(),
        )
        progress.setWindowTitle("Drone Sweeper")
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.canceled.connect(self._request_cancel)
        progress.show()

        def set_progress(value: int, text: str) -> None:
            if self._cancel_requested:
                raise ProcessingError("Cancelled by user.")
            progress.setValue(max(0, min(100, int(value))))
            progress.setLabelText(text)
            QCoreApplication.processEvents()

        try:
            summary = self._execute(
                layer=layer,
                polygon_name=polygon_name,
                grid_size=grid_size,
                fetch_trails=fetch_trails,
                fetch_waterways=fetch_waterways,
                set_progress=set_progress,
            )
            progress.setValue(100)
            progress.close()
            self.iface.messageBar().pushMessage(
                "Drone Sweeper",
                summary,
                level=Qgis.Success,
                duration=12,
            )
            QMessageBox.information(self.dialog, "Drone Sweeper", summary)
            self.dialog.accept()
        except ProcessingError as exc:
            progress.close()
            self.iface.messageBar().pushMessage(
                "Drone Sweeper",
                str(exc),
                level=Qgis.Warning,
                duration=10,
            )
            QMessageBox.warning(self.dialog, "Drone Sweeper", str(exc))
        except OsmFetchError as exc:
            progress.close()
            msg = f"OpenStreetMap fetch failed:\n{exc}"
            self.iface.messageBar().pushMessage(
                "Drone Sweeper", msg, level=Qgis.Critical, duration=12
            )
            QMessageBox.critical(self.dialog, "Drone Sweeper", msg)
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors to user
            progress.close()
            detail = f"{exc}\n\n{traceback.format_exc()}"
            self.iface.messageBar().pushMessage(
                "Drone Sweeper",
                str(exc),
                level=Qgis.Critical,
                duration=12,
            )
            QMessageBox.critical(self.dialog, "Drone Sweeper — Unexpected Error", detail)

    def _request_cancel(self):
        self._cancel_requested = True

    # ------------------------------------------------------------------ #
    # Core pipeline
    # ------------------------------------------------------------------ #
    def _execute(
        self,
        layer: QgsVectorLayer,
        polygon_name: str,
        grid_size: float,
        fetch_trails: bool,
        fetch_waterways: bool,
        set_progress,
    ) -> str:
        set_progress(2, "Checking project save location…")
        project_dir = ensure_project_directory()

        set_progress(5, "Creating output folders…")
        flight_dir, area_dir, linear_dir = build_output_folders(project_dir, polygon_name)

        def grid_cb(done, total, msg):
            pct = 5 + int(30 * (done / max(total, 1)))
            set_progress(pct, msg)

        set_progress(8, "Generating search grid…")
        grid_layer = generate_grid_layer(
            layer, grid_size, polygon_name, progress_callback=grid_cb
        )

        set_progress(38, "Exporting grid cell KML files…")

        def kml_grid_cb(done, total, msg):
            pct = 38 + int(22 * (done / max(total, 1)))
            set_progress(pct, msg)

        area_files = export_features_as_individual_kml(
            grid_layer, "name", area_dir, progress_callback=kml_grid_cb
        )

        linear_files: list = []
        if fetch_trails or fetch_waterways:
            set_progress(62, "Fetching OpenStreetMap features…")

            def osm_msg(msg: str):
                set_progress(64, msg)

            clipped = fetch_and_clip_osm(
                layer,
                fetch_trails=fetch_trails,
                fetch_waterways=fetch_waterways,
                progress_callback=osm_msg,
            )
            set_progress(72, "Naming linear features (MGRS)…")
            linear_layer = name_linear_features(clipped, polygon_name)

            set_progress(75, "Exporting linear feature KML files…")

            def kml_lin_cb(done, total, msg):
                pct = 75 + int(8 * (done / max(total, 1)))
                set_progress(pct, msg)

            if linear_layer.featureCount() > 0:
                linear_files = export_features_as_individual_kml(
                    linear_layer,
                    "export_name",
                    linear_dir,
                    progress_callback=kml_lin_cb,
                )
            else:
                set_progress(82, "No OSM linear features found inside the AOI.")
        else:
            set_progress(80, "Skipping OSM download (both toggles off).")

        set_progress(84, "Loading KML files into the layer panel…")

        def tree_cb(done, total, msg):
            pct = 84 + int(15 * (done / max(total, 1)))
            set_progress(pct, msg)

        organize_layer_tree(
            polygon_name,
            area_files,
            linear_files,
            progress_callback=tree_cb,
        )

        set_progress(100, "Done.")
        n_lin = len(linear_files)
        n_area = len(area_files)
        return (
            f"Exported {n_area} area-search KML file(s) and {n_lin} linear-search "
            f"KML file(s) for “{polygon_name}”.\n\n"
            f"Flight Plan: {flight_dir}\n"
            f"Area Search: {area_dir}\n"
            f"Linear Search: {linear_dir}"
        )
