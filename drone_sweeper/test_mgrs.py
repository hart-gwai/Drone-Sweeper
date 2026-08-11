# -*- coding: utf-8 -*-
"""
Self-test for mgrs_converter (runs without QGIS).

Usage (from plugin root, with system Python):
  python test_mgrs.py
"""

from mgrs_converter import format_feature_name, latlon_to_mgrs


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def col_index_to_letters(index: int) -> str:
    result = []
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def test_mgrs_known_points():
    # Manila area ~ 51P
    mgrs = latlon_to_mgrs(14.5995, 120.9842, precision=3, spaced=True)
    _assert(mgrs.startswith("51P"), f"Expected zone/band 51P…, got {mgrs}")
    _assert(" " in mgrs, f"Expected spaced MGRS, got {mgrs}")

    # Southern hemisphere — Sydney ~ 56H
    mgrs_s = latlon_to_mgrs(-33.8688, 151.2093, precision=5, spaced=True)
    _assert(mgrs_s.startswith("56H"), f"Expected 56H… for Sydney, got {mgrs_s}")

    named = format_feature_name("Stream", 14.5995, 120.9842, precision=3)
    _assert(named.startswith("Stream near "), named)
    print("MGRS tests OK:")
    print(" ", mgrs)
    print(" ", mgrs_s)
    print(" ", named)


def test_columns():
    _assert(col_index_to_letters(1) == "A", "1→A")
    _assert(col_index_to_letters(26) == "Z", "26→Z")
    _assert(col_index_to_letters(27) == "AA", "27→AA")
    _assert(col_index_to_letters(28) == "AB", "28→AB")
    print("Column naming OK: A, Z, AA, AB")


if __name__ == "__main__":
    test_columns()
    test_mgrs_known_points()
    print("All self-tests passed.")
