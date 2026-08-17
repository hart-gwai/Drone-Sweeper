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

from .drone_sweeper_dialog import MODE_AREA, MODE_LINEAR, DroneSweeperDialog
from .processing_engine import (
    ProcessingError,
    area_search_kml_path,
    build_output_folders,
    ensure_project_directory,
    export_area_search_kml,
    export_hazards_as_gpx,
    export_linear_category_kml,
    filter_lines_by_category,
    generate_grid_layer,
    name_linear_features,
    organize_layer_tree,
    paths_trails_kml_path,
    sanitize_name,
    waterways_streams_kml_path,
)
from .osm_fetcher import OsmFetchError, fetch_and_clip_hazards, fetch_and_clip_osm


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
        mode = self.dialog.current_mode()
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
            if mode == MODE_AREA:
                summary = self._execute_area(
                    layer=layer,
                    polygon_name=polygon_name,
                    grid_size=self.dialog.grid_size_meters(),
                    set_progress=set_progress,
                )
            else:
                summary = self._execute_linear(
                    layer=layer,
                    polygon_name=polygon_name,
                    fetch_trails=self.dialog.fetch_trails(),
                    fetch_waterways=self.dialog.fetch_waterways(),
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
            msg = (
                f"OpenStreetMap fetch failed:\n{exc}\n\n"
                "Area Search (grid) is separate — switch the mode dropdown to "
                "generate the grid offline without OSM."
            )
            self.iface.messageBar().pushMessage(
                "Drone Sweeper", msg, level=Qgis.Critical, duration=12
            )
            QMessageBox.critical(self.dialog, "Drone Sweeper", msg)
        except Exception as exc:  # noqa: BLE001
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

    def _execute_area(
        self,
        layer: QgsVectorLayer,
        polygon_name: str,
        grid_size: float,
        set_progress,
    ) -> str:
        set_progress(2, "Checking project save location…")
        project_dir = ensure_project_directory()

        set_progress(5, "Creating Flight Plan folder…")
        flight_dir, _area_parent, _linear_dir, hazards_dir = build_output_folders(
            project_dir,
            polygon_name,
            create_area=True,
            create_linear=False,
            create_hazards=True,
        )
        area_kml = area_search_kml_path(flight_dir, polygon_name)

        def grid_cb(done, total, msg):
            set_progress(5 + int(40 * (done / max(total, 1))), msg)

        set_progress(8, "Generating MGRS-aligned search grid…")
        grid_layer = generate_grid_layer(
            layer, grid_size, polygon_name, progress_callback=grid_cb
        )

        def kml_cb(done, total, msg):
            set_progress(50 + int(25 * (done / max(total, 1))), msg)

        set_progress(50, "Exporting Area Search KML…")
        export_area_search_kml(grid_layer, area_kml, progress_callback=kml_cb)
        n_cells = grid_layer.featureCount()

        set_progress(78, "Loading Area Search layer…")
        organize_layer_tree(
            polygon_name,
            area_kml_path=area_kml,
            replace_area=True,
            replace_linear=False,
            replace_hazards=False,
        )

        # Best-effort hazards: never fail the Area Search grid if OSM is down
        hazard_note = ""
        set_progress(82, "Fetching potential hazards (power lines / pylons)…")
        try:
            def haz_msg(msg: str):
                set_progress(84, msg)

            points, lines = fetch_and_clip_hazards(layer, progress_callback=haz_msg)
            set_progress(92, "Exporting hazard GPX files…")
            hazard_files = export_hazards_as_gpx(points, lines, hazards_dir)
            organize_layer_tree(
                polygon_name,
                hazard_gpx_paths=hazard_files,
                replace_hazards=True,
            )
            if hazard_files:
                n_pt = points.featureCount()
                n_ln = lines.featureCount()
                hazard_note = (
                    f"\nPotential Hazards: {n_pt} pylon/pole(s), {n_ln} power line(s) "
                    f"→ {hazards_dir}"
                )
            else:
                hazard_note = (
                    "\nPotential Hazards: no mapped power lines/pylons found in the AOI "
                    "(OSM coverage may be incomplete)."
                )
        except (OsmFetchError, ProcessingError, Exception) as exc:
            hazard_note = (
                f"\nPotential Hazards: skipped ({exc}). "
                "Grid export completed; OSM hazard data needs internet."
            )
            self.iface.messageBar().pushMessage(
                "Drone Sweeper",
                f"Potential Hazards skipped: {exc}",
                level=Qgis.Warning,
                duration=10,
            )

        set_progress(100, "Done.")
        return (
            f"Area Search: {n_cells} grid cell(s) in one KML for “{polygon_name}”.\n\n"
            f"Flight Plan: {flight_dir}\n"
            f"KML: {area_kml}"
            f"{hazard_note}"
        )

    def _execute_linear(
        self,
        layer: QgsVectorLayer,
        polygon_name: str,
        fetch_trails: bool,
        fetch_waterways: bool,
        set_progress,
    ) -> str:
        set_progress(2, "Checking project save location…")
        project_dir = ensure_project_directory()

        set_progress(5, "Creating Flight Plan folder…")
        flight_dir, _a, _l, _h = build_output_folders(
            project_dir, polygon_name, create_area=False, create_linear=True
        )
        trails_path = paths_trails_kml_path(flight_dir, polygon_name)
        water_path = waterways_streams_kml_path(flight_dir, polygon_name)

        set_progress(10, "Fetching OpenStreetMap features…")

        def osm_msg(msg: str):
            set_progress(15, msg)

        clipped = fetch_and_clip_osm(
            layer,
            fetch_trails=fetch_trails,
            fetch_waterways=fetch_waterways,
            progress_callback=osm_msg,
        )
        set_progress(55, "Naming linear features…")
        linear_layer = name_linear_features(clipped, polygon_name)

        trails_kml = None
        water_kml = None
        n_trails = n_water = 0

        if fetch_trails:
            trails = filter_lines_by_category(
                linear_layer, "trail", f"Paths and Trails_{sanitize_name(polygon_name)}"
            )
            n_trails = trails.featureCount()
            set_progress(60, f"Exporting Paths and Trails ({n_trails} features)…")
            trails_kml = export_linear_category_kml(trails, trails_path)

        if fetch_waterways:
            water = filter_lines_by_category(
                linear_layer,
                "waterway",
                f"Waterways and Streams_{sanitize_name(polygon_name)}",
            )
            n_water = water.featureCount()
            set_progress(72, f"Exporting Waterways and Streams ({n_water} features)…")
            water_kml = export_linear_category_kml(water, water_path)

        if n_trails == 0 and n_water == 0:
            set_progress(80, "No OSM linear features found inside the AOI.")

        set_progress(85, "Loading linear layers…")
        organize_layer_tree(
            polygon_name,
            trails_kml_path=trails_kml,
            waterways_kml_path=water_kml,
            replace_area=False,
            replace_linear=True,
            replace_hazards=False,
        )

        set_progress(100, "Done.")
        lines = [
            f"Linear Search for “{polygon_name}”:",
            f"  Paths and Trails: {n_trails} feature(s)",
            f"  Waterways and Streams: {n_water} feature(s)",
            "",
            f"Flight Plan: {flight_dir}",
        ]
        if trails_kml:
            lines.append(f"KML: {trails_kml}")
        if water_kml:
            lines.append(f"KML: {water_kml}")
        return "\n".join(lines)
