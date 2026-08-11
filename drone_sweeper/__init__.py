# -*- coding: utf-8 -*-
"""
Drone Sweeper — QGIS plugin entry point.
"""


def classFactory(iface):
    """Load DroneSweeperPlugin when QGIS starts.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    from .main_plugin import DroneSweeperPlugin

    return DroneSweeperPlugin(iface)
