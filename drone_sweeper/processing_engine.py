# -*- coding: utf-8 -*-
"""
Grid generation, naming, KML export, and layer-tree organization for Drone Sweeper.
"""

from __future__ import annotations

import math
import os
import re
from typing import Callable, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFillSymbol,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsLineSymbol,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from .mgrs_converter import format_feature_name
from .osm_fetcher import line_midpoint_xy, transform_geometry, union_aoi_geometry


class ProcessingError(Exception):
    """User-facing processing failure."""


def sanitize_name(name: str) -> str:
    """Filesystem-safe name (keep spaces for KML readability where needed)."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "AOI"


def sanitize_filename(name: str) -> str:
    """Strict filename sanitization for Windows / DJI Pilot 2."""
    cleaned = sanitize_name(name)
    # Trailing dots/spaces are illegal on Windows
    cleaned = cleaned.rstrip(". ")
    return cleaned or "feature"


def col_index_to_letters(index: int) -> str:
    """1-based spreadsheet column index → A, B, … Z, AA, AB, …"""
    if index < 1:
        raise ValueError("Column index must be >= 1")
    result = []
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def suggest_metric_crs(layer: QgsVectorLayer) -> QgsCoordinateReferenceSystem:
    """Pick a local projected CRS for meter-based grid spacing.

    Prefers the UTM zone of the AOI centroid; falls back to EPSG:3857.
    """
    extent = layer.extent()
    cx = (extent.xMinimum() + extent.xMaximum()) / 2.0
    cy = (extent.yMinimum() + extent.yMaximum()) / 2.0
    pt = QgsGeometry.fromPointXY(QgsPointXY(cx, cy))

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    src = layer.crs()
    if src != wgs84:
        xform = QgsCoordinateTransform(src, wgs84, QgsProject.instance())
        pt.transform(xform)

    point = pt.asPoint()
    lon, lat = point.x(), point.y()

    zone = int((lon + 180.0) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
    if crs.isValid():
        return crs
    return QgsCoordinateReferenceSystem("EPSG:3857")


def ensure_project_directory() -> str:
    """Return the directory containing the saved QGIS project file."""
    project_path = QgsProject.instance().fileName()
    if not project_path:
        raise ProcessingError(
            "Please save the QGIS project before generating flight plans. "
            "Drone Sweeper writes output next to the .qgz / .qgs file."
        )
    return os.path.dirname(os.path.abspath(project_path))


def build_output_folders(project_dir: str, polygon_name: str) -> Tuple[str, str, str]:
    """Create and return (flight_plan_dir, area_search_dir, linear_search_dir).

    Layout (adjacent to the saved project file)::

        <Project_Directory>/Flight Plan_<polygon_name>/Area Search_<polygon_name>/
        <Project_Directory>/Flight Plan_<polygon_name>/Linear Search_<polygon_name>/
    """
    safe = sanitize_filename(polygon_name)
    flight = os.path.join(project_dir, f"Flight Plan_{safe}")
    area_dir = os.path.join(flight, f"Area Search_{safe}")
    linear_dir = os.path.join(flight, f"Linear Search_{safe}")
    os.makedirs(area_dir, exist_ok=True)
    os.makedirs(linear_dir, exist_ok=True)
    return flight, area_dir, linear_dir


def generate_grid_layer(
    aoi_layer: QgsVectorLayer,
    grid_size_m: float,
    polygon_name: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> QgsVectorLayer:
    """Build a clipped grid with spreadsheet-style cell names.

    Naming: ``<polygon_name>_<ColLetters><RowNumber>`` e.g. AlphaSector_A1
    Columns: West→East (A, B, …)
    Rows: South→North (1, 2, …)
    """
    if grid_size_m <= 0:
        raise ProcessingError("Grid size must be greater than zero.")

    aoi_geom = union_aoi_geometry(aoi_layer)
    metric_crs = suggest_metric_crs(aoi_layer)
    aoi_metric = transform_geometry(aoi_geom, aoi_layer.crs(), metric_crs)
    bbox = aoi_metric.boundingBox()

    xmin, ymin = bbox.xMinimum(), bbox.yMinimum()
    xmax, ymax = bbox.xMaximum(), bbox.yMaximum()

    # Align grid origin to bbox SW corner
    n_cols = max(1, int(math.ceil((xmax - xmin) / grid_size_m)))
    n_rows = max(1, int(math.ceil((ymax - ymin) / grid_size_m)))
    total = n_cols * n_rows

    layer = QgsVectorLayer(
        f"Polygon?crs={metric_crs.authid()}",
        f"Area Search_{sanitize_name(polygon_name)}",
        "memory",
    )
    provider = layer.dataProvider()
    fields = QgsFields()
    fields.append(QgsField("name", QVariant.String))
    fields.append(QgsField("col_idx", QVariant.Int))
    fields.append(QgsField("row_idx", QVariant.Int))
    fields.append(QgsField("col_letter", QVariant.String))
    provider.addAttributes(fields)
    layer.updateFields()

    features: List[QgsFeature] = []
    done = 0
    safe_poly = sanitize_name(polygon_name)

    for col in range(n_cols):
        for row in range(n_rows):
            done += 1
            if progress_callback and done % 25 == 0:
                progress_callback(done, total, "Generating grid cells…")

            x0 = xmin + col * grid_size_m
            y0 = ymin + row * grid_size_m
            x1 = x0 + grid_size_m
            y1 = y0 + grid_size_m
            cell = QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1))

            if not cell.intersects(aoi_metric):
                continue
            clipped = cell.intersection(aoi_metric)
            if clipped is None or clipped.isEmpty():
                continue
            if QgsWkbTypes.geometryType(clipped.wkbType()) != QgsWkbTypes.PolygonGeometry:
                continue
            # Prefer a single Polygon for DJI Pilot 2 KML
            if QgsWkbTypes.isMultiType(clipped.wkbType()):
                try:
                    multipoly = clipped.asMultiPolygon()
                    if not multipoly:
                        continue
                    clipped = QgsGeometry.fromPolygonXY(
                        max(
                            multipoly,
                            key=lambda poly: abs(QgsGeometry.fromPolygonXY(poly).area()),
                        )
                    )
                except Exception:
                    continue

            col_idx = col + 1  # West→East, 1-based
            row_idx = row + 1  # South→North, 1-based
            letters = col_index_to_letters(col_idx)
            cell_name = f"{safe_poly}_{letters}{row_idx}"

            feat = QgsFeature(layer.fields())
            feat.setGeometry(clipped)
            feat.setAttribute("name", cell_name)
            feat.setAttribute("col_idx", col_idx)
            feat.setAttribute("row_idx", row_idx)
            feat.setAttribute("col_letter", letters)
            features.append(feat)

    if not features:
        raise ProcessingError(
            "No grid cells intersect the AOI. Check the polygon geometry and grid size."
        )

    provider.addFeatures(features)
    layer.updateExtents()
    if progress_callback:
        progress_callback(total, total, f"Generated {len(features)} grid cells.")
    return layer


def name_linear_features(
    line_layer: QgsVectorLayer,
    polygon_name: str,
) -> QgsVectorLayer:
    """Return a copy of line_layer with a resolved display ``export_name`` field."""
    out = QgsVectorLayer(
        f"LineString?crs={line_layer.crs().authid()}",
        f"Linear Search_{sanitize_name(polygon_name)}",
        "memory",
    )
    provider = out.dataProvider()
    fields = QgsFields()
    for f in line_layer.fields():
        fields.append(f)
    fields.append(QgsField("export_name", QVariant.String))
    provider.addAttributes(fields)
    out.updateFields()

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    src_crs = line_layer.crs()
    xform = (
        None
        if src_crs == wgs84
        else QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
    )

    features: List[QgsFeature] = []
    used_names: dict = {}

    for feat in line_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue

        osm_name = (feat.attribute("name") or "").strip()
        feature_type = (feat.attribute("feature_type") or "Path").strip() or "Path"

        if osm_name:
            export_name = osm_name
        else:
            mid = line_midpoint_xy(geom)
            if mid is None:
                export_name = f"{feature_type} unnamed"
            else:
                pt = mid
                if xform is not None:
                    pt = xform.transform(mid)
                export_name = format_feature_name(feature_type, pt.y(), pt.x(), precision=3)

        # Ensure uniqueness for file names
        base = export_name
        if base in used_names:
            used_names[base] += 1
            export_name = f"{base} ({used_names[base]})"
        else:
            used_names[base] = 1

        new_feat = QgsFeature(out.fields())
        new_feat.setGeometry(QgsGeometry(geom))
        attrs = list(feat.attributes()) + [export_name]
        new_feat.setAttributes(attrs)
        features.append(new_feat)

    if features:
        provider.addFeatures(features)
    out.updateExtents()
    return out


def _writer_options() -> "QgsVectorFileWriter.SaveVectorOptions":
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "KML"
    opts.fileEncoding = "UTF-8"
    return opts


def _write_kml_layer(tmp: QgsVectorLayer, out_path: str, src_crs: QgsCoordinateReferenceSystem) -> None:
    """Write a single-feature layer to KML in WGS84 (QGIS 3.16+ compatible)."""
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform_context = QgsProject.instance().transformContext()

    if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
        opts = _writer_options()
        opts.ct = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
        name_idx = tmp.fields().indexOf("Name")
        if name_idx >= 0:
            opts.attributes = [name_idx]
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, out_path, transform_context, opts
        )
        err_code = result[0] if isinstance(result, tuple) else result
        if err_code != QgsVectorFileWriter.NoError:
            msg = result[2] if isinstance(result, tuple) and len(result) > 2 else str(result)
            raise ProcessingError(f"Failed to write KML '{out_path}': {msg}")
        return

    # Fallback for older QGIS builds
    ct = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
    # Reproject feature geometries into a WGS84 temp layer when V3 API is missing
    wgs_layer = QgsVectorLayer(
        f"{QgsWkbTypes.displayString(QgsWkbTypes.flatType(next(tmp.getFeatures()).geometry().wkbType()))}"
        f"?crs=EPSG:4326",
        tmp.name(),
        "memory",
    )
    wgs_prov = wgs_layer.dataProvider()
    wgs_prov.addAttributes(tmp.fields())
    wgs_layer.updateFields()
    out_feats = []
    for f in tmp.getFeatures():
        g = QgsGeometry(f.geometry())
        g.transform(ct)
        nf = QgsFeature(wgs_layer.fields())
        nf.setGeometry(g)
        nf.setAttributes(f.attributes())
        out_feats.append(nf)
    wgs_prov.addFeatures(out_feats)
    err = QgsVectorFileWriter.writeAsVectorFormat(
        wgs_layer, out_path, "UTF-8", wgs84, "KML"
    )
    if isinstance(err, tuple):
        err = err[0]
    if err != QgsVectorFileWriter.NoError:
        raise ProcessingError(f"Failed to write KML '{out_path}'.")


def export_features_as_individual_kml(
    layer: QgsVectorLayer,
    name_field: str,
    output_dir: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[str]:
    """Export each feature as its own WGS84 KML file. Returns written paths."""
    written: List[str] = []
    features = list(layer.getFeatures())
    total = len(features)

    for i, feat in enumerate(features, start=1):
        raw_name = feat.attribute(name_field) or f"feature_{i}"
        file_stem = sanitize_filename(str(raw_name))
        out_path = os.path.join(output_dir, f"{file_stem}.kml")

        # Avoid silent overwrite collisions
        if os.path.exists(out_path):
            n = 2
            while os.path.exists(os.path.join(output_dir, f"{file_stem}_{n}.kml")):
                n += 1
            out_path = os.path.join(output_dir, f"{file_stem}_{n}.kml")

        geom = feat.geometry()
        flat = QgsWkbTypes.flatType(geom.wkbType())
        # DJI Pilot 2 prefers simple Polygon / LineString
        if QgsWkbTypes.geometryType(flat) == QgsWkbTypes.PolygonGeometry:
            wkb_name = "Polygon"
            if QgsWkbTypes.isMultiType(flat):
                # Take largest polygon part for a simple KML
                polys = geom.asMultiPolygon() if hasattr(geom, "asMultiPolygon") else []
                if polys:
                    geom = QgsGeometry.fromPolygonXY(max(polys, key=lambda p: abs(QgsGeometry.fromPolygonXY(p).area())))
        elif QgsWkbTypes.geometryType(flat) == QgsWkbTypes.LineGeometry:
            wkb_name = "LineString"
            if QgsWkbTypes.isMultiType(flat):
                lines = geom.asMultiPolyline()
                if lines:
                    geom = QgsGeometry.fromPolylineXY(max(lines, key=len))
        else:
            wkb_name = QgsWkbTypes.displayString(flat)

        tmp = QgsVectorLayer(
            f"{wkb_name}?crs={layer.crs().authid()}",
            file_stem,
            "memory",
        )
        tmp_prov = tmp.dataProvider()
        tmp_fields = QgsFields()
        tmp_fields.append(QgsField("Name", QVariant.String))
        tmp_fields.append(QgsField("name", QVariant.String))
        tmp_prov.addAttributes(tmp_fields)
        tmp.updateFields()

        single = QgsFeature(tmp.fields())
        single.setGeometry(QgsGeometry(geom))
        single.setAttribute("Name", str(raw_name))
        single.setAttribute("name", str(raw_name))
        tmp_prov.addFeature(single)
        tmp.updateExtents()

        _write_kml_layer(tmp, out_path, layer.crs())
        written.append(out_path)
        if progress_callback and (i % 10 == 0 or i == total):
            progress_callback(i, total, f"Exporting KML ({i}/{total})…")

    return written


def get_or_create_group(parent: QgsLayerTreeGroup, name: str) -> QgsLayerTreeGroup:
    """Find a child group by name or create it."""
    for child in parent.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == name:
            return child
    return parent.addGroup(name)


def clear_group_layers(group: QgsLayerTreeGroup) -> None:
    """Remove layers previously added under a group (keep subgroup structure)."""
    project = QgsProject.instance()
    to_remove = []
    for child in list(group.children()):
        if isinstance(child, QgsLayerTreeLayer):
            to_remove.append(child.layerId())
        elif isinstance(child, QgsLayerTreeGroup):
            clear_group_layers(child)
    if to_remove:
        project.removeMapLayers(to_remove)


def load_kml_layer(kml_path: str) -> Optional[QgsVectorLayer]:
    """Open an on-disk KML as an OGR vector layer (not a memory scratch layer)."""
    layer_name = os.path.splitext(os.path.basename(kml_path))[0]
    layer = QgsVectorLayer(kml_path, layer_name, "ogr")
    if not layer.isValid():
        return None
    return layer


# Distinct colours for linear SAR features (readable on imagery / OSM basemaps)
_LINEAR_COLORS = (
    QColor(0, 120, 255),    # blue
    QColor(0, 170, 80),     # green
    QColor(255, 140, 0),    # orange
    QColor(160, 60, 200),   # purple
    QColor(0, 190, 200),    # cyan
    QColor(220, 50, 50),    # red
    QColor(230, 190, 0),    # gold
    QColor(30, 30, 30),     # near-black
    QColor(255, 80, 160),   # magenta/pink
    QColor(90, 60, 20),     # brown
)


def style_area_grid_layer(layer: QgsVectorLayer) -> None:
    """Red outline + red fill at 50% opacity for area-search grid cells."""
    symbol = QgsFillSymbol.createSimple(
        {
            "color": "255,0,0,128",  # 50% opaque red fill
            "style": "solid",
            "outline_color": "220,0,0,255",
            "outline_width": "0.4",
            "outline_style": "solid",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def style_linear_layer(layer: QgsVectorLayer, color_index: int = 0) -> None:
    """Thick coloured stroke for linear-search features; cycles a colour palette."""
    color = _LINEAR_COLORS[color_index % len(_LINEAR_COLORS)]
    symbol = QgsLineSymbol.createSimple(
        {
            "line_color": f"{color.red()},{color.green()},{color.blue()},255",
            "line_width": "2.0",
            "line_width_unit": "MM",
            "line_style": "solid",
            "capstyle": "round",
            "joinstyle": "round",
        }
    )
    symbol.setWidth(2.0)
    symbol.setColor(color)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def add_kml_files_to_group(
    group: QgsLayerTreeGroup,
    kml_paths: List[str],
    style: str = "none",
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """Add each KML path as its own project layer under ``group``. Returns count added.

    :param style: ``"area"`` (red 50% fill), ``"linear"`` (thick multi-colour), or ``"none"``
    """
    project = QgsProject.instance()
    added = 0
    total = len(kml_paths)
    batch: List[QgsVectorLayer] = []

    def _style_and_attach(layers: List[QgsVectorLayer], start_index: int) -> None:
        project.addMapLayers(layers, False)
        for offset, lyr in enumerate(layers):
            if style == "area":
                style_area_grid_layer(lyr)
            elif style == "linear":
                style_linear_layer(lyr, start_index + offset)
            group.addLayer(lyr)

    for i, path in enumerate(kml_paths, start=1):
        layer = load_kml_layer(path)
        if layer is None:
            continue
        batch.append(layer)
        # Flush in chunks so the UI stays responsive on large grids
        if len(batch) >= 50:
            _style_and_attach(batch, added)
            added += len(batch)
            batch = []
            if progress_callback:
                progress_callback(i, total, f"Adding KML layers ({i}/{total})…")

    if batch:
        _style_and_attach(batch, added)
        added += len(batch)

    if progress_callback and total:
        progress_callback(total, total, f"Added {added} KML layer(s).")
    return added


def organize_layer_tree(
    polygon_name: str,
    area_kml_paths: List[str],
    linear_kml_paths: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """Load exported KML files into Flight Plan / Area Search / Linear Search groups."""
    safe = sanitize_name(polygon_name)
    root = QgsProject.instance().layerTreeRoot()
    flight_group = get_or_create_group(root, f"Flight Plan_{safe}")
    area_group = get_or_create_group(flight_group, f"Area Search_{safe}")
    linear_group = get_or_create_group(flight_group, f"Linear Search_{safe}")

    # Replace previous outputs in these groups
    clear_group_layers(area_group)
    clear_group_layers(linear_group)

    area_total = len(area_kml_paths)
    linear_paths = linear_kml_paths or []
    grand_total = max(area_total + len(linear_paths), 1)

    def area_cb(done, total, msg):
        if progress_callback:
            progress_callback(done, grand_total, msg)

    add_kml_files_to_group(
        area_group, area_kml_paths, style="area", progress_callback=area_cb
    )

    if linear_paths:
        def linear_cb(done, total, msg):
            if progress_callback:
                progress_callback(area_total + done, grand_total, msg)

        add_kml_files_to_group(
            linear_group, linear_paths, style="linear", progress_callback=linear_cb
        )
