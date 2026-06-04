import json
import os
import threading
import time
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests
from flask import Flask, jsonify, request, Response, send_file


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
KML_FILE = DATA_DIR / "track.kml"

DATA_DIR.mkdir(exist_ok=True)


DEFAULT_CONFIG = {
    "device_ip": "10.58.1.116",
    "device_port": 8041,
    "location_path": "/api/v1/location",

    "update_seconds": 5,
    "initial_lat": 40.39169233333334,
    "initial_lon": -3.6787388333333335,

    "app_host": "127.0.0.1",
    "app_port": 5000,
    "request_timeout_seconds": 3,

    "kml_filename": "track.kml"
}


@dataclass
class TrackPoint:
    lat: float
    lon: float
    timestamp_utc: str


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = self.load_config()
        self.points: List[TrackPoint] = []
        self.last_position: Optional[TrackPoint] = None
        self.last_received_position: Optional[TrackPoint] = None
        self.last_error: Optional[str] = None
        self.running = True
        self.session_filename = self.create_session_filename()
        self.config["kml_filename"] = self.session_filename
        self.write_kml()

    @staticmethod
    def create_session_filename() -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"track_{timestamp}.kml"

    def load_config(self) -> Dict[str, Any]:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            for key in DEFAULT_CONFIG:
                if key in loaded:
                    cfg[key] = loaded[key]

            if "kml_filename" not in loaded and "kmz_filename" in loaded:
                cfg["kml_filename"] = str(loaded["kmz_filename"]).removesuffix(".kmz") + ".kml"

            return cfg

        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return DEFAULT_CONFIG.copy()

    def save_config(self) -> None:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def device_url(self) -> str:
        path = str(self.config.get("location_path", "/api/v1/location"))
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{self.config['device_ip']}:{int(self.config['device_port'])}{path}"

    def update_config(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "device_ip": str,
            "device_port": int,
            "location_path": str,
            "update_seconds": float,
            "initial_lat": float,
            "initial_lon": float,
            "request_timeout_seconds": float,
        }

        with self.lock:
            for key, converter in allowed.items():
                if key in partial:
                    value = partial[key]
                    if converter is bool:
                        if isinstance(value, bool):
                            self.config[key] = value
                        elif isinstance(value, str):
                            self.config[key] = value.lower() in ("true", "1", "yes", "on")
                        else:
                            self.config[key] = bool(value)
                    else:
                        self.config[key] = converter(value)

            if self.config["update_seconds"] < 1:
                self.config["update_seconds"] = 1

            self.save_config()
            return self.config.copy()

    def poll_once(self) -> None:
        with self.lock:
            url = self.device_url()
            update_seconds = max(1, float(self.config.get("update_seconds", 5)))
            configured_timeout = float(self.config.get("request_timeout_seconds", 3))
            timeout = max(0.1, min(configured_timeout, update_seconds))

        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            payload = r.json()

            lat = float(payload["lat"])
            lon = float(payload["lon"])

            received_point = TrackPoint(
                lat=lat,
                lon=lon,
                timestamp_utc=datetime.now(timezone.utc).isoformat()
            )

            # Ignore zero/zero for tracking unless this is genuinely desired. It is usually "no GPS fix".
            if lat == 0 and lon == 0:
                with self.lock:
                    self.last_received_position = received_point
                    self.last_error = "No position received from device (lat=0 lon=0)"
                return

            with self.lock:
                self.last_position = received_point
                self.last_received_position = received_point
                self.points.append(received_point)
                self.last_error = None

            self.write_kml()

        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)

    def loop(self) -> None:
        next_poll_at = time.monotonic()
        while self.running:
            now = time.monotonic()
            if now < next_poll_at:
                time.sleep(next_poll_at - now)

            poll_started_at = time.monotonic()
            self.poll_once()

            with self.lock:
                wait_seconds = max(1, float(self.config.get("update_seconds", 5)))
            next_poll_at = poll_started_at + wait_seconds

    def write_kml(self) -> None:
        with self.lock:
            points_copy = list(self.points)
            kml_name = self.config.get("kml_filename", KML_FILE.name)

        coordinates = "\n".join(
            f"{p.lon},{p.lat},0" for p in points_copy
        )

        placemarks = "\n".join(
            f"""
            <Placemark>
                <name>{p.timestamp_utc}</name>
                <TimeStamp><when>{p.timestamp_utc}</when></TimeStamp>
                <Point><coordinates>{p.lon},{p.lat},0</coordinates></Point>
            </Placemark>
            """
            for p in points_copy
        )

        kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>Live GPS Track</name>

    <Style id="trackStyle">
        <LineStyle>
            <color>ff0000ff</color>
            <width>4</width>
        </LineStyle>
    </Style>

    <Placemark>
        <name>GPS Track</name>
        <styleUrl>#trackStyle</styleUrl>
        <LineString>
            <tessellate>1</tessellate>
            <coordinates>
{coordinates}
            </coordinates>
        </LineString>
    </Placemark>

{placemarks}

</Document>
</kml>
"""

        output_file = DATA_DIR / kml_name
        tmp_file = output_file.with_suffix(".tmp")

        with tmp_file.open("w", encoding="utf-8") as f:
            f.write(kml)

        os.replace(tmp_file, output_file)

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "config": self.config.copy(),
                "device_url": self.device_url(),
                "last_position": asdict(self.last_position) if self.last_position else None,
                "last_received_position": asdict(self.last_received_position) if self.last_received_position else None,
                "last_error": self.last_error,
                "points": [asdict(p) for p in self.points[-2000:]],
                "point_count": len(self.points),
                "kml_file": str(DATA_DIR / self.config.get("kml_filename", KML_FILE.name)),
            }

    def clear_track(self) -> None:
        with self.lock:
            self.points = []
            self.last_position = None
            self.last_received_position = None
            self.last_error = None

        output_file = DATA_DIR / self.config.get("kml_filename", KML_FILE.name)
        if output_file.exists():
            output_file.unlink()


tracker = Tracker()
app = Flask(__name__)


HTML_PAGE = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>OSM KML GPS Tracker</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    />

    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f2f2f2;
        }

        #topbar {
            padding: 10px;
            background: #202020;
            color: white;
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }

        #topbar label {
            font-size: 13px;
        }

        #topbar input {
            width: 115px;
            padding: 4px;
        }

        #topbar button, #topbar a {
            padding: 6px 10px;
            border: 0;
            background: #ffffff;
            color: #111;
            text-decoration: none;
            cursor: pointer;
            border-radius: 4px;
        }

        #topbar button.active {
            background: #2f80ed;
            color: #ffffff;
        }

        #last_position_display {
            font-size: 13px;
            font-weight: bold;
            white-space: nowrap;
        }

        #status {
            padding: 7px 10px;
            background: #fff;
            border-bottom: 1px solid #ccc;
            font-size: 14px;
        }

        #map {
            width: 100vw;
            height: calc(100vh - 94px);
        }

        .ok {
            color: #0a7c20;
            font-weight: bold;
        }

        .bad {
            color: #b00020;
            font-weight: bold;
        }

        .leaflet-control-scale {
            margin-right: 16px;
            margin-bottom: 16px;
        }

        .leaflet-control-scale-line {
            background: rgba(255, 255, 255, 0.9);
            border-color: #111;
            border-top: 0;
            color: #111;
            font-size: 12px;
            font-weight: bold;
            padding: 2px 6px 3px;
            text-shadow: none;
        }
    </style>
</head>

<body>
    <div id="topbar">
        <label>Device IP:
            <input id="device_ip">
        </label>

        <label>Port:
            <input id="device_port" type="number">
        </label>

        <label>Update seconds:
            <input id="update_seconds" type="number" min="1" step="1" oninput="updateSecondsChanged()">
        </label>

        <button id="center_button" class="active" onclick="centerOnVehicle()" type="button" aria-pressed="true">Center</button>
        <button onclick="saveConfig()">Save config</button>
        <button onclick="clearTrack()">Clear track</button>
        <a href="/download-kml">Download KML</a>
        <span id="last_position_display" class="bad">No position received</span>
    </div>

    <div id="status">Starting...</div>
    <div id="map"></div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

    <script>
        let map;
        let marker;
        let trackLine;
        let refreshTimer = null;
        let firstLoad = true;
        let centerEnabled = true;
        let suppressMoveDeselect = false;

        function setStatus(html) {
            document.getElementById("status").innerHTML = html;
        }

        async function loadState() {
            const response = await fetch("/api/state");
            return await response.json();
        }

        function fillConfigInputs(cfg) {
            document.getElementById("device_ip").value = cfg.device_ip;
            document.getElementById("device_port").value = cfg.device_port;
            document.getElementById("update_seconds").value = cfg.update_seconds;
        }

        function formatPosition(value) {
            return Number(value).toFixed(7);
        }

        function updateLastPositionDisplay(state) {
            const display = document.getElementById("last_position_display");
            const received = state.last_received_position;

            if (!received || (received.lat === 0 && received.lon === 0)) {
                display.className = "bad";
                display.textContent = "No position received";
                return;
            }

            display.className = "ok";
            display.textContent = "Last position: Lat " + formatPosition(received.lat) +
                ", Lon " + formatPosition(received.lon);
        }

        function createMap(cfg, state) {
            const startLat = state.last_position ? state.last_position.lat : cfg.initial_lat;
            const startLon = state.last_position ? state.last_position.lon : cfg.initial_lon;

            map = L.map("map").setView([startLat, startLon], 16);

            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19,
                attribution: '&copy; OpenStreetMap contributors'
            }).addTo(map);

            marker = L.marker([startLat, startLon]).addTo(map);
            trackLine = L.polyline([], { weight: 4 }).addTo(map);

            L.control.scale({
                position: "bottomright",
                metric: true,
                imperial: false,
                maxWidth: 160
            }).addTo(map);

            map.on("dragstart", function () {
                if (!suppressMoveDeselect) {
                    setCenterEnabled(false);
                }
            });
        }

        function updateMap(state) {
            const cfg = state.config;
            if (firstLoad) {
                fillConfigInputs(cfg);
            }

            if (!map) {
                createMap(cfg, state);
            }

            updateLastPositionDisplay(state);

            const points = state.points.map(p => [p.lat, p.lon]);
            trackLine.setLatLngs(points);

            if (state.last_position) {
                const latlon = [state.last_position.lat, state.last_position.lon];
                marker.setLatLng(latlon);
                marker.bindPopup(
                    "Lat: " + state.last_position.lat.toFixed(7) +
                    "<br>Lon: " + state.last_position.lon.toFixed(7) +
                    "<br>UTC: " + state.last_position.timestamp_utc
                );

                if (centerEnabled || firstLoad) {
                    suppressMoveDeselect = true;
                    map.setView(latlon, map.getZoom());
                    suppressMoveDeselect = false;
                }
            }

            let statusHtml = "";
            if (state.last_error) {
                statusHtml += '<span class="bad">ERROR:</span> ' + state.last_error + " | ";
            } else {
                statusHtml += '<span class="ok">OK</span> | ';
            }

            statusHtml += "Device URL: " + state.device_url + " | ";
            statusHtml += "Track points: " + state.point_count + " | ";
            statusHtml += "KML: " + state.kml_file;

            setStatus(statusHtml);
            firstLoad = false;

            scheduleNext(document.getElementById("update_seconds").value || cfg.update_seconds);
        }

        function scheduleNext(seconds) {
            if (refreshTimer) {
                clearTimeout(refreshTimer);
            }
            refreshTimer = setTimeout(refresh, Math.max(1, Number(seconds)) * 1000);
        }

        async function refresh() {
            try {
                const state = await loadState();
                updateMap(state);
            } catch (err) {
                setStatus('<span class="bad">Frontend error:</span> ' + err);
                scheduleNext(5);
            }
        }

        async function updateSecondsChanged() {
            const updateSeconds = Math.max(1, Number(document.getElementById("update_seconds").value) || 1);
            const response = await fetch("/api/config", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({update_seconds: updateSeconds})
            });
            const cfg = await response.json();
            document.getElementById("update_seconds").value = cfg.update_seconds;
            scheduleNext(cfg.update_seconds);
        }

        async function saveConfig() {
            const body = {
                device_ip: document.getElementById("device_ip").value,
                device_port: Number(document.getElementById("device_port").value),
                update_seconds: Number(document.getElementById("update_seconds").value)
            };

            const response = await fetch("/api/config", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(body)
            });

            fillConfigInputs(await response.json());
            await refresh();
        }

        function setCenterEnabled(enabled) {
            centerEnabled = enabled;
            const button = document.getElementById("center_button");
            button.classList.toggle("active", centerEnabled);
            button.setAttribute("aria-pressed", centerEnabled ? "true" : "false");
        }

        function centerOnVehicle() {
            setCenterEnabled(true);
            if (marker && map) {
                suppressMoveDeselect = true;
                map.setView(marker.getLatLng(), map.getZoom());
                suppressMoveDeselect = false;
            }
        }

        async function clearTrack() {
            await fetch("/api/clear-track", {method: "POST"});
            await refresh();
        }

        refresh();
    </script>
</body>
</html>
"""


@app.route("/")
def index() -> Response:
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/state")
def api_state():
    return jsonify(tracker.state())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(tracker.state()["config"])

    data = request.get_json(force=True, silent=True) or {}
    return jsonify(tracker.update_config(data))


@app.route("/api/clear-track", methods=["POST"])
def api_clear_track():
    tracker.clear_track()
    return jsonify({"ok": True})


@app.route("/download-kml")
def download_kml():
    path = DATA_DIR / tracker.config.get("kml_filename", KML_FILE.name)
    if not path.exists():
        tracker.write_kml()

    if not path.exists():
        return Response("No KML track has been created yet.", status=404)

    return send_file(path, as_attachment=True, download_name=path.name)


def main() -> None:
    worker = threading.Thread(target=tracker.loop, daemon=True)
    worker.start()

    host = tracker.config.get("app_host", "127.0.0.1")
    port = int(tracker.config.get("app_port", 5000))

    print()
    print("OSM KML GPS Tracker")
    print("-------------------")
    print(f"Open this URL in your browser: http://{host}:{port}")
    print(f"Polling device: {tracker.device_url()}")
    app_url = f"http://{host}:{port}"
    print(f"KML file: {DATA_DIR / tracker.config.get('kml_filename', KML_FILE.name)}")
    print()

    threading.Timer(1.0, lambda: webbrowser.open(app_url)).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
