# -*- coding: utf-8 -*-
"""
Copy or symlink the drone_sweeper plugin into the active QGIS 3 profile.

Examples:
  python install_to_qgis.py
  python install_to_qgis.py --symlink
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys


def default_plugins_dir() -> str:
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return os.path.join(appdata, "QGIS", "QGIS3", "profiles", "default", "python", "plugins")
    if sys.platform == "darwin":
        return os.path.join(
            home, "Library", "Application Support", "QGIS", "QGIS3", "profiles", "default", "python", "plugins"
        )
    return os.path.join(home, ".local", "share", "QGIS", "QGIS3", "profiles", "default", "python", "plugins")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Drone Sweeper into QGIS 3 plugins folder")
    parser.add_argument("--symlink", action="store_true", help="Create a directory junction/symlink instead of copying")
    parser.add_argument("--target", default=None, help="Override QGIS plugins directory")
    args = parser.parse_args()

    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drone_sweeper")
    if not os.path.isdir(src) or not os.path.isfile(os.path.join(src, "metadata.txt")):
        print("Could not find drone_sweeper/metadata.txt next to this script.", file=sys.stderr)
        return 1

    plugins = args.target or default_plugins_dir()
    os.makedirs(plugins, exist_ok=True)
    dest = os.path.join(plugins, "drone_sweeper")

    if os.path.lexists(dest):
        if os.path.islink(dest) or (sys.platform.startswith("win") and os.path.isdir(dest)):
            # Remove previous install
            if os.path.islink(dest):
                os.unlink(dest)
            else:
                shutil.rmtree(dest)
        else:
            shutil.rmtree(dest)

    if args.symlink:
        if sys.platform.startswith("win"):
            # Directory junction does not require admin
            import subprocess

            subprocess.check_call(["cmd", "/c", "mklink", "/J", dest, src])
        else:
            os.symlink(src, dest, target_is_directory=True)
        print(f"Linked:\n  {dest}\n→ {src}")
    else:
        shutil.copytree(src, dest)
        print(f"Copied plugin to:\n  {dest}")

    print("Restart QGIS, then enable “Drone Sweeper” under Plugins → Manage and Install Plugins → Installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
