# OSM KMZ GPS Tracker

Small Python app that:

- Polls `http://<device_ip>:<device_port>/api/v1/location`
- Shows the position on an OpenStreetMap / Leaflet map
- Updates every configurable number of seconds
- Stores the GPS history as a KMZ file

## Install

From this folder:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bat
python app.py
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
- Zoom
- Auto-follow

You can also edit `config.json` directly.

## KMZ output

The file is saved here while the app is running:

```text
data\track.kmz
```

You can also click **Download KMZ** in the web app.
