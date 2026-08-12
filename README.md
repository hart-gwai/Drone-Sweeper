# Drone Sweeper

QGIS 3 plugin for Search and Rescue (SAR) drone flight planning.

Generate **MGRS-aligned area-search grids**, pull **OSM trails/waterways**, flag **power-line hazards**, and export **DJI Pilot 2–compatible KML** (plus hazard **GPX**) beside your saved QGIS project.

**Stable release:** [v1.1.0](https://github.com/hart-gwai/Drone-Sweeper/releases/tag/v1.1.0)

## Features

| Mode | What it does | Needs internet? |
|------|----------------|-----------------|
| **Area Search** | Builds a metre grid snapped to MGRS/UTM lines; exports one KML per cell; loads layers into QGIS | No (grid works offline) |
| **Linear Search** | Fetches OSM paths/trails and waterways, clips to AOI, names unnamed features with MGRS | Yes (Overpass API) |
| **Potential Hazards** *(with Area Search)* | Best-effort OSM power lines & pylons → GPX + hazard symbology | Yes (skipped gracefully if offline) |

### Area Search details
- Default cell size **100 × 100 m** (must be a **multiple of 10 m**)
- Cell / file names: `PolygonName_A1_50Q KK 123456` (100 m / 6-digit MGRS)
- Red fill at 50% opacity; **XY** labels at cell centres (shrink / hide when zoomed out)
- Output folder: `Flight Plan_<name>/Area Search_<name>/`

### Linear Search details
- Trails: `path`, `footway`, `track`, `steps`, `bridleway`, `cycleway`
- Waterways: `stream`, `river`, `canal`, `drain`
- Unnamed features → e.g. `Stream near 50Q KK 123456`
- Thick multi-colour line symbology
- Output folder: `Flight Plan_<name>/Linear Search_<name>/`

### Potential Hazards
- OSM `power=line|minor_line|cable` and `power=tower|pole|portal`
- Saved as GPX under `Flight Plan_<name>/Potential Hazards/`
- Warning markers on pylons; orange lines with spaced hazard symbols
- **Awareness only** — not a certified obstacle database; always verify on site

## Requirements
- **QGIS 3.16+** (tested on QGIS 3.x)
- Saved `.qgz` / `.qgs` project (outputs are written next to the project file)
- Polygon AOI layer with a **valid CRS** that matches the coordinates (e.g. WGS84 / EPSG:4326 or HK 1980 / EPSG:2326)

## Install

### From ZIP (recommended for testers)
1. Download [`Drone_Sweeper_v1.1.0.zip`](https://github.com/hart-gwai/Drone-Sweeper/releases/download/v1.1.0/Drone_Sweeper_v1.1.0.zip)
2. QGIS → **Plugins → Manage and Install Plugins → Install from ZIP**
3. Enable **Drone Sweeper**

### From source (developers)
```bash
# Windows example — junction into the default QGIS profile
python install_to_qgis.py --symlink
```
Or copy/symlink the `drone_sweeper/` folder into:
`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`

Restart QGIS (or use Plugin Reloader), then enable the plugin.

## Quick start
1. Save your QGIS project
2. Add / select an AOI **polygon** layer
3. Open **Plugins → Drone Sweeper** (or the toolbar icon)
4. Choose mode:
   - **Area Search (Grid)** → set grid size → **Generate Area Search**
   - **Linear Search (OSM…)** → toggle trails/waterways → **Generate Linear Search**
5. Find KMLs/GPXs next to the project under `Flight Plan_<layer name>/`
6. Import KMLs into **DJI Pilot 2** as needed

## Project / layer CRS (short guide)
- **Layer CRS** = how that layer’s coordinates are stored  
- **Project CRS** = how QGIS draws the map (on-the-fly reprojection is normal)
- **Lat/lon WGS84 (EPSG:4326)** — GPS, OSM, KML, DJI  
- **HK 1980 Grid (EPSG:2326)** — local Hong Kong metre mapping  
- Drone Sweeper reprojects as needed for MGRS grids and WGS84 exports  
- Never “Set CRS” to “fix” wrong coordinates — **reproject/export** instead if you need to convert

## Output layout
```
<project folder>/
  Flight Plan_<AOI>/
    Area Search_<AOI>/          # one .kml per grid cell
    Linear Search_<AOI>/        # one .kml per linear feature
    Potential Hazards/          # Power_Pylons.gpx, Power_Lines.gpx
```

Layer panel groups mirror that structure under **Flight Plan_<AOI>**.

## Repo layout
```
drone_sweeper/           # installable QGIS plugin
  main_plugin.py
  drone_sweeper_dialog.py
  processing_engine.py
  osm_fetcher.py
  mgrs_converter.py
  metadata.txt
  icons/
install_to_qgis.py       # helper to copy/junction into QGIS plugins folder
```

## Disclaimer
OpenStreetMap coverage varies. Hazard and trail layers are for **operational awareness** only. Confirm wires, terrain, and airspace with local knowledge, visual recon, and authoritative sources before flight.

## License
See repository license (if added). OSM data © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright)).
