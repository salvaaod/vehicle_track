# OSM KMZ GPS Tracker

Small Python app that:

- Polls `http://<device_ip>:<device_port>/api/v1/location`
- Shows the position on an OpenStreetMap / Leaflet map
- Updates every configurable number of seconds
- Stores each run's GPS history as a timestamped KMZ file
- Opens the web page automatically when the program starts

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

The app automatically opens the browser. You can also open it manually:

```text
http://127.0.0.1:5000
```

## Configure

You can configure from the web page:

- Device IP
- Device port
- Update seconds

The **Update seconds** value is saved as soon as the number is changed; you do not need to press **Save config** for that field.

The **Center** button starts active. While active, the map keeps the current browser zoom level and centers on the vehicle when positions arrive. Moving the map automatically deselects **Center**, and incoming positions will no longer recenter the map until **Center** is pressed again.

You can also edit `config.json` directly.

## KMZ output

Each time the program starts, it creates a new timestamped file in `data`, for example:

```text
data\track_20260604_153000.kmz
```

You can also click **Download KMZ** in the web app.
