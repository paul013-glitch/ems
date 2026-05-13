#!/usr/bin/env python3
"""Local polling loop and dashboard API for the EMS devices.

The loop reads devices on the local Modbus network, writes the latest state to
telemetry_state.json, and serves a tiny HTTP API for live-dashboard.html.

Open http://127.0.0.1:8080 after starting this script.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from read_clou_battery import FUNCTION_READ_HOLDING_REGISTERS, read_battery, status_label as battery_status_label
from read_growatt_max import read_inverter, status_label as inverter_status_label
from read_sdm630_kw import read_total_system_power_watts


WORKSPACE = Path(__file__).resolve().parent
STATE_FILE = WORKSPACE / "telemetry_state.json"
DASHBOARD_FILE = WORKSPACE / "live-dashboard.html"

DEFAULT_SHARED_HOST = "10.201.150.254"
DEFAULT_BATTERY_HOSTS = "10.201.150.180,10.201.150.181,10.201.150.182,10.201.150.183"
DEFAULT_PORT = 502
DEFAULT_METER_UNIT_ID = 2
DEFAULT_BATTERY_UNIT_ID = 1
DEFAULT_INVERTER_UNIT_IDS = "1,3,4,5,6,7"
ONLINE_WINDOW_SECONDS = 60

STATE_LOCK = threading.Lock()
STATE = {
    "paused": False,
    "last_poll": None,
    "poll_interval_seconds": 10,
    "online_window_seconds": ONLINE_WINDOW_SECONDS,
    "devices": {},
    "ems_control": {
        "solar_setpoint_percent": 100,
        "battery_setpoint_percent": 0,
        "last_action": "Waiting for live values",
        "rules": [],
    },
}
LAST_CLOUD_UPLOAD = 0.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_unit_ids(unit_ids: str) -> list[int]:
    return [int(value.strip()) for value in unit_ids.split(",") if value.strip()]


def parse_hosts(hosts: str) -> list[str]:
    return [value.strip() for value in hosts.split(",") if value.strip()]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def device_is_online(device: dict, now: datetime) -> bool:
    last_success = parse_iso(device.get("last_success"))
    if not last_success:
        return False
    return (now - last_success).total_seconds() <= ONLINE_WINDOW_SECONDS


def snapshot_state() -> dict:
    now = utc_now()
    with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE))

    for device in snapshot["devices"].values():
        device["online"] = device_is_online(device, now)
    update_calculated_totals(snapshot, now)
    return snapshot


def update_calculated_totals(state: dict, now: datetime) -> None:
    totals = state.setdefault("totals", {})
    devices = state.get("devices", {})
    grid_kw = devices.get("grid_meter", {}).get("values", {}).get("current_kw")

    inverters = [device for device in devices.values() if device.get("type") == "growatt_max"]
    online_inverters = [device for device in inverters if device_is_online(device, now)]
    solar_kw = sum(
        device.get("values", {}).get("production_kw", 0)
        for device in online_inverters
        if isinstance(device.get("values", {}).get("production_kw"), (int, float))
    )

    batteries = [device for device in devices.values() if device.get("type") == "clou_battery"]
    online_batteries = [device for device in batteries if device_is_online(device, now)]
    battery_kw = sum(
        device.get("values", {}).get("power_kw", 0)
        for device in online_batteries
        if isinstance(device.get("values", {}).get("power_kw"), (int, float))
    )
    battery_soc_values = [
        device.get("values", {}).get("soc_percent")
        for device in online_batteries
        if isinstance(device.get("values", {}).get("soc_percent"), (int, float))
    ]

    totals["inverters_online"] = len(online_inverters)
    totals["inverters_count"] = len(inverters) or totals.get("inverters_count", 0)
    totals["solar_production_kw"] = round(solar_kw, 3)
    totals["batteries_online"] = len(online_batteries)
    totals["batteries_count"] = len(batteries) or totals.get("batteries_count", 0)
    totals["battery_power_kw"] = round(battery_kw, 1)
    totals["battery_average_soc_percent"] = (
        round(sum(battery_soc_values) / len(battery_soc_values), 1) if battery_soc_values else None
    )

    if all(isinstance(value, (int, float)) for value in (grid_kw, solar_kw, battery_kw)):
        totals["calculated_load_kw"] = round(solar_kw + grid_kw + battery_kw, 3)
    else:
        totals["calculated_load_kw"] = None


def clamp(value: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))


def apply_shadow_ems_rules(args: argparse.Namespace) -> None:
    state = snapshot_state()
    totals = state.get("totals", {})
    devices = state.get("devices", {})
    grid_kw = devices.get("grid_meter", {}).get("values", {}).get("current_kw")
    battery_soc = totals.get("battery_average_soc_percent")

    with STATE_LOCK:
        control = STATE.setdefault("ems_control", {})
        solar_setpoint = int(control.get("solar_setpoint_percent", 100))
        battery_setpoint = int(control.get("battery_setpoint_percent", 0))

    if not isinstance(grid_kw, (int, float)) or not isinstance(battery_soc, (int, float)):
        with STATE_LOCK:
            control = STATE.setdefault("ems_control", {})
            control["last_action"] = "Waiting for meter and battery SOC"
            control["rules"] = []
        print("shadow_ems: waiting for meter and battery SOC")
        return

    battery_empty = battery_soc <= args.battery_empty_threshold
    battery_full = battery_soc >= args.battery_full_threshold
    actions = []

    if grid_kw > 2 and solar_setpoint < 100:
        solar_setpoint += 1
        actions.append("meter import >2kW, solar +1%")

    if grid_kw > 10 and not battery_empty and solar_setpoint >= 100:
        battery_setpoint += 1
        actions.append("meter import >10kW, battery discharge +1%")

    if battery_empty:
        if battery_setpoint != 0:
            actions.append("battery below threshold, battery setpoint 0%")
        battery_setpoint = 0

    if grid_kw < -2 and not battery_full:
        battery_setpoint -= 1
        actions.append("meter export >2kW, battery charge -1%")

    if grid_kw < -2 and battery_full:
        solar_setpoint -= 1
        actions.append("meter export >2kW and battery full, solar -1%")

    if battery_setpoint <= -100 and grid_kw < -10:
        solar_setpoint -= 1
        actions.append("battery charge setpoint -100% and export >10kW, solar -1%")

    solar_setpoint = clamp(solar_setpoint, 0, 100)
    battery_setpoint = clamp(battery_setpoint, -100, 100)
    last_action = "; ".join(actions) if actions else "Hold setpoints"

    with STATE_LOCK:
        control = STATE.setdefault("ems_control", {})
        control["solar_setpoint_percent"] = solar_setpoint
        control["battery_setpoint_percent"] = battery_setpoint
        control["battery_empty_threshold_percent"] = args.battery_empty_threshold
        control["battery_full_threshold_percent"] = args.battery_full_threshold
        control["last_action"] = last_action
        control["rules"] = actions

    print(
        "shadow_ems: "
        f"solar={solar_setpoint}%, battery={battery_setpoint}%, "
        f"grid={grid_kw:.3f}kW, battery_soc={battery_soc:.1f}%, action={last_action}"
    )


def save_state() -> None:
    STATE_FILE.write_text(json.dumps(snapshot_state(), indent=2), encoding="utf-8")


def upload_to_cloud(args: argparse.Namespace) -> None:
    global LAST_CLOUD_UPLOAD

    if not args.cloud_url:
        return

    now = time.monotonic()
    if now - LAST_CLOUD_UPLOAD < args.cloud_upload_interval:
        return

    payload = json.dumps(snapshot_state()).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "hmg-local-ems/0.1",
    }
    if args.cloud_token:
        headers["Authorization"] = f"Bearer {args.cloud_token}"

    request = urllib.request.Request(args.cloud_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.cloud_timeout) as response:
            response.read()
        LAST_CLOUD_UPLOAD = now
        print(f"cloud_upload: ok -> {args.cloud_url}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"cloud_upload: failed ({exc})")


def update_device(device_id: str, device_type: str, values: dict | None = None, error: str | None = None) -> None:
    now = iso_now()
    with STATE_LOCK:
        previous = STATE["devices"].get(device_id, {})
        STATE["devices"][device_id] = {
            "id": device_id,
            "type": device_type,
            "last_attempt": now,
            "last_success": now if error is None else previous.get("last_success"),
            "online": error is None,
            "values": values if values is not None else previous.get("values", {}),
            "error": error,
        }


def read_meter(args: argparse.Namespace) -> None:
    try:
        watts = read_total_system_power_watts(
            host=args.host,
            port=args.port,
            unit_id=args.meter_unit_id,
            timeout=args.timeout,
            reverse_register_order=args.reverse_meter_register_order,
        )
    except Exception as exc:
        update_device("grid_meter", "sdm630", error=str(exc))
        print(f"grid_meter: offline ({exc})")
        return

    values = {"current_kw": round(watts / 1000, 3)}
    update_device("grid_meter", "sdm630", values=values)
    print(f"grid_meter: online, current_kw={values['current_kw']:.3f}")


def read_inverters(args: argparse.Namespace, unit_ids: list[int]) -> None:
    total_kw = 0.0
    online_count = 0

    def read_one(unit_id: int) -> tuple[int, int | None, float | None, str | None]:
        try:
            status, production_kw = read_inverter(
                host=args.host,
                port=args.port,
                unit_id=unit_id,
                timeout=args.timeout,
            )
            return unit_id, status, production_kw, None
        except Exception as exc:
            return unit_id, None, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(len(unit_ids), args.max_workers)) as executor:
        futures = [executor.submit(read_one, unit_id) for unit_id in unit_ids]
        results = [future.result() for future in as_completed(futures)]

    for unit_id, status, production_kw, error in sorted(results, key=lambda result: result[0]):
        device_id = f"inverter_{unit_id}"
        if error is not None or status is None or production_kw is None:
            update_device(device_id, "growatt_max", error=error or "unknown read error")
            print(f"{device_id}: offline ({error})")
            continue

        values = {
            "unit_id": unit_id,
            "status": inverter_status_label(status),
            "status_code": status,
            "production_kw": round(production_kw, 3),
        }
        update_device(device_id, "growatt_max", values=values)
        online_count += 1
        total_kw += production_kw
        print(f"{device_id}: online, status={values['status']}, production_kw={production_kw:.3f}")

    with STATE_LOCK:
        totals = STATE.setdefault("totals", {})
        totals["inverters_count"] = len(unit_ids)
        totals["last_poll_inverters_online"] = online_count
        totals["last_poll_solar_production_kw"] = round(total_kw, 3)


def read_battery_devices(args: argparse.Namespace, battery_hosts: list[str]) -> None:
    if args.disable_battery:
        return

    online_count = 0
    total_power_kw = 0.0
    soc_values = []

    def read_one(host: str) -> tuple[str, str | None, float | None, float | None, int | None, str | None]:
        try:
            source, soc_percent, power_kw, status = read_battery(
                host=host,
                port=args.battery_port,
                unit_id=args.battery_unit_id,
                timeout=args.timeout,
                function_code=FUNCTION_READ_HOLDING_REGISTERS,
                source=args.battery_source,
            )
            return host, source, soc_percent, power_kw, status, None
        except Exception as exc:
            return host, None, None, None, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(len(battery_hosts), args.max_workers)) as executor:
        futures = [executor.submit(read_one, host) for host in battery_hosts]
        results = [future.result() for future in as_completed(futures)]

    for host, source, soc_percent, power_kw, status, error in sorted(results, key=lambda result: result[0]):
        device_id = f"battery_{host.split('.')[-1]}"
        if error is not None or source is None or soc_percent is None or power_kw is None or status is None:
            update_device(device_id, "clou_battery", error=error or "unknown read error")
            print(f"{device_id}: offline ({error})")
            continue

        values = {
            "host": host,
            "unit_id": args.battery_unit_id,
            "source": source,
            "soc_percent": round(soc_percent, 1),
            "power_kw": round(power_kw, 1),
            "status": battery_status_label(status),
            "status_code": status,
        }
        update_device(device_id, "clou_battery", values=values)
        online_count += 1
        total_power_kw += power_kw
        soc_values.append(soc_percent)
        print(
            f"{device_id}: online, host={host}, "
            f"source={source}, soc={soc_percent:.1f}, power_kw={power_kw:.1f}, status={values['status']}"
        )

    average_soc = sum(soc_values) / len(soc_values) if soc_values else None
    with STATE_LOCK:
        totals = STATE.setdefault("totals", {})
        totals["batteries_count"] = len(battery_hosts)
        totals["last_poll_batteries_online"] = online_count
        totals["last_poll_battery_power_kw"] = round(total_power_kw, 1)
        totals["last_poll_battery_average_soc_percent"] = round(average_soc, 1) if average_soc is not None else None


def poll_once(args: argparse.Namespace, inverter_unit_ids: list[int], battery_hosts: list[str]) -> None:
    with STATE_LOCK:
        STATE["last_poll"] = iso_now()
        STATE["poll_interval_seconds"] = args.interval
        STATE["online_window_seconds"] = ONLINE_WINDOW_SECONDS

    print()
    print(f"--- {datetime.now().isoformat(timespec='seconds')} ---")
    read_meter(args)
    read_inverters(args, inverter_unit_ids)
    read_battery_devices(args, battery_hosts)
    apply_shadow_ems_rules(args)
    save_state()
    upload_to_cloud(args)


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/live-dashboard.html"):
            body = DASHBOARD_FILE.read_bytes()
            self.send_bytes(200, body, "text/html; charset=utf-8")
            return

        if path in ("/api/state", "/telemetry_state.json"):
            body = json.dumps(snapshot_state(), indent=2).encode("utf-8")
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return

        self.send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/control":
            self.send_bytes(404, b"not found", "text/plain; charset=utf-8")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_bytes(400, b"invalid json", "text/plain; charset=utf-8")
            return

        with STATE_LOCK:
            if "paused" in payload:
                STATE["paused"] = bool(payload["paused"])

        save_state()
        response = json.dumps(snapshot_state(), indent=2).encode("utf-8")
        self.send_bytes(200, response, "application/json; charset=utf-8")


def start_dashboard_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll EMS devices and serve a local dashboard API.")
    parser.add_argument("--host", default=DEFAULT_SHARED_HOST, help="Shared Modbus TCP host/IP for meter/inverters")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Shared Modbus TCP port")
    parser.add_argument("--meter-unit-id", default=DEFAULT_METER_UNIT_ID, type=int, help="SDM630 unit id")
    parser.add_argument(
        "--inverter-unit-ids",
        default=DEFAULT_INVERTER_UNIT_IDS,
        help="Comma-separated Growatt inverter unit ids",
    )
    parser.add_argument(
        "--battery-hosts",
        default=DEFAULT_BATTERY_HOSTS,
        help="Comma-separated CLOU battery Modbus TCP hosts/IPs",
    )
    parser.add_argument("--battery-port", default=DEFAULT_PORT, type=int, help="CLOU battery Modbus TCP port")
    parser.add_argument("--battery-unit-id", default=DEFAULT_BATTERY_UNIT_ID, type=int, help="CLOU battery unit id")
    parser.add_argument(
        "--battery-source",
        choices=("auto", "cabinet", "string"),
        default="auto",
        help="CLOU battery register group",
    )
    parser.add_argument("--disable-battery", action="store_true", help="Skip battery reads")
    parser.add_argument("--interval", default=10.0, type=float, help="Polling interval in seconds")
    parser.add_argument("--timeout", default=3.0, type=float, help="Per-read socket timeout in seconds")
    parser.add_argument("--max-workers", default=12, type=int, help="Maximum parallel device reads per group")
    parser.add_argument(
        "--battery-empty-threshold",
        default=10.0,
        type=float,
        help="Average SOC percent at or below which the shadow EMS considers the battery empty",
    )
    parser.add_argument(
        "--battery-full-threshold",
        default=95.0,
        type=float,
        help="Average SOC percent at or above which the shadow EMS considers the battery full",
    )
    parser.add_argument("--dashboard-port", default=8080, type=int, help="Local dashboard HTTP port")
    parser.add_argument("--no-dashboard", action="store_true", help="Only run the polling loop")
    parser.add_argument(
        "--cloud-url",
        default="",
        help="Optional Netlify telemetry endpoint, for example https://example.netlify.app/api/telemetry",
    )
    parser.add_argument("--cloud-token", default="", help="Optional bearer token matching TELEMETRY_TOKEN on Netlify")
    parser.add_argument("--cloud-upload-interval", default=10.0, type=float, help="Cloud upload interval in seconds")
    parser.add_argument("--cloud-timeout", default=10.0, type=float, help="Cloud upload timeout in seconds")
    parser.add_argument(
        "--reverse-meter-register-order",
        action="store_true",
        help="Use if the SDM630 is configured for reversed float register order",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inverter_unit_ids = parse_unit_ids(args.inverter_unit_ids)
    battery_hosts = parse_hosts(args.battery_hosts)

    if not args.no_dashboard:
        start_dashboard_server(args.dashboard_port)
        print(f"Dashboard: http://127.0.0.1:{args.dashboard_port}")

    print("Starting polling loop. Press Ctrl+C to stop.")
    print(f"Polling every {args.interval:g}s: meter unit {args.meter_unit_id}, inverters {inverter_unit_ids}")
    if not args.disable_battery:
        print(f"Battery hosts: {battery_hosts}")

    try:
        while True:
            cycle_started = time.monotonic()
            with STATE_LOCK:
                paused = STATE["paused"]

            if paused:
                save_state()
                print("paused")
            else:
                poll_once(args, inverter_unit_ids, battery_hosts)

            elapsed = time.monotonic() - cycle_started
            time.sleep(max(0, args.interval - elapsed))
    except KeyboardInterrupt:
        print()
        print("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
