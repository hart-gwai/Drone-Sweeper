# -*- coding: utf-8 -*-
"""
OpenStreetMap Overpass API fetcher and AOI clipper for Drone Sweeper.
"""

from __future__ import annotations

import json
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

TRAIL_HIGHWAYS = ("path", "footway", "track", "steps", "bridleway", "cycleway")
WATERWAY_TYPES = ("stream", "river", "canal", "drain")
POWER_LINE_TYPES = ("line", "minor_line", "cable")
POWER_POINT_TYPES = ("tower", "pole", "portal")


class OsmFetchError(Exception):
    """Raised when an Overpass query or parse step fails."""


def _is_finite_extent(extent: QgsRectangle) -> bool:
    """True if rectangle has finite, non-empty coordinates."""
    if extent is None or extent.isEmpty():
        return False
    vals = (
        extent.xMinimum(),
        extent.xMaximum(),
        extent.yMinimum(),
        extent.yMaximum(),
    )
    return all(math.isfinite(v) for v in vals)


def layer_feature_extent(layer: QgsVectorLayer) -> QgsRectangle:
    """Compute extent from features (more reliable than cached layer.extent())."""
    rect = QgsRectangle()
    rect.setNull()
    count = 0
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        gext = geom.boundingBox()
        if not _is_finite_extent(gext):
            continue
        if rect.isNull():
            rect = QgsRectangle(gext)
        else:
            rect.combineExtentWith(gext)
        count += 1
    if count == 0 or not _is_finite_extent(rect):
        raise OsmFetchError(
            f"Layer “{layer.name()}” has no valid geometries to build a bounding box from."
        )
    return rect


def polygon_layer_bbox_wgs84(layer: QgsVectorLayer) -> Tuple[float, float, float, float]:
    """Return (south, west, north, east) WGS84 bbox covering all features.

    Rebuilds extent from features and validates CRS/transform so invalid
    cached extents (inf/nan — common with some KML/OGR layers) do not crash.
    """
    src_crs = layer.crs()
    if not src_crs.isValid():
        raise OsmFetchError(
            f"Layer “{layer.name()}” has no valid coordinate reference system. "
            "Set the layer CRS (e.g. EPSG:4326 or a local projected CRS) and try again."
        )

    # Prefer feature-derived extent; fall back to layer.extent() only if finite
    try:
        extent = layer_feature_extent(layer)
    except OsmFetchError:
        extent = layer.extent()
        if not _is_finite_extent(extent):
            raise

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if src_crs != wgs84:
        try:
            xform = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
            # Transform corners individually for robustness with geographic/projected mixes
            extent = xform.transformBoundingBox(extent)
        except QgsCsException as exc:
            raise OsmFetchError(
                f"Could not reproject “{layer.name()}” ({src_crs.authid() or src_crs.description()}) "
                f"to WGS84 for the OSM query.\n{exc}\n\n"
                "Check that the layer CRS matches the data, or reproject/export the AOI to EPSG:4326."
            ) from exc

    if not _is_finite_extent(extent):
        raise OsmFetchError(
            f"WGS84 bounding box for “{layer.name()}” is invalid (infinite or empty). "
            "The layer CRS may not match the geometry coordinates."
        )

    south, west = extent.yMinimum(), extent.xMinimum()
    north, east = extent.yMaximum(), extent.xMaximum()

    # Sanity: Overpass needs geographic degrees
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise OsmFetchError(
            f"Latitude out of range after transform ({south}, {north}). "
            "The AOI layer CRS is likely wrong for its coordinates."
        )
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        # Allow slight wrap padding later; still flag clearly broken values
        if abs(west) > 360 or abs(east) > 360:
            raise OsmFetchError(
                f"Longitude out of range after transform ({west}, {east}). "
                "The AOI layer CRS is likely wrong for its coordinates."
            )

    return south, west, north, east


def _padded_bbox(south: float, west: float, north: float, east: float) -> str:
    pad = 0.0005  # ~50 m padding to catch edge crossings
    return f"{south - pad},{west - pad},{north + pad},{east + pad}"


def build_overpass_query(
    south: float,
    west: float,
    north: float,
    east: float,
    fetch_trails: bool = True,
    fetch_waterways: bool = True,
) -> str:
    """Build an Overpass QL query for trails and/or waterways in a bbox."""
    if not fetch_trails and not fetch_waterways:
        raise OsmFetchError("At least one OSM feature type must be enabled.")

    bbox = _padded_bbox(south, west, north, east)
    parts: List[str] = []
    if fetch_trails:
        highway_re = "|".join(TRAIL_HIGHWAYS)
        parts.append(f'way["highway"~"{highway_re}"]({bbox});')
    if fetch_waterways:
        water_re = "|".join(WATERWAY_TYPES)
        parts.append(f'way["waterway"~"{water_re}"]({bbox});')

    body = "\n  ".join(parts)
    return f"""
[out:json][timeout:90];
(
  {body}
);
out body;
>;
out skel qt;
""".strip()


def build_hazards_overpass_query(
    south: float,
    west: float,
    north: float,
    east: float,
) -> str:
    """Overpass QL for power lines and pylons/poles (drone obstacle awareness)."""
    bbox = _padded_bbox(south, west, north, east)
    line_re = "|".join(POWER_LINE_TYPES)
    point_re = "|".join(POWER_POINT_TYPES)
    return f"""
[out:json][timeout:90];
(
  way["power"~"{line_re}"]({bbox});
  node["power"~"{point_re}"]({bbox});
);
out body;
>;
out skel qt;
""".strip()


def _post_overpass(query: str, timeout_ms: int = 120000) -> dict:
    """POST an Overpass query using QgsNetworkAccessManager (blocking)."""
    from qgis.PyQt.QtCore import QEventLoop, QTimer
    from qgis.PyQt.QtNetwork import QNetworkReply

    nam = QgsNetworkAccessManager.instance()
    request = QNetworkRequest(QUrl(OVERPASS_URL))
    request.setHeader(
        QNetworkRequest.ContentTypeHeader,
        "application/x-www-form-urlencoded",
    )
    payload = ("data=" + query).encode("utf-8")

    loop = QEventLoop()
    reply = nam.post(request, payload)

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    reply.finished.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec_()

    if not reply.isFinished():
        reply.abort()
        reply.deleteLater()
        raise OsmFetchError("Overpass API request timed out.")

    err = reply.error()
    raw = bytes(reply.readAll())
    reply.deleteLater()

    if err != QNetworkReply.NoError:
        raise OsmFetchError(f"Overpass network error: {reply.errorString()}")

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OsmFetchError(f"Invalid Overpass JSON response: {exc}") from exc


def _fallback_urllib(query: str, timeout_s: int = 90) -> dict:
    """Fallback HTTP POST via urllib if network manager is unavailable."""
    import urllib.error
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OsmFetchError(f"Overpass request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OsmFetchError(f"Invalid Overpass JSON response: {exc}") from exc


def fetch_overpass_json(
    query: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Execute Overpass query; prefer QgsNetworkAccessManager, else urllib."""
    if progress_callback:
        progress_callback("Querying OpenStreetMap Overpass API…")
    try:
        return _post_overpass(query)
    except OsmFetchError as primary:
        try:
            return _fallback_urllib(query)
        except OsmFetchError:
            raise primary


def _nodes_index(elements: Sequence[dict]) -> Dict[int, Tuple[float, float]]:
    """Map node id → (lon, lat)."""
    nodes: Dict[int, Tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node":
            nodes[el["id"]] = (float(el["lon"]), float(el["lat"]))
    return nodes


def _way_to_geometry(
    way: dict,
    nodes: Dict[int, Tuple[float, float]],
) -> Optional[QgsGeometry]:
    """Build a LineString geometry from an OSM way in EPSG:4326."""
    coords: List[QgsPointXY] = []
    for nid in way.get("nodes", []):
        if nid not in nodes:
            continue
        lon, lat = nodes[nid]
        coords.append(QgsPointXY(lon, lat))
    if len(coords) < 2:
        return None
    return QgsGeometry.fromPolylineXY(coords)


def _classify_way(tags: dict) -> Optional[Tuple[str, str]]:
    """Return (feature_type_label, category) or None if unsupported."""
    highway = tags.get("highway")
    waterway = tags.get("waterway")
    if highway in TRAIL_HIGHWAYS:
        return "Path", "trail"
    if waterway in WATERWAY_TYPES:
        # Prefer "Stream" / "River" wording for SAR labels
        label = waterway.capitalize()
        if waterway == "drain":
            label = "Drain"
        elif waterway == "canal":
            label = "Canal"
        return label, "waterway"
    return None


def osm_json_to_memory_layer(
    osm_data: dict,
    layer_name: str = "OSM Linear Features",
) -> QgsVectorLayer:
    """Convert Overpass JSON elements into a WGS84 LineString memory layer."""
    layer = QgsVectorLayer("LineString?crs=EPSG:4326", layer_name, "memory")
    provider = layer.dataProvider()

    fields = QgsFields()
    fields.append(QgsField("osm_id", QVariant.LongLong))
    fields.append(QgsField("name", QVariant.String))
    fields.append(QgsField("feature_type", QVariant.String))
    fields.append(QgsField("category", QVariant.String))
    fields.append(QgsField("highway", QVariant.String))
    fields.append(QgsField("waterway", QVariant.String))
    provider.addAttributes(fields)
    layer.updateFields()

    elements = osm_data.get("elements", [])
    nodes = _nodes_index(elements)
    features: List[QgsFeature] = []

    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {}) or {}
        classified = _classify_way(tags)
        if not classified:
            continue
        feature_type, category = classified
        geom = _way_to_geometry(el, nodes)
        if geom is None or geom.isEmpty():
            continue

        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttribute("osm_id", el.get("id"))
        feat.setAttribute("name", tags.get("name") or "")
        feat.setAttribute("feature_type", feature_type)
        feat.setAttribute("category", category)
        feat.setAttribute("highway", tags.get("highway") or "")
        feat.setAttribute("waterway", tags.get("waterway") or "")
        features.append(feat)

    if features:
        provider.addFeatures(features)
    layer.updateExtents()
    return layer


def union_aoi_geometry(layer: QgsVectorLayer) -> QgsGeometry:
    """Dissolve/union all polygon features into a single AOI geometry."""
    geoms: List[QgsGeometry] = []
    for feat in layer.getFeatures():
        g = feat.geometry()
        if g and not g.isEmpty():
            geoms.append(QgsGeometry(g))
    if not geoms:
        raise OsmFetchError("Input polygon layer has no geometries.")
    result = geoms[0]
    for g in geoms[1:]:
        result = result.combine(g)
    return result


def transform_geometry(
    geom: QgsGeometry,
    src_crs: QgsCoordinateReferenceSystem,
    dst_crs: QgsCoordinateReferenceSystem,
) -> QgsGeometry:
    """Return a deep-copied geometry transformed to dst_crs."""
    out = QgsGeometry(geom)
    if src_crs == dst_crs:
        return out
    xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
    out.transform(xform)
    return out


def _extract_line_parts(geom: QgsGeometry) -> List[QgsGeometry]:
    """Explode any geometry into individual LineString parts."""
    if geom is None or geom.isEmpty():
        return []
    parts: List[QgsGeometry] = []
    wkb = geom.wkbType()
    gtype = QgsWkbTypes.geometryType(wkb)

    if gtype == QgsWkbTypes.LineGeometry:
        if QgsWkbTypes.isMultiType(wkb):
            for line in geom.asMultiPolyline():
                if len(line) >= 2:
                    parts.append(QgsGeometry.fromPolylineXY(line))
        else:
            parts.append(QgsGeometry(geom))
        return parts

    # GeometryCollection or unexpected types: walk collection if available
    try:
        collection = geom.asGeometryCollection()
    except Exception:
        collection = []
    for part in collection:
        parts.extend(_extract_line_parts(part))
    return parts


def clip_lines_to_polygon(
    line_layer: QgsVectorLayer,
    aoi_geom_src: QgsGeometry,
    aoi_crs: QgsCoordinateReferenceSystem,
) -> QgsVectorLayer:
    """Clip linear features to the AOI polygon; output stays in line_layer CRS."""
    line_crs = line_layer.crs()
    aoi = transform_geometry(aoi_geom_src, aoi_crs, line_crs)

    out = QgsVectorLayer(
        f"LineString?crs={line_crs.authid()}",
        f"{line_layer.name()} (clipped)",
        "memory",
    )
    provider = out.dataProvider()
    provider.addAttributes(line_layer.fields())
    out.updateFields()

    clipped_feats: List[QgsFeature] = []
    for feat in line_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if not geom.intersects(aoi):
            continue
        clipped = geom.intersection(aoi)
        for part in _extract_line_parts(clipped):
            new_feat = QgsFeature(out.fields())
            new_feat.setGeometry(part)
            new_feat.setAttributes(feat.attributes())
            clipped_feats.append(new_feat)

    if clipped_feats:
        provider.addFeatures(clipped_feats)
    out.updateExtents()
    return out


def line_midpoint_xy(geom: QgsGeometry) -> Optional[QgsPointXY]:
    """Return the 2D midpoint measured along the line length."""
    if geom is None or geom.isEmpty():
        return None

    # Prefer length-parameterized interpolation when available
    try:
        length = geom.length()
        if length > 0:
            mid = geom.interpolate(length / 2.0)
            if mid and not mid.isEmpty():
                return mid.asPoint()
    except Exception:
        pass

    # Fallback: average of vertices
    try:
        if QgsWkbTypes.isMultiType(geom.wkbType()):
            pts = []
            for part in geom.asGeometryCollection():
                pts.extend(part.asPolyline())
        else:
            pts = geom.asPolyline()
        if not pts:
            return None
        mid_idx = len(pts) // 2
        return pts[mid_idx]
    except Exception:
        return None


def fetch_and_clip_osm(
    aoi_layer: QgsVectorLayer,
    fetch_trails: bool,
    fetch_waterways: bool,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> QgsVectorLayer:
    """Full pipeline: bbox → Overpass → memory layer → clip to AOI."""
    south, west, north, east = polygon_layer_bbox_wgs84(aoi_layer)
    query = build_overpass_query(
        south, west, north, east, fetch_trails, fetch_waterways
    )
    data = fetch_overpass_json(query, progress_callback=progress_callback)
    if progress_callback:
        progress_callback("Building OSM vector layer…")
    raw_layer = osm_json_to_memory_layer(data, "OSM Linear Features")
    aoi_geom = union_aoi_geometry(aoi_layer)
    if progress_callback:
        progress_callback("Clipping OSM features to AOI…")
    return clip_lines_to_polygon(raw_layer, aoi_geom, aoi_layer.crs())


def _classify_power_tags(tags: dict) -> Optional[Tuple[str, str, str]]:
    """Return (label, geom_kind, power_value) where geom_kind is 'point' or 'line'."""
    power = (tags.get("power") or "").strip()
    if power in POWER_POINT_TYPES:
        label = {"tower": "Pylon", "pole": "Power Pole", "portal": "Power Portal"}.get(
            power, power.capitalize()
        )
        return label, "point", power
    if power in POWER_LINE_TYPES:
        label = {
            "line": "Power Line",
            "minor_line": "Minor Power Line",
            "cable": "Power Cable",
        }.get(power, "Power Line")
        return label, "line", power
    return None


def osm_hazards_to_memory_layers(
    osm_data: dict,
) -> Tuple[QgsVectorLayer, QgsVectorLayer]:
    """Convert Overpass JSON into WGS84 point + line memory layers for power hazards."""
    point_layer = QgsVectorLayer("Point?crs=EPSG:4326", "Power Pylons", "memory")
    line_layer = QgsVectorLayer("LineString?crs=EPSG:4326", "Power Lines", "memory")

    def _init_fields(layer: QgsVectorLayer) -> None:
        fields = QgsFields()
        fields.append(QgsField("osm_id", QVariant.LongLong))
        fields.append(QgsField("name", QVariant.String))
        fields.append(QgsField("feature_type", QVariant.String))
        fields.append(QgsField("power", QVariant.String))
        fields.append(QgsField("voltage", QVariant.String))
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()

    _init_fields(point_layer)
    _init_fields(line_layer)

    elements = osm_data.get("elements", [])
    nodes = _nodes_index(elements)
    point_feats: List[QgsFeature] = []
    line_feats: List[QgsFeature] = []

    for el in elements:
        tags = el.get("tags", {}) or {}
        classified = _classify_power_tags(tags)
        if not classified:
            continue
        label, kind, power = classified
        name = tags.get("name") or label
        voltage = str(tags.get("voltage") or "")

        if kind == "point" and el.get("type") == "node":
            lon = el.get("lon")
            lat = el.get("lat")
            if lon is None or lat is None:
                continue
            feat = QgsFeature(point_layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(lon), float(lat))))
            feat.setAttribute("osm_id", el.get("id"))
            feat.setAttribute("name", name)
            feat.setAttribute("feature_type", label)
            feat.setAttribute("power", power)
            feat.setAttribute("voltage", voltage)
            point_feats.append(feat)
        elif kind == "line" and el.get("type") == "way":
            geom = _way_to_geometry(el, nodes)
            if geom is None or geom.isEmpty():
                continue
            feat = QgsFeature(line_layer.fields())
            feat.setGeometry(geom)
            feat.setAttribute("osm_id", el.get("id"))
            feat.setAttribute("name", name)
            feat.setAttribute("feature_type", label)
            feat.setAttribute("power", power)
            feat.setAttribute("voltage", voltage)
            line_feats.append(feat)

    if point_feats:
        point_layer.dataProvider().addFeatures(point_feats)
    if line_feats:
        line_layer.dataProvider().addFeatures(line_feats)
    point_layer.updateExtents()
    line_layer.updateExtents()
    return point_layer, line_layer


def clip_points_to_polygon(
    point_layer: QgsVectorLayer,
    aoi_geom_src: QgsGeometry,
    aoi_crs: QgsCoordinateReferenceSystem,
) -> QgsVectorLayer:
    """Keep points that fall inside the AOI."""
    pt_crs = point_layer.crs()
    aoi = transform_geometry(aoi_geom_src, aoi_crs, pt_crs)
    out = QgsVectorLayer(
        f"Point?crs={pt_crs.authid()}",
        f"{point_layer.name()} (clipped)",
        "memory",
    )
    provider = out.dataProvider()
    provider.addAttributes(point_layer.fields())
    out.updateFields()
    kept: List[QgsFeature] = []
    for feat in point_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if geom.within(aoi) or geom.intersects(aoi):
            new_feat = QgsFeature(out.fields())
            new_feat.setGeometry(QgsGeometry(geom))
            new_feat.setAttributes(feat.attributes())
            kept.append(new_feat)
    if kept:
        provider.addFeatures(kept)
    out.updateExtents()
    return out


def fetch_and_clip_hazards(
    aoi_layer: QgsVectorLayer,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[QgsVectorLayer, QgsVectorLayer]:
    """Fetch OSM power lines/pylons and clip to AOI. Returns (points, lines)."""
    south, west, north, east = polygon_layer_bbox_wgs84(aoi_layer)
    query = build_hazards_overpass_query(south, west, north, east)
    data = fetch_overpass_json(query, progress_callback=progress_callback)
    if progress_callback:
        progress_callback("Building power hazard layers…")
    points, lines = osm_hazards_to_memory_layers(data)
    aoi_geom = union_aoi_geometry(aoi_layer)
    if progress_callback:
        progress_callback("Clipping power hazards to AOI…")
    clipped_points = clip_points_to_polygon(points, aoi_geom, aoi_layer.crs())
    clipped_lines = clip_lines_to_polygon(lines, aoi_geom, aoi_layer.crs())
    return clipped_points, clipped_lines
