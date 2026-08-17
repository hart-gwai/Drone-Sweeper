# -*- coding: utf-8 -*-
"""
Grid generation, naming, KML export, and layer-tree organization for Drone Sweeper.
"""

from __future__ import annotations

import math
import os
import re
from typing import Callable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

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
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant, QDir
from qgis.PyQt.QtGui import QColor

from .mgrs_converter import format_feature_name, latlon_to_mgrs
from .osm_fetcher import (
    OsmFetchError,
    layer_feature_extent,
    line_midpoint_xy,
    transform_geometry,
    union_aoi_geometry,
)


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


def extract_xy_from_cell_name(name: str) -> str:
    """Extract spreadsheet XY token from ``poly_A12_50Q KK 123456`` style names."""
    m = re.search(r"_([A-Z]+\d+)_\d{1,2}[C-X]\b", name or "")
    if m:
        return m.group(1)
    m = re.search(r"_([A-Z]+\d+)(?:_|\.|$)", name or "")
    return m.group(1) if m else (name or "")


def suggest_metric_crs(layer: QgsVectorLayer) -> QgsCoordinateReferenceSystem:
    """Pick a local projected CRS for meter-based grid spacing.

    Prefers the UTM zone of the AOI centroid; falls back to EPSG:3857.
    UTM easting/northing align with MGRS numerical grid lines.
    """
    try:
        extent = layer_feature_extent(layer)
    except OsmFetchError:
        extent = layer.extent()
        if extent.isEmpty():
            raise ProcessingError(
                f"Layer “{layer.name()}” has no usable extent for grid generation."
            )

    cx = (extent.xMinimum() + extent.xMaximum()) / 2.0
    cy = (extent.yMinimum() + extent.yMaximum()) / 2.0
    if not (math.isfinite(cx) and math.isfinite(cy)):
        raise ProcessingError(
            f"Layer “{layer.name()}” has an invalid extent. "
            "Check the layer CRS matches its coordinates."
        )

    pt = QgsGeometry.fromPointXY(QgsPointXY(cx, cy))

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    src = layer.crs()
    if not src.isValid():
        raise ProcessingError(
            f"Layer “{layer.name()}” has no valid CRS. "
            "Assign a CRS before generating flight plans."
        )
    if src != wgs84:
        try:
            xform = QgsCoordinateTransform(src, wgs84, QgsProject.instance())
            pt.transform(xform)
        except Exception as exc:
            raise ProcessingError(
                f"Could not reproject “{layer.name()}” to WGS84 to pick a UTM zone.\n{exc}"
            ) from exc

    point = pt.asPoint()
    lon, lat = point.x(), point.y()
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ProcessingError(
            f"Could not determine a geographic position for “{layer.name()}”. "
            "The layer CRS may not match the geometry coordinates."
        )

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


def build_output_folders(
    project_dir: str,
    polygon_name: str,
    create_area: bool = True,
    create_linear: bool = True,
    create_hazards: bool = False,
) -> Tuple[str, str, str, str]:
    """Create and return (flight, area_dir, linear_dir, hazards_dir)."""
    safe = sanitize_filename(polygon_name)
    flight = os.path.join(project_dir, f"Flight Plan_{safe}")
    area_dir = os.path.join(flight, f"Area Search_{safe}")
    linear_dir = os.path.join(flight, f"Linear Search_{safe}")
    hazards_dir = os.path.join(flight, "Potential Hazards")
    os.makedirs(flight, exist_ok=True)
    if create_area:
        os.makedirs(area_dir, exist_ok=True)
    if create_linear:
        os.makedirs(linear_dir, exist_ok=True)
    if create_hazards:
        os.makedirs(hazards_dir, exist_ok=True)
    return flight, area_dir, linear_dir, hazards_dir


def generate_grid_layer(
    aoi_layer: QgsVectorLayer,
    grid_size_m: float,
    polygon_name: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> QgsVectorLayer:
    """Build an MGRS-aligned clipped grid.

    Cell edges snap to UTM/MGRS metre lines. Size must be a multiple of 10 m.
    Naming: ``<polygon>_<XY>_<MGRS100m>`` e.g. ``AlphaSector_A1_50Q KK 123456``
    Columns West→East (A…); rows South→North (1…).
    """
    if grid_size_m < 10:
        raise ProcessingError("Grid size must be at least 10 m.")
    if abs(grid_size_m % 10.0) > 1e-6:
        raise ProcessingError(
            "Grid size must be a multiple of 10 m so cells line up with MGRS lines."
        )

    aoi_geom = union_aoi_geometry(aoi_layer)
    metric_crs = suggest_metric_crs(aoi_layer)
    aoi_metric = transform_geometry(aoi_geom, aoi_layer.crs(), metric_crs)
    bbox = aoi_metric.boundingBox()

    xmin, ymin = bbox.xMinimum(), bbox.yMinimum()
    xmax, ymax = bbox.xMaximum(), bbox.yMaximum()

    # Snap to MGRS/UTM metre grid (multiples of grid_size_m)
    x0_origin = math.floor(xmin / grid_size_m) * grid_size_m
    y0_origin = math.floor(ymin / grid_size_m) * grid_size_m
    x1_limit = math.ceil(xmax / grid_size_m) * grid_size_m
    y1_limit = math.ceil(ymax / grid_size_m) * grid_size_m

    n_cols = max(1, int(round((x1_limit - x0_origin) / grid_size_m)))
    n_rows = max(1, int(round((y1_limit - y0_origin) / grid_size_m)))
    total = n_cols * n_rows

    layer = QgsVectorLayer(
        f"Polygon?crs={metric_crs.authid()}",
        f"Area Search_{sanitize_name(polygon_name)}",
        "memory",
    )
    provider = layer.dataProvider()
    fields = QgsFields()
    fields.append(QgsField("name", QVariant.String))
    fields.append(QgsField("label", QVariant.String))
    fields.append(QgsField("mgrs", QVariant.String))
    fields.append(QgsField("col_idx", QVariant.Int))
    fields.append(QgsField("row_idx", QVariant.Int))
    fields.append(QgsField("col_letter", QVariant.String))
    provider.addAttributes(fields)
    layer.updateFields()

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    to_wgs = QgsCoordinateTransform(metric_crs, wgs84, QgsProject.instance())

    features: List[QgsFeature] = []
    done = 0
    safe_poly = sanitize_name(polygon_name)

    for col in range(n_cols):
        for row in range(n_rows):
            done += 1
            if progress_callback and done % 25 == 0:
                progress_callback(done, total, "Generating MGRS-aligned grid…")

            x0 = x0_origin + col * grid_size_m
            y0 = y0_origin + row * grid_size_m
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

            col_idx = col + 1
            row_idx = row + 1
            letters = col_index_to_letters(col_idx)
            xy_label = f"{letters}{row_idx}"

            # MGRS at 100 m (6 digits) from unclipped cell centre
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            wgs_pt = to_wgs.transform(QgsPointXY(cx, cy))
            mgrs = latlon_to_mgrs(wgs_pt.y(), wgs_pt.x(), precision=3, spaced=True)
            cell_name = f"{safe_poly}_{xy_label}_{mgrs}"

            feat = QgsFeature(layer.fields())
            feat.setGeometry(clipped)
            feat.setAttribute("name", cell_name)
            feat.setAttribute("label", xy_label)
            feat.setAttribute("mgrs", mgrs)
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


def _reproject_geom_wgs84(
    geom: QgsGeometry,
    src_crs: QgsCoordinateReferenceSystem,
) -> QgsGeometry:
    """Return geometry copied into WGS84."""
    out = QgsGeometry(geom)
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if src_crs != wgs84:
        xform = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
        out.transform(xform)
    return out


def _ring_to_coord_text(
    ring: Sequence,
    altitude_m: float = 0.0,
) -> str:
    """Build ``lon,lat,alt lon,lat,alt …`` for DJI Pilot 2 (altitude required)."""
    parts = []
    for pt in ring:
        # QgsPointXY or (x,y)
        try:
            lon, lat = float(pt.x()), float(pt.y())
        except AttributeError:
            lon, lat = float(pt[0]), float(pt[1])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        parts.append(f"{lon:.10f},{lat:.10f},{altitude_m:.1f}")
    if len(parts) >= 2 and parts[0] != parts[-1]:
        parts.append(parts[0])  # close ring
    return " ".join(parts)


def _line_to_coord_text(
    line: Sequence,
    altitude_m: float = 0.0,
) -> str:
    parts = []
    for pt in line:
        try:
            lon, lat = float(pt.x()), float(pt.y())
        except AttributeError:
            lon, lat = float(pt[0]), float(pt[1])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        parts.append(f"{lon:.10f},{lat:.10f},{altitude_m:.1f}")
    return " ".join(parts)


def build_dji_pilot_kml(
    name: str,
    geom_wgs84: QgsGeometry,
    altitude_m: float = 0.0,
) -> str:
    """Minimal KML accepted by DJI Pilot 2 (no Folder; altitude on every vertex).

    Area/mapping import → Polygon. Waypoint / mapping corridor → LineString.
    """
    safe_name = xml_escape(name or "FlightPlan")
    gtype = QgsWkbTypes.geometryType(geom_wgs84.wkbType())

    if gtype == QgsWkbTypes.PolygonGeometry:
        if QgsWkbTypes.isMultiType(geom_wgs84.wkbType()):
            polys = geom_wgs84.asMultiPolygon()
            ring = max(polys, key=lambda p: abs(QgsGeometry.fromPolygonXY(p).area()))[0]
        else:
            ring = geom_wgs84.asPolygon()[0]
        coords = _ring_to_coord_text(ring, altitude_m=altitude_m)
        if coords.count(",") < 6:  # need ≥3 vertices with lon,lat,alt
            raise ProcessingError(f"Polygon “{name}” has too few vertices for DJI KML.")
        geometry_xml = f"""    <Polygon>
      <tessellate>1</tessellate>
      <altitudeMode>relativeToGround</altitudeMode>
      <outerBoundaryIs>
        <LinearRing>
          <coordinates>{coords}</coordinates>
        </LinearRing>
      </outerBoundaryIs>
    </Polygon>"""
        style = """  <Style id="polyStyle">
    <LineStyle><color>ff0000ff</color><width>1.5</width></LineStyle>
    <PolyStyle><color>7d0000ff</color></PolyStyle>
  </Style>"""
        style_url = "#polyStyle"
    elif gtype == QgsWkbTypes.LineGeometry:
        if QgsWkbTypes.isMultiType(geom_wgs84.wkbType()):
            line = max(geom_wgs84.asMultiPolyline(), key=len)
        else:
            line = geom_wgs84.asPolyline()
        coords = _line_to_coord_text(line, altitude_m=altitude_m)
        if coords.count(",") < 4:  # need ≥2 vertices
            raise ProcessingError(f"Line “{name}” has too few vertices for DJI KML.")
        geometry_xml = f"""    <LineString>
      <tessellate>1</tessellate>
      <altitudeMode>relativeToGround</altitudeMode>
      <coordinates>{coords}</coordinates>
    </LineString>"""
        style = """  <Style id="lineStyle">
    <LineStyle><color>ff0000ff</color><width>2</width></LineStyle>
  </Style>"""
        style_url = "#lineStyle"
    else:
        raise ProcessingError(
            f"Unsupported geometry for DJI KML (“{name}”). Need Polygon or LineString."
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{safe_name}</name>
{style}
  <Placemark>
    <name>{safe_name}</name>
    <styleUrl>{style_url}</styleUrl>
{geometry_xml}
  </Placemark>
</Document>
</kml>
"""


def write_dji_kml(kml_path: str, kml_text: str) -> str:
    """Write a DJI Pilot 2–compatible ``.kml`` file."""
    with open(kml_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(kml_text)
    return kml_path


def export_features_as_individual_kml(
    layer: QgsVectorLayer,
    name_field: str,
    output_dir: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    altitude_m: float = 0.0,
) -> List[str]:
    """Export each feature as a DJI Pilot 2–compatible KML. Returns KML paths."""
    written: List[str] = []
    features = list(layer.getFeatures())
    total = len(features)

    for i, feat in enumerate(features, start=1):
        raw_name = feat.attribute(name_field) or f"feature_{i}"
        file_stem = sanitize_filename(str(raw_name))
        out_path = os.path.join(output_dir, f"{file_stem}.kml")

        if os.path.exists(out_path):
            n = 2
            while os.path.exists(os.path.join(output_dir, f"{file_stem}_{n}.kml")):
                n += 1
            out_path = os.path.join(output_dir, f"{file_stem}_{n}.kml")

        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        flat = QgsWkbTypes.flatType(geom.wkbType())

        if QgsWkbTypes.geometryType(flat) == QgsWkbTypes.PolygonGeometry:
            if QgsWkbTypes.isMultiType(flat):
                polys = geom.asMultiPolygon() if hasattr(geom, "asMultiPolygon") else []
                if polys:
                    geom = QgsGeometry.fromPolygonXY(
                        max(polys, key=lambda p: abs(QgsGeometry.fromPolygonXY(p).area()))
                    )
        elif QgsWkbTypes.geometryType(flat) == QgsWkbTypes.LineGeometry:
            if QgsWkbTypes.isMultiType(flat):
                lines = geom.asMultiPolyline()
                if lines:
                    geom = QgsGeometry.fromPolylineXY(max(lines, key=len))
        else:
            continue

        geom_wgs = _reproject_geom_wgs84(geom, layer.crs())
        try:
            kml_text = build_dji_pilot_kml(str(raw_name), geom_wgs, altitude_m=altitude_m)
        except ProcessingError:
            # Skip degenerate clipped slivers rather than aborting the whole export
            continue

        write_dji_kml(out_path, kml_text)
        written.append(out_path)
        if progress_callback and (i % 10 == 0 or i == total):
            progress_callback(i, total, f"Exporting DJI KML ({i}/{total})…")

    if not written and total:
        raise ProcessingError(
            "No DJI-compatible KML files were written. "
            "Features may be too small/degenerate after clipping."
        )
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
    """Red outline + 50% fill, plus scale-dependent XY centroid labels."""
    symbol = QgsFillSymbol.createSimple(
        {
            "color": "255,0,0,128",
            "style": "solid",
            "outline_color": "220,0,0,255",
            "outline_width": "0.4",
            "outline_style": "solid",
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    _apply_xy_centroid_label(layer)
    layer.triggerRepaint()


def _apply_xy_centroid_label(layer: QgsVectorLayer) -> None:
    """Label each grid cell with its XY id; shrinks then hides when zoomed out."""
    try:
        from qgis.core import (
            QgsPalLayerSettings,
            QgsProperty,
            QgsTextFormat,
            QgsUnitTypes,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QFont
    except Exception:
        return

    xy = extract_xy_from_cell_name(layer.name())
    if not xy:
        return

    settings = QgsPalLayerSettings()
    settings.enabled = True
    settings.isExpression = True
    settings.fieldName = "'" + xy.replace("'", "''") + "'"

    # Centre of polygon
    placement = getattr(QgsPalLayerSettings, "OverPoint", None)
    if placement is None:
        placement = getattr(QgsPalLayerSettings, "Centroid", None)
    if placement is not None:
        settings.placement = placement
    if hasattr(settings, "centroidWhole"):
        settings.centroidWhole = True

    # Hide when zoomed out past ~1:40 000
    settings.scaleVisibility = True
    settings.minimumScale = 40000
    settings.maximumScale = 0

    text_format = QgsTextFormat()
    text_format.setSize(8)
    if hasattr(QgsUnitTypes, "RenderPoints"):
        text_format.setSizeUnit(QgsUnitTypes.RenderPoints)
    text_format.setColor(QColor(100, 0, 0))
    font = QFont("Arial")
    font.setBold(True)
    text_format.setFont(font)

    # Shrink label size as the user zooms out
    size_expr = (
        "CASE"
        " WHEN @map_scale < 2000 THEN 12"
        " WHEN @map_scale < 5000 THEN 10"
        " WHEN @map_scale < 10000 THEN 8"
        " WHEN @map_scale < 20000 THEN 6"
        " WHEN @map_scale < 40000 THEN 4"
        " ELSE 0 END"
    )
    size_prop = QgsProperty.fromExpression(size_expr)
    applied_dd = False
    try:
        settings.setDataDefinedProperty(QgsPalLayerSettings.Size, size_prop)
        applied_dd = True
    except Exception:
        pass
    if not applied_dd:
        try:
            dd = text_format.dataDefinedProperties()
            size_key = getattr(QgsTextFormat, "Size", None)
            if size_key is not None:
                dd.setProperty(size_key, size_prop)
                text_format.setDataDefinedProperties(dd)
        except Exception:
            pass

    settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)


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
    area_kml_paths: Optional[List[str]] = None,
    linear_kml_paths: Optional[List[str]] = None,
    hazard_gpx_paths: Optional[List[Tuple[str, str]]] = None,
    replace_area: bool = False,
    replace_linear: bool = False,
    replace_hazards: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """Load exported files into Flight Plan groups.

    Only replaces the subgroup(s) for the mode that was just generated, so a
    failed Linear Search run never wipes an existing Area Search grid.

    ``hazard_gpx_paths`` is a list of ``(path, style)`` where style is
    ``"hazard_point"`` or ``"hazard_line"``.
    """
    safe = sanitize_name(polygon_name)
    root = QgsProject.instance().layerTreeRoot()
    flight_group = get_or_create_group(root, f"Flight Plan_{safe}")
    area_group = get_or_create_group(flight_group, f"Area Search_{safe}")
    linear_group = get_or_create_group(flight_group, f"Linear Search_{safe}")
    hazards_group = get_or_create_group(flight_group, "Potential Hazards")

    area_paths = area_kml_paths or []
    linear_paths = linear_kml_paths or []
    hazard_items = hazard_gpx_paths or []
    grand_total = max(len(area_paths) + len(linear_paths) + len(hazard_items), 1)
    done_base = 0

    if replace_area:
        clear_group_layers(area_group)
        if area_paths:
            def area_cb(done, total, msg):
                if progress_callback:
                    progress_callback(done, grand_total, msg)

            add_kml_files_to_group(
                area_group, area_paths, style="area", progress_callback=area_cb
            )
        done_base = len(area_paths)

    if replace_linear:
        clear_group_layers(linear_group)
        if linear_paths:
            def linear_cb(done, total, msg):
                if progress_callback:
                    progress_callback(done_base + done, grand_total, msg)

            add_kml_files_to_group(
                linear_group, linear_paths, style="linear", progress_callback=linear_cb
            )
        done_base += len(linear_paths)

    if replace_hazards:
        clear_group_layers(hazards_group)
        if hazard_items:
            add_hazard_gpx_to_group(hazards_group, hazard_items)
            if progress_callback:
                progress_callback(grand_total, grand_total, "Potential Hazards loaded.")


def export_layer_as_gpx(
    layer: QgsVectorLayer,
    out_path: str,
    force_track: bool = False,
) -> str:
    """Write a layer to GPX (WGS84). Returns the output path."""
    if layer.featureCount() == 0:
        raise ProcessingError("No features to export to GPX.")

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform_context = QgsProject.instance().transformContext()
    gtype = QgsWkbTypes.geometryType(layer.wkbType())

    if gtype == QgsWkbTypes.PointGeometry:
        tmp = QgsVectorLayer(f"Point?crs={layer.crs().authid()}", layer.name(), "memory")
    else:
        tmp = QgsVectorLayer(
            f"LineString?crs={layer.crs().authid()}", layer.name(), "memory"
        )

    prov = tmp.dataProvider()
    fields = QgsFields()
    fields.append(QgsField("name", QVariant.String))
    fields.append(QgsField("feature_type", QVariant.String))
    fields.append(QgsField("power", QVariant.String))
    fields.append(QgsField("voltage", QVariant.String))
    prov.addAttributes(fields)
    tmp.updateFields()

    name_idx = layer.fields().indexOf("name")
    type_idx = layer.fields().indexOf("feature_type")
    power_idx = layer.fields().indexOf("power")
    volt_idx = layer.fields().indexOf("voltage")

    out_feats = []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue

        def _attrs_for(dst: QgsFeature) -> None:
            dst.setAttribute(
                "name",
                str(feat.attribute(name_idx) if name_idx >= 0 else layer.name()),
            )
            dst.setAttribute(
                "feature_type",
                str(feat.attribute(type_idx) if type_idx >= 0 else ""),
            )
            dst.setAttribute(
                "power", str(feat.attribute(power_idx) if power_idx >= 0 else "")
            )
            dst.setAttribute(
                "voltage", str(feat.attribute(volt_idx) if volt_idx >= 0 else "")
            )

        if gtype == QgsWkbTypes.LineGeometry and QgsWkbTypes.isMultiType(geom.wkbType()):
            for line in geom.asMultiPolyline():
                if len(line) < 2:
                    continue
                nf = QgsFeature(tmp.fields())
                nf.setGeometry(QgsGeometry.fromPolylineXY(line))
                _attrs_for(nf)
                out_feats.append(nf)
        else:
            nf = QgsFeature(tmp.fields())
            nf.setGeometry(QgsGeometry(geom))
            _attrs_for(nf)
            out_feats.append(nf)

    if not out_feats:
        raise ProcessingError("No valid geometries to export to GPX.")
    prov.addFeatures(out_feats)
    tmp.updateExtents()

    if os.path.exists(out_path):
        os.remove(out_path)

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPX"
    opts.fileEncoding = "UTF-8"
    opts.ct = QgsCoordinateTransform(tmp.crs(), wgs84, QgsProject.instance())
    opts.datasourceOptions = ["GPX_USE_EXTENSIONS=YES"]
    if force_track or gtype == QgsWkbTypes.LineGeometry:
        opts.layerOptions = ["FORCE_GPX_TRACK=YES"]

    if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, out_path, transform_context, opts
        )
        err = result[0] if isinstance(result, tuple) else result
        if err != QgsVectorFileWriter.NoError:
            msg = result[2] if isinstance(result, tuple) and len(result) > 2 else str(result)
            raise ProcessingError(f"Failed to write GPX '{out_path}': {msg}")
    else:
        err = QgsVectorFileWriter.writeAsVectorFormat(
            tmp, out_path, "UTF-8", wgs84, "GPX"
        )
        if isinstance(err, tuple):
            err = err[0]
        if err != QgsVectorFileWriter.NoError:
            raise ProcessingError(f"Failed to write GPX '{out_path}'.")

    return out_path


def export_hazards_as_gpx(
    point_layer: QgsVectorLayer,
    line_layer: QgsVectorLayer,
    hazards_dir: str,
) -> List[Tuple[str, str]]:
    """Export pylons + power lines to GPX. Returns list of (path, style_key)."""
    written: List[Tuple[str, str]] = []
    if point_layer is not None and point_layer.featureCount() > 0:
        path = os.path.join(hazards_dir, "Power_Pylons.gpx")
        export_layer_as_gpx(point_layer, path, force_track=False)
        written.append((path, "hazard_point"))
    if line_layer is not None and line_layer.featureCount() > 0:
        path = os.path.join(hazards_dir, "Power_Lines.gpx")
        export_layer_as_gpx(line_layer, path, force_track=True)
        written.append((path, "hazard_line"))
    return written


def _hazard_icon_path() -> str:
    return os.path.join(os.path.dirname(__file__), "icons", "hazard.svg")


def style_hazard_point_layer(layer: QgsVectorLayer) -> None:
    """Warning-triangle hazard marker for pylons / poles."""
    icon = _hazard_icon_path()
    if os.path.exists(icon):
        # QGIS expects forward slashes in SVG paths on Windows
        svg_path = QDir.fromNativeSeparators(icon)
        symbol = QgsMarkerSymbol.createSimple(
            {
                "name": "circle",  # overridden if svg path works via svgMarker
                "color": "255,200,0,220",
                "outline_color": "180,0,0,255",
                "outline_width": "0.6",
                "size": "5",
            }
        )
        try:
            from qgis.core import QgsSvgMarkerSymbolLayer

            svg = QgsSvgMarkerSymbolLayer(svg_path)
            svg.setSize(6)
            symbol.changeSymbolLayer(0, svg)
        except Exception:
            symbol = QgsMarkerSymbol.createSimple(
                {
                    "name": "triangle",
                    "color": "255,200,0,230",
                    "outline_color": "180,0,0,255",
                    "outline_width": "0.8",
                    "size": "5",
                    "angle": "0",
                }
            )
    else:
        symbol = QgsMarkerSymbol.createSimple(
            {
                "name": "triangle",
                "color": "255,200,0,230",
                "outline_color": "180,0,0,255",
                "outline_width": "0.8",
                "size": "5",
            }
        )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def style_hazard_line_layer(layer: QgsVectorLayer) -> None:
    """Orange power-line stroke with hazard markers spaced along the line."""
    try:
        from qgis.core import (
            QgsMarkerLineSymbolLayer,
            QgsSimpleLineSymbolLayer,
            QgsSvgMarkerSymbolLayer,
            QgsUnitTypes,
        )
    except Exception:
        # Fallback: thick dashed orange line
        symbol = QgsLineSymbol.createSimple(
            {
                "line_color": "255,140,0,255",
                "line_width": "1.6",
                "line_style": "dash",
            }
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        return

    line_layer = QgsSimpleLineSymbolLayer.create(
        {
            "line_color": "255,140,0,255",
            "line_width": "1.4",
            "line_style": "solid",
            "capstyle": "round",
            "joinstyle": "round",
        }
    )

    marker_line = QgsMarkerLineSymbolLayer()
    # Place markers at a fixed map-metre interval along the line
    try:
        marker_line.setPlacement(QgsMarkerLineSymbolLayer.Interval)
    except Exception:
        try:
            marker_line.setPlacement(0)  # Interval in older QGIS enums
        except Exception:
            pass
    marker_line.setInterval(60.0)
    try:
        marker_line.setIntervalUnit(QgsUnitTypes.RenderMetersInMapUnits)
    except Exception:
        pass

    icon = _hazard_icon_path()
    if os.path.exists(icon):
        try:
            svg = QgsSvgMarkerSymbolLayer(QDir.fromNativeSeparators(icon))
            svg.setSize(4.5)
            sub = QgsMarkerSymbol()
            sub.changeSymbolLayer(0, svg)
            marker_line.setSubSymbol(sub)
        except Exception:
            sub = QgsMarkerSymbol.createSimple(
                {
                    "name": "triangle",
                    "color": "255,200,0,230",
                    "outline_color": "180,0,0,255",
                    "size": "3.5",
                }
            )
            marker_line.setSubSymbol(sub)
    else:
        sub = QgsMarkerSymbol.createSimple(
            {
                "name": "triangle",
                "color": "255,200,0,230",
                "outline_color": "180,0,0,255",
                "size": "3.5",
            }
        )
        marker_line.setSubSymbol(sub)

    symbol = QgsLineSymbol()
    symbol.changeSymbolLayer(0, line_layer)
    symbol.appendSymbolLayer(marker_line)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def _load_gpx_geometry_layer(gpx_path: str, prefer: str) -> Optional[QgsVectorLayer]:
    """Load the most relevant GPX sublayer (waypoints / tracks / routes)."""
    base = os.path.splitext(os.path.basename(gpx_path))[0]
    candidates = []
    if prefer == "point":
        candidates = ["waypoints", "route_points", "track_points"]
    else:
        candidates = ["tracks", "routes", "waypoints"]

    for layername in candidates:
        uri = f"{gpx_path}|layername={layername}"
        layer = QgsVectorLayer(uri, base, "ogr")
        if layer.isValid() and layer.featureCount() > 0:
            return layer

    # Fallback: plain path
    layer = QgsVectorLayer(gpx_path, base, "ogr")
    if layer.isValid() and layer.featureCount() > 0:
        return layer
    return None


def add_hazard_gpx_to_group(
    group: QgsLayerTreeGroup,
    hazard_items: List[Tuple[str, str]],
) -> int:
    """Add hazard GPX files under Potential Hazards with appropriate symbology."""
    project = QgsProject.instance()
    added = 0
    layers_to_add: List[QgsVectorLayer] = []
    styles: List[str] = []

    for path, style in hazard_items:
        prefer = "point" if style == "hazard_point" else "line"
        layer = _load_gpx_geometry_layer(path, prefer=prefer)
        if layer is None:
            continue
        # Friendly legend names
        if style == "hazard_point":
            layer.setName("Power Pylons / Poles")
        else:
            layer.setName("Power Lines")
        layers_to_add.append(layer)
        styles.append(style)

    if not layers_to_add:
        return 0

    project.addMapLayers(layers_to_add, False)
    for lyr, style in zip(layers_to_add, styles):
        if style == "hazard_point":
            style_hazard_point_layer(lyr)
        else:
            style_hazard_line_layer(lyr)
        group.addLayer(lyr)
        added += 1
    return added
