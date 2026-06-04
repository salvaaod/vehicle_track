import atexit
import copy
import json
import os
import re
import signal
import threading
import time
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from xml.sax.saxutils import escape

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
    "selected_vehicle": "Default vehicle",
    "vehicles": [
        {
            "name": "Default vehicle",
            "ip": "10.58.1.116",
            "port": 8041
        }
    ],
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
    timestamp_local: str


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.config = self.load_config()
        self.points: List[TrackPoint] = []
        self.last_position: Optional[TrackPoint] = None
        self.last_received_position: Optional[TrackPoint] = None
        self.last_error: Optional[str] = None
        self.running = True
        self.session_filename = self.create_session_filename(self.config.get("selected_vehicle"))
        self.config["kml_filename"] = self.session_filename
        self.save_config()
        self.write_kml()

    @staticmethod
    def local_timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def safe_filename_part(value: Any) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "vehicle")).strip("._-")
        return cleaned or "vehicle"

    def create_session_filename(self, vehicle_name: Optional[str] = None) -> str:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        vehicle_part = self.safe_filename_part(vehicle_name or self.config.get("selected_vehicle"))
        return f"track_{vehicle_part}_{timestamp}.kml"

    @staticmethod
    def vehicle_name_from_entry(vehicle: Dict[str, Any], fallback: str) -> str:
        name = str(vehicle.get("name", "")).strip()
        return name or fallback

    def normalize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        vehicles = []
        for index, vehicle in enumerate(cfg.get("vehicles", [])):
            if not isinstance(vehicle, dict):
                continue

            try:
                port = int(vehicle.get("port", cfg.get("device_port", DEFAULT_CONFIG["device_port"])))
            except (TypeError, ValueError):
                port = int(DEFAULT_CONFIG["device_port"])

            vehicles.append({
                "name": self.vehicle_name_from_entry(vehicle, f"Vehicle {index + 1}"),
                "ip": str(vehicle.get("ip", cfg.get("device_ip", DEFAULT_CONFIG["device_ip"]))).strip(),
                "port": port,
            })

        if not vehicles:
            vehicles.append({
                "name": str(cfg.get("selected_vehicle") or "Default vehicle"),
                "ip": str(cfg.get("device_ip", DEFAULT_CONFIG["device_ip"])).strip(),
                "port": int(cfg.get("device_port", DEFAULT_CONFIG["device_port"])),
            })

        selected = str(cfg.get("selected_vehicle") or vehicles[0]["name"]).strip()
        if selected not in {vehicle["name"] for vehicle in vehicles}:
            selected = vehicles[0]["name"]

        cfg["vehicles"] = vehicles
        cfg["selected_vehicle"] = selected
        self.sync_selected_vehicle_fields(cfg)
        return cfg

    @staticmethod
    def sync_selected_vehicle_fields(cfg: Dict[str, Any]) -> None:
        selected = cfg.get("selected_vehicle")
        for vehicle in cfg.get("vehicles", []):
            if vehicle.get("name") == selected:
                cfg["device_ip"] = vehicle["ip"]
                cfg["device_port"] = vehicle["port"]
                return

    def selected_vehicle(self) -> Dict[str, Any]:
        selected = self.config.get("selected_vehicle")
        for vehicle in self.config.get("vehicles", []):
            if vehicle.get("name") == selected:
                return vehicle
        return self.config["vehicles"][0]

    def initial_config(self) -> Dict[str, Any]:
        return self.normalize_config(copy.deepcopy(DEFAULT_CONFIG))

    @staticmethod
    def write_config_file(config: Dict[str, Any]) -> None:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def load_config(self) -> Dict[str, Any]:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            for key in DEFAULT_CONFIG:
                if key in loaded:
                    cfg[key] = loaded[key]

            if "kml_filename" not in loaded and "kmz_filename" in loaded:
                cfg["kml_filename"] = str(loaded["kmz_filename"]).removesuffix(".kmz") + ".kml"

            return self.normalize_config(cfg)

        cfg = self.initial_config()
        self.write_config_file(cfg)
        return cfg

    def save_config(self) -> None:
        self.write_config_file(self.config)

    def device_url(self) -> str:
        path = str(self.config.get("location_path", "/api/v1/location"))
        if not path.startswith("/"):
            path = "/" + path
        vehicle = self.selected_vehicle()
        return f"http://{vehicle['ip']}:{int(vehicle['port'])}{path}"

    def update_current_vehicle(self, partial: Dict[str, Any]) -> None:
        vehicle = self.selected_vehicle()
        old_name = vehicle["name"]
        new_name = old_name

        if "vehicle_name" in partial:
            proposed_name = str(partial["vehicle_name"]).strip()
            if proposed_name:
                new_name = proposed_name

        if old_name != new_name:
            for other in self.config["vehicles"]:
                if other is not vehicle and other["name"] == new_name:
                    raise ValueError(f"A vehicle named '{new_name}' already exists.")

            vehicle["name"] = new_name
            self.config["selected_vehicle"] = new_name

        if "device_ip" in partial:
            vehicle["ip"] = str(partial["device_ip"]).strip()
        if "device_port" in partial:
            vehicle["port"] = int(partial["device_port"])

        self.sync_selected_vehicle_fields(self.config)

    def update_config(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "location_path": str,
            "update_seconds": float,
            "initial_lat": float,
            "initial_lon": float,
            "request_timeout_seconds": float,
        }

        with self.lock:
            if any(key in partial for key in ("vehicle_name", "device_ip", "device_port")):
                self.update_current_vehicle(partial)

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

    def add_vehicle(self, vehicle: Dict[str, Any]) -> Dict[str, Any]:
        name = str(vehicle.get("name", "")).strip()
        ip = str(vehicle.get("ip", "")).strip()
        if not name:
            raise ValueError("Vehicle name is required.")
        if not ip:
            raise ValueError("Vehicle IP is required.")

        try:
            port = int(vehicle.get("port"))
        except (TypeError, ValueError):
            raise ValueError("Vehicle port is required.") from None

        new_vehicle = {
            "name": name,
            "ip": ip,
            "port": port,
        }

        with self.lock:
            if any(existing["name"] == name for existing in self.config["vehicles"]):
                raise ValueError(f"A vehicle named '{name}' already exists.")

            self.config["vehicles"].append(new_vehicle)
            self.config["selected_vehicle"] = name
            self.sync_selected_vehicle_fields(self.config)
            self.start_new_track_locked()
            self.save_config()

        self.write_kml()
        return self.config.copy()

    def select_vehicle(self, name: str) -> Dict[str, Any]:
        selected_name = str(name).strip()
        if not selected_name:
            raise ValueError("Vehicle name is required.")

        should_restart = False
        with self.lock:
            if not any(vehicle["name"] == selected_name for vehicle in self.config["vehicles"]):
                raise ValueError(f"Unknown vehicle '{selected_name}'.")

            if self.config.get("selected_vehicle") != selected_name:
                self.config["selected_vehicle"] = selected_name
                self.sync_selected_vehicle_fields(self.config)
                self.start_new_track_locked()
                should_restart = True

            self.save_config()

        if should_restart:
            self.write_kml()
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
                timestamp_local=self.local_timestamp()
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
            vehicle_name = self.config.get("selected_vehicle", "Vehicle")

        coordinates = "\n".join(
            f"{p.lon},{p.lat},0" for p in points_copy
        )

        placemarks = "\n".join(
            f"""
            <Placemark>
                <name>{escape(p.timestamp_local)}</name>
                <TimeStamp><when>{escape(p.timestamp_local)}</when></TimeStamp>
                <Point><coordinates>{p.lon},{p.lat},0</coordinates></Point>
            </Placemark>
            """
            for p in points_copy
        )

        kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>{escape(vehicle_name)} Live GPS Track</name>

    <Style id="trackStyle">
        <LineStyle>
            <color>ff0000ff</color>
            <width>4</width>
        </LineStyle>
    </Style>

    <Placemark>
        <name>{escape(vehicle_name)} GPS Track</name>
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
                "selected_vehicle": self.selected_vehicle().copy(),
                "last_position": asdict(self.last_position) if self.last_position else None,
                "last_received_position": asdict(self.last_received_position) if self.last_received_position else None,
                "last_error": self.last_error,
                "points": [asdict(p) for p in self.points[-2000:]],
                "point_count": len(self.points),
                "kml_file": str(DATA_DIR / self.config.get("kml_filename", KML_FILE.name)),
            }

    def start_new_track_locked(self) -> None:
        self.points = []
        self.last_position = None
        self.last_received_position = None
        self.last_error = None
        self.session_filename = self.create_session_filename(self.config.get("selected_vehicle"))
        self.config["kml_filename"] = self.session_filename

    def new_track(self) -> None:
        with self.lock:
            self.start_new_track_locked()
            self.save_config()

        self.write_kml()

    def shutdown(self) -> None:
        with self.lock:
            self.running = False
            self.save_config()


tracker = Tracker()
app = Flask(__name__)
atexit.register(tracker.shutdown)


def handle_shutdown_signal(signum, frame) -> None:
    tracker.shutdown()
    raise SystemExit(0)


signal.signal(signal.SIGINT, handle_shutdown_signal)
signal.signal(signal.SIGTERM, handle_shutdown_signal)


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

        #topbar input, #topbar select {
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

        #last_position_display, #measure_distance_display {
            font-size: 13px;
            font-weight: bold;
            white-space: nowrap;
        }

        #measure_distance_display {
            color: #ffffff;
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

        .measure-active #map {
            cursor: crosshair;
        }

        .measure-marker {
            align-items: center;
            background: #f2994a;
            border: 2px solid #ffffff;
            border-radius: 50%;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.5);
            color: #111111;
            display: flex;
            font-size: 11px;
            font-weight: bold;
            height: 20px;
            justify-content: center;
            width: 20px;
        }
    </style>
</head>

<body>
    <div id="topbar">
        <label>Vehicle:
            <select id="vehicle_select" onchange="vehicleSelected()"></select>
        </label>

        <label>Name:
            <input id="vehicle_name">
        </label>

        <label>Device IP:
            <input id="device_ip">
        </label>

        <label>Port:
            <input id="device_port" type="number">
        </label>
        <button onclick="addVehicle()">Add vehicle</button>

        <label>Update seconds:
            <input id="update_seconds" type="number" min="1" step="1" oninput="updateSecondsChanged()">
        </label>

        <button id="center_button" class="active" onclick="centerOnVehicle()" type="button" aria-pressed="true">Center</button>
        <button id="measure_button" onclick="toggleMeasure()" type="button" aria-pressed="false" title="Click map points to measure distance">Measure</button>
        <button onclick="clearMeasure()" type="button">Clear measure</button>
        <span id="measure_distance_display">Measure: 0 m</span>
        <button onclick="saveConfig()">Save config</button>
        <button onclick="newTrack()">New track</button>
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
        let measureEnabled = false;
        let measurePoints = [];
        let measureLine = null;
        let measureMarkers = [];

        function setStatus(html) {
            document.getElementById("status").innerHTML = html;
        }

        async function loadState() {
            const response = await fetch("/api/state");
            return await response.json();
        }

        function fillConfigInputs(cfg) {
            const vehicleSelect = document.getElementById("vehicle_select");
            vehicleSelect.innerHTML = "";
            for (const vehicle of cfg.vehicles || []) {
                const option = document.createElement("option");
                option.value = vehicle.name;
                option.textContent = vehicle.name;
                option.selected = vehicle.name === cfg.selected_vehicle;
                vehicleSelect.appendChild(option);
            }

            const selectedVehicle = (cfg.vehicles || []).find(vehicle => vehicle.name === cfg.selected_vehicle) || {};
            document.getElementById("vehicle_name").value = selectedVehicle.name || cfg.selected_vehicle || "";
            document.getElementById("device_ip").value = cfg.device_ip;
            document.getElementById("device_port").value = cfg.device_port;
            document.getElementById("update_seconds").value = cfg.update_seconds;
        }

        function formatPosition(value) {
            return Number(value).toFixed(7);
        }

        function formatMeasureDistance(meters) {
            if (meters < 1000) {
                return Math.round(meters) + " m";
            }

            return (meters / 1000).toFixed(meters < 10000 ? 2 : 1) + " km";
        }

        function measureDistanceMeters() {
            let total = 0;
            for (let i = 1; i < measurePoints.length; i += 1) {
                total += measurePoints[i - 1].distanceTo(measurePoints[i]);
            }
            return total;
        }

        function updateMeasureDisplay() {
            const display = document.getElementById("measure_distance_display");
            display.textContent = "Measure: " + formatMeasureDistance(measureDistanceMeters());
        }

        function updateMeasureButton() {
            const button = document.getElementById("measure_button");
            button.classList.toggle("active", measureEnabled);
            button.setAttribute("aria-pressed", measureEnabled ? "true" : "false");
            document.body.classList.toggle("measure-active", measureEnabled);
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
            measureLine = L.polyline([], {
                color: "#f2994a",
                dashArray: "8 6",
                weight: 4
            }).addTo(map);

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

            map.on("click", function (event) {
                if (measureEnabled) {
                    addMeasurePoint(event.latlng);
                }
            });
        }

        function addMeasurePoint(latlng) {
            measurePoints.push(latlng);
            measureLine.setLatLngs(measurePoints);

            const marker = L.marker(latlng, {
                icon: L.divIcon({
                    className: "measure-marker",
                    html: String(measurePoints.length),
                    iconSize: [20, 20],
                    iconAnchor: [10, 10]
                })
            }).addTo(map);
            measureMarkers.push(marker);
            updateMeasureDisplay();
        }

        function toggleMeasure() {
            measureEnabled = !measureEnabled;
            updateMeasureButton();
        }

        function clearMeasure() {
            measurePoints = [];
            if (measureLine) {
                measureLine.setLatLngs([]);
            }

            for (const marker of measureMarkers) {
                marker.remove();
            }
            measureMarkers = [];
            updateMeasureDisplay();
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
                    "<br>Local time: " + state.last_position.timestamp_local
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
            statusHtml += "Vehicle: " + state.config.selected_vehicle + " | ";
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
                vehicle_name: document.getElementById("vehicle_name").value,
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

        async function addVehicle() {
            const response = await fetch("/api/vehicles", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    name: document.getElementById("vehicle_name").value,
                    ip: document.getElementById("device_ip").value,
                    port: Number(document.getElementById("device_port").value)
                })
            });

            if (!response.ok) {
                const error = await response.json();
                setStatus('<span class="bad">ERROR:</span> ' + error.error);
                return;
            }

            fillConfigInputs(await response.json());
            await refresh();
        }

        async function vehicleSelected() {
            const response = await fetch("/api/select-vehicle", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({name: document.getElementById("vehicle_select").value})
            });

            if (!response.ok) {
                const error = await response.json();
                setStatus('<span class="bad">ERROR:</span> ' + error.error);
                return;
            }

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

        async function newTrack() {
            await fetch("/api/new-track", {method: "POST"});
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
    try:
        return jsonify(tracker.update_config(data))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/vehicles", methods=["POST"])
def api_vehicles():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(tracker.add_vehicle(data))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/select-vehicle", methods=["POST"])
def api_select_vehicle():
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(tracker.select_vehicle(data.get("name", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/new-track", methods=["POST"])
def api_new_track():
    tracker.new_track()
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
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    finally:
        tracker.shutdown()


if __name__ == "__main__":
    main()
