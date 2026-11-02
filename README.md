# Drone Sweeper

QGIS 3 plugin for Search and Rescue (SAR) drone flight planning.

Generate **MGRS-aligned area-search grids**, pull **OSM trails/waterways**, flag **power-line hazards**, and export **DJI Pilot 2–compatible KML** (plus hazard **GPX**) beside your saved QGIS project.

**Stable release:** [v1.2.2](https://github.com/hart-gwai/Drone-Sweeper/releases/tag/v1.2.2)

## Features

| Mode | What it does | Needs internet? |
|------|----------------|-----------------|
| **Area Search** | Builds a metre grid snapped to MGRS/UTM lines; exports one multi-cell KML; loads layer into QGIS | No (grid works offline) |
| **Linear Search** | Fetches OSM paths/trails and waterways, clips to AOI, exports two consolidated KMLs | Yes (Overpass API) |
| **Potential Hazards** *(with Area Search)* | Best-effort OSM power lines & pylons → GPX + hazard symbology | Yes (skipped gracefully if offline) |

### Area Search details
- Default cell size **100 × 100 m** (must be a **multiple of 10 m**)
- One KML/layer: `Area Search_<polygon>.kml` with features `A1_<polygon>`, `A2_<polygon>`, … (MGRS kept as attributes)
- Red fill at 50% opacity; **XY** labels at cell centres (shrink / hide when zoomed out)

### Linear Search details
- Trails: `path`, `footway`, `track`, `steps`, `bridleway`, `cycleway`
- Waterways: `stream`, `river`, `canal`, `drain`
- Two KMLs under `Flight Plan_<name>/`: `Paths and Trails_<name>.kml` (green), `Waterways and Streams_<name>.kml` (blue)
- Unnamed features: `Trail1_<name>`, `Trail2_<name>`, … and `water1_<name>`, `water2_<name>`, …; **OSM `name` tags are kept when present**

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
1. Download [`Drone_Sweeper_v1.2.2.zip`](https://github.com/hart-gwai/Drone-Sweeper/releases/download/v1.2.2/Drone_Sweeper_v1.2.2.zip)
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
    Area Search_<AOI>.kml
    Paths and Trails_<AOI>.kml
    Waterways and Streams_<AOI>.kml
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
dist/                    # release ZIPs (local; not in git)
```

## Disclaimer
OpenStreetMap coverage varies. Hazard and trail layers are for **operational awareness** only. Confirm wires, terrain, and airspace with local knowledge, visual recon, and authoritative sources before flight.

## License
See repository license (if added). OSM data © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright)).
