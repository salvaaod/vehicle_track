# OSM KML GPS Tracker

Small Python app that:

- Polls `http://<device_ip>:<device_port>/api/v1/location`
- Shows the position on an OpenStreetMap / Leaflet map
- Updates every configurable number of seconds
- Stores each run's GPS history as a timestamped KML file
- Saves multiple named vehicles, each with its own IP and port
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

- Vehicle selector
- Vehicle name
- Device IP
- Device port
- Update seconds

Use **Add vehicle** to save a new named vehicle. Use the **Vehicle** selector to switch between saved vehicles. Selecting a different vehicle immediately starts a new timestamped KML file for that vehicle.

The **Update seconds** value is saved as soon as the number is changed; you do not need to press **Save config** for that field.

Configuration is saved when values change and again when the program closes. If `config.json` does not exist, the app creates it from the initial defaults embedded in `vehicle_track.py`.

The **Center** button starts active. While active, the map keeps the current browser zoom level and centers on the vehicle when positions arrive. Moving the map automatically deselects **Center**, and incoming positions will no longer recenter the map until **Center** is pressed again.

You can also edit the generated `config.json` directly. The file is intentionally ignored by Git so each machine can keep its own local settings.

## KML output

Each time the program starts, it creates a new timestamped file in `data`, for example:

```text
data\track_Default_vehicle_20260604_153000_123456.kml
```

File names use the computer's local time. Position timestamps inside the KML file and in the browser popup also use the computer's local time.

You can also click **Download KML** in the web app. Click **New track** to close the current KML file and start writing a new timestamped one for the selected vehicle.
