# -*- coding: utf-8 -*-
"""
Lightweight Lat/Lon → MGRS converter (pure Python, no pip dependencies).

Produces strings such as:
  50Q KK 123456
  50Q KK 12345 67890
"""

from __future__ import annotations

import math
from typing import Tuple

# Ellipsoid constants (WGS84)
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2.0 - _F)
_EP2 = _E2 / (1.0 - _E2)
_K0 = 0.9996

# MGRS 100 km square letter sets
_SET_COL_LETTERS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_SET_ROW_LETTERS = ("ABCDEFGHJKLMNPQRSTUV", "FGHJKLMNPQRSTUVABCDE")

# Latitude band letters (C–X, excluding I and O)
_LAT_BANDS = "CDEFGHJKLMNPQRSTUVWX"


def _latlon_to_utm(lat: float, lon: float) -> Tuple[int, str, float, float]:
    """Convert WGS84 lat/lon to UTM zone, hemisphere, easting, northing."""
    if not (-80.0 <= lat <= 84.0):
        raise ValueError("Latitude out of MGRS/UTM range (-80 to 84)")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("Longitude out of range (-180 to 180)")

    zone = int((lon + 180.0) // 6) + 1

    # Special zones for Norway / Svalbard
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        zone = 32
    if 72.0 <= lat < 84.0:
        if 0.0 <= lon < 9.0:
            zone = 31
        elif 9.0 <= lon < 21.0:
            zone = 33
        elif 21.0 <= lon < 33.0:
            zone = 35
        elif 33.0 <= lon < 42.0:
            zone = 37

    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)

    n = _A / math.sqrt(1.0 - _E2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r) ** 2
    c = _EP2 * math.cos(lat_r) ** 2
    a = math.cos(lat_r) * (lon_r - lon0)

    m = _A * (
        (1.0 - _E2 / 4.0 - 3.0 * _E2 ** 2 / 64.0 - 5.0 * _E2 ** 3 / 256.0) * lat_r
        - (3.0 * _E2 / 8.0 + 3.0 * _E2 ** 2 / 32.0 + 45.0 * _E2 ** 3 / 1024.0)
        * math.sin(2.0 * lat_r)
        + (15.0 * _E2 ** 2 / 256.0 + 45.0 * _E2 ** 3 / 1024.0) * math.sin(4.0 * lat_r)
        - (35.0 * _E2 ** 3 / 3072.0) * math.sin(6.0 * lat_r)
    )

    easting = (
        _K0
        * n
        * (
            a
            + (1.0 - t + c) * a ** 3 / 6.0
            + (5.0 - 18.0 * t + t ** 2 + 72.0 * c - 58.0 * _EP2) * a ** 5 / 120.0
        )
        + 500000.0
    )

    northing = _K0 * (
        m
        + n
        * math.tan(lat_r)
        * (
            a ** 2 / 2.0
            + (5.0 - t + 9.0 * c + 4.0 * c ** 2) * a ** 4 / 24.0
            + (61.0 - 58.0 * t + t ** 2 + 600.0 * c - 330.0 * _EP2) * a ** 6 / 720.0
        )
    )

    hemisphere = "N" if lat >= 0 else "S"
    if lat < 0:
        northing += 10000000.0

    return zone, hemisphere, easting, northing


def _latitude_band(lat: float) -> str:
    """Return MGRS latitude band letter for a given latitude."""
    if lat < -80.0 or lat > 84.0:
        raise ValueError("Latitude out of MGRS band range")
    # Band C starts at -80; each band is 8 degrees, except X (12°)
    idx = int((lat + 80.0) // 8)
    if idx >= len(_LAT_BANDS):
        idx = len(_LAT_BANDS) - 1
    return _LAT_BANDS[idx]


def _get_100k_letters(zone: int, easting: float, northing: float) -> str:
    """Return the two-letter 100 km square designator."""
    set_idx = (zone - 1) % 3
    col_letters = _SET_COL_LETTERS[set_idx]
    row_letters = _SET_ROW_LETTERS[(zone - 1) % 2]

    # Reduce to within-zone 100 km grid
    e100 = int(easting // 100000) - 1  # columns A–H / J–R / S–Z map from 1..8
    n100 = int(northing // 100000) % 20

    if e100 < 0 or e100 >= len(col_letters):
        # Clamp edge cases near zone boundaries
        e100 = max(0, min(len(col_letters) - 1, e100))

    return col_letters[e100] + row_letters[n100]


def latlon_to_mgrs(lat: float, lon: float, precision: int = 5, spaced: bool = True) -> str:
    """Convert WGS84 latitude/longitude to an MGRS string.

    :param lat: Latitude in decimal degrees
    :param lon: Longitude in decimal degrees
    :param precision: Digits per easting/northing component (0–5).
                      5 → 1 m, 4 → 10 m, 3 → 100 m, etc.
    :param spaced: If True, insert spaces for readability
                   (e.g. ``50Q KK 12345 67890``).
    :return: MGRS grid reference string
    """
    if precision < 0 or precision > 5:
        raise ValueError("precision must be between 0 and 5")

    zone, _hem, easting, northing = _latlon_to_utm(lat, lon)
    band = _latitude_band(lat)
    square = _get_100k_letters(zone, easting, northing)

    # Residual meters within the 100 km square
    e_res = int(easting) % 100000
    n_res = int(northing) % 100000

    if precision == 0:
        east_str = north_str = ""
    else:
        divisor = 10 ** (5 - precision)
        east_str = f"{e_res // divisor:0{precision}d}"
        north_str = f"{n_res // divisor:0{precision}d}"

    zone_band = f"{zone:02d}{band}"

    if spaced:
        parts = [zone_band, square]
        if precision > 0:
            # Compact form for precision 3 (common SAR usage): "50Q KK 123456"
            if precision == 3:
                parts.append(f"{east_str}{north_str}")
            else:
                parts.append(east_str)
                parts.append(north_str)
        return " ".join(parts)

    return f"{zone_band}{square}{east_str}{north_str}"


def format_feature_name(feature_type: str, lat: float, lon: float, precision: int = 3) -> str:
    """Build a nameless-feature label: ``<Type> near <MGRS>``."""
    mgrs = latlon_to_mgrs(lat, lon, precision=precision, spaced=True)
    return f"{feature_type} near {mgrs}"
