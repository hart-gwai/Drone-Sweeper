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
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

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
    """Create and return (root, area_search_dir, linear_search_dir)."""
    safe = sanitize_filename(polygon_name)
    root = os.path.join(project_dir, safe)
    flight = os.path.join(root, f"Flight Plan_{safe}")
    area_dir = os.path.join(flight, f"Area Search_{safe}")
    linear_dir = os.path.join(flight, f"Linear Search_{safe}")
    os.makedirs(area_dir, exist_ok=True)
    os.makedirs(linear_dir, exist_ok=True)
    return root, area_dir, linear_dir


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


def organize_layer_tree(
    polygon_name: str,
    grid_layer: QgsVectorLayer,
    linear_layer: Optional[QgsVectorLayer],
) -> None:
    """Add results under Flight Plan_<name> / Area Search / Linear Search groups."""
    safe = sanitize_name(polygon_name)
    root = QgsProject.instance().layerTreeRoot()
    flight_group = get_or_create_group(root, f"Flight Plan_{safe}")
    area_group = get_or_create_group(flight_group, f"Area Search_{safe}")
    linear_group = get_or_create_group(flight_group, f"Linear Search_{safe}")

    # Replace previous outputs in these groups
    clear_group_layers(area_group)
    clear_group_layers(linear_group)

    QgsProject.instance().addMapLayer(grid_layer, False)
    area_group.addLayer(grid_layer)

    if linear_layer is not None and linear_layer.featureCount() > 0:
        QgsProject.instance().addMapLayer(linear_layer, False)
        linear_group.addLayer(linear_layer)

    # Labels for grid cells
    try:
        from qgis.core import QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling
        from qgis.PyQt.QtGui import QColor, QFont

        settings = QgsPalLayerSettings()
        settings.fieldName = "name"
        settings.isExpression = False
        settings.enabled = True
        text_format = QgsTextFormat()
        text_format.setFont(QFont("Arial", 8))
        text_format.setSize(8)
        text_format.setColor(QColor(20, 40, 30))
        settings.setFormat(text_format)
        grid_layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        grid_layer.setLabelsEnabled(True)
    except Exception:
        pass
