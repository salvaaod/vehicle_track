# OSM KML GPS Tracker

Small Python app that:

- Polls `http://<device_ip>:<device_port>/api/v1/location`
- Shows the position on an OpenStreetMap / Leaflet map
- Updates every configurable number of seconds
- Stores the GPS history as a KML file

## Install

From this folder:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bat
python vehicle_track.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Configure

You can configure from the web page:

- Device IP
- Device port
- Update seconds

The **Center** button keeps the current browser zoom level and centers the map on the vehicle. Moving or zooming the map automatically deselects **Center**, and incoming positions will no longer recenter the map until **Center** is pressed again.

You can also edit `config.json` directly.

## KML output

The file is saved here while the app is running:

```text
data\track.kml
```

You can also click **Download KML** in the web app.
