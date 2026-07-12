import os, time, json, sqlite3, threading, asyncio
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

# --- Config ---
DB_PATH = os.path.expanduser("~/keezer/keezer.db")
CONFIG_PATH = os.path.expanduser("~/keezer/config.json")
SIMULATE = not os.path.exists("/sys/bus/w1/devices")
READ_INTERVAL = 30
COMPRESSOR_MIN_OFF = 180
COMPRESSOR_MIN_ON = 120

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}

def save_config(data):
    cfg = load_config()
    cfg.update(data)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

app = Flask(__name__)
cfg = load_config()

# --- State ---
state = {
    "target_f": cfg.get("target_f", 36.0),
    "upper_f": cfg.get("upper_f", 38.0),
    "lower_f": cfg.get("lower_f", 34.0),
    "current_f": None,
    "room_f": None,
    "compressor_on": False,
    "last_compressor_off": 0,
    "last_compressor_on": 0,
    "mode": cfg.get("mode", "cool"),
    "comp_state": "idle",
    "watts": 0,
    "kwh_today": 0,
    "avg_kwh": 0,
    "kasa_ip": cfg.get("kasa_ip", ""),
}

# --- Kasa Smart Plug ---
def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def _kasa_client():
    ip = state["kasa_ip"]
    user = os.environ.get("KASA_USER", "")
    pw = os.environ.get("KASA_PASS", "")
    if not (ip and user and pw):
        return None
    from klap import KlapClient
    return KlapClient(ip, user, pw)

def check_plug_reachable(ip=None, timeout=3):
    """Check if the Kasa plug is reachable via TCP port 80."""
    import socket
    ip = ip or state["kasa_ip"]
    if not ip:
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, 80))
        sock.close()
        return result == 0
    except Exception:
        return False

def check_host_reachable(ip, timeout=2):
    """Check if a host is reachable via ping."""
    import subprocess
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout * 1000)), ip],
            capture_output=True, timeout=timeout + 1
        )
        return result.returncode == 0
    except Exception:
        return False

def get_pi_ip():
    """Get the Pi's own IP address."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"

def kasa_auto_discover():
    """Auto-discover Kasa plug if configured IP is unreachable.
    Returns the new IP if found, None otherwise."""
    import socket, os as _os
    print("[kasa] Configured IP unreachable, running auto-discovery...")
    # Scan local subnet for KLAP-responding devices
    pi_ip = get_pi_ip()
    subnet = ".".join(pi_ip.split(".")[:3])
    found_ip = None
    for i in range(2, 255):
        ip = f"{subnet}.{i}"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.3)
            if sock.connect_ex((ip, 80)) == 0:
                sock.close()
                # Try KLAP handshake
                import urllib.request
                data = _os.urandom(16)
                req = urllib.request.Request(
                    f"http://{ip}:80/app/handshake1", data=data, method="POST"
                )
                req.add_header("Content-Type", "application/octet-stream")
                try:
                    r = urllib.request.urlopen(req, timeout=2)
                    if r.status == 200 and len(r.read()) == 48:
                        found_ip = ip
                        break
                except Exception:
                    pass
            else:
                sock.close()
        except Exception:
            pass
    if found_ip:
        print(f"[kasa] Auto-discovered plug at {found_ip}")
        state["kasa_ip"] = found_ip
        save_config({"kasa_ip": found_ip})
        return found_ip
    print("[kasa] Auto-discovery failed - no plug found")
    return None

# Track plug reachability for UI
state["plug_reachable"] = False
state["pi_ip"] = ""

def kasa_control(on):
    client = _kasa_client()
    if not client:
        return
    try:
        _run_async(client.turn_on() if on else client.turn_off())
        state["plug_reachable"] = True
    except Exception:
        state["plug_reachable"] = False
        # Try auto-discovery
        new_ip = kasa_auto_discover()
        if new_ip:
            client = _kasa_client()
            if client:
                _run_async(client.turn_on() if on else client.turn_off())
                state["plug_reachable"] = True
        else:
            raise

def kasa_read_energy():
    client = _kasa_client()
    if not client:
        return 0, 0, 0
    emeter = _run_async(client.get_emeter())
    state["plug_reachable"] = True
    watts = round(emeter.get("power_mw", emeter.get("power", 0) * 1000) / 1000, 1)
    kwh = 0
    avg_kwh = 0
    try:
        now = datetime.now()
        daily = _run_async(client.request({"emeter": {"get_daystat": {"month": now.month, "year": now.year}}}))
        day_list = daily["emeter"]["get_daystat"]["day_list"]
        today = next((d for d in day_list if d["day"] == now.day), None)
        kwh = round(today.get("energy_wh", today.get("energy", 0) * 1000) / 1000, 3) if today else 0
        # Rolling 30 days: this month's past days + last month's days
        past_days = [d for d in day_list if d["day"] < now.day]
        days_needed = 30 - len(past_days)
        if days_needed > 0:
            prev_month = now.month - 1 or 12
            prev_year = now.year if now.month > 1 else now.year - 1
            try:
                prev = _run_async(client.request({"emeter": {"get_daystat": {"month": prev_month, "year": prev_year}}}))
                prev_list = prev["emeter"]["get_daystat"]["day_list"]
                past_days = prev_list[-days_needed:] + past_days
            except Exception:
                pass
        if past_days:
            total = sum(d.get("energy_wh", d.get("energy", 0) * 1000) / 1000 for d in past_days)
            avg_kwh = round(total / len(past_days), 3)
    except Exception:
        pass
    return watts, kwh, avg_kwh

def kasa_discover():
    from kasa import Discover
    async def _do():
        devices = await Discover.discover(timeout=10)
        return {addr: dev.model for addr, dev in devices.items()}
    return _run_async(_do())

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        ts TEXT PRIMARY KEY, temp_f REAL, room_f REAL,
        compressor INTEGER, state TEXT, watts REAL)""")
    conn.commit()
    conn.close()

def log_reading(temp_f, room_f, compressor_on, comp_state, watts):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?)",
                 (datetime.now().isoformat(), temp_f, room_f,
                  int(compressor_on), comp_state, watts))
    conn.execute("DELETE FROM readings WHERE ts < ?",
                 ((datetime.now() - timedelta(hours=72)).isoformat(),))
    conn.commit()
    conn.close()

def get_readings(hours=72):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, temp_f, room_f, compressor, state, watts FROM readings WHERE ts > ? ORDER BY ts",
        ((datetime.now() - timedelta(hours=hours)).isoformat(),)
    ).fetchall()
    conn.close()
    return rows

# --- Temp Probes ---
def _read_probe(device_id):
    try:
        with open(f"/sys/bus/w1/devices/{device_id}/w1_slave") as f:
            lines = f.readlines()
        if "YES" in lines[0]:
            temp_c = int(lines[1].split("t=")[1]) / 1000.0
            return round(temp_c * 9 / 5 + 32, 1)
    except Exception:
        pass
    return None

def read_temps():
    if SIMULATE:
        import random
        cur = state.get("current_f") or state["target_f"]
        if state["compressor_on"]:
            cur -= random.uniform(0.1, 0.5)
        else:
            cur += random.uniform(0.05, 0.3)
        cur += random.uniform(-0.15, 0.15)
        room = round(cur + random.uniform(15, 25), 1)
        return round(cur, 1), room
    probes = sorted(d for d in os.listdir("/sys/bus/w1/devices") if d.startswith("28-"))
    beer_t = _read_probe(probes[0]) if len(probes) > 0 else None
    room_t = _read_probe(probes[1]) if len(probes) > 1 else None
    return beer_t, room_t

# --- Relay Control ---
def set_compressor(on):
    if state["kasa_ip"]:
        try:
            kasa_control(on)
        except Exception as e:
            print(f"[kasa] ERROR controlling plug: {e}")
            state["plug_reachable"] = False
            # Don't update compressor state if we couldn't reach the plug
            return
    elif not SIMULATE:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(17, GPIO.OUT)
        GPIO.output(17, GPIO.HIGH if on else GPIO.LOW)
    state["compressor_on"] = on
    if on:
        state["last_compressor_on"] = time.time()
    else:
        state["last_compressor_off"] = time.time()

# --- Control Loop ---
def control_loop():
    init_db()
    state["pi_ip"] = get_pi_ip()
    check_count = 0
    while True:
        beer_t, room_t = read_temps()
        watts = 0

        # Periodically check plug reachability (every 5 cycles = ~2.5 min)
        check_count += 1
        if check_count % 5 == 0 and state["kasa_ip"]:
            state["plug_reachable"] = check_plug_reachable()
            if not state["plug_reachable"]:
                kasa_auto_discover()

        if beer_t is not None:
            state["current_f"] = beer_t
            state["room_f"] = room_t

            # Read energy
            if state["kasa_ip"]:
                try:
                    watts, kwh, avg_kwh = kasa_read_energy()
                    state["watts"] = watts
                    state["kwh_today"] = kwh
                    state["avg_kwh"] = avg_kwh
                except Exception:
                    pass

            if state["mode"] == "off":
                if state["compressor_on"]:
                    set_compressor(False)
                state["comp_state"] = "idle"
            else:
                target = state["target_f"]
                upper = state["upper_f"]
                lower = state["lower_f"]
                off_elapsed = time.time() - state["last_compressor_off"]
                on_elapsed = time.time() - state["last_compressor_on"]

                if state["compressor_on"]:
                    if beer_t < lower:
                        if on_elapsed >= COMPRESSOR_MIN_ON:
                            set_compressor(False)
                            state["comp_state"] = "idle"
                        else:
                            state["comp_state"] = "min_cool"
                    else:
                        state["comp_state"] = "cooling"
                else:
                    if beer_t > upper:
                        if off_elapsed >= COMPRESSOR_MIN_OFF:
                            set_compressor(True)
                            state["comp_state"] = "cooling"
                        else:
                            state["comp_state"] = "wait_cool"
                    else:
                        state["comp_state"] = "idle"

            log_reading(beer_t, room_t, state["compressor_on"],
                        state["comp_state"], watts)
        time.sleep(READ_INTERVAL)

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    # Compute averages from DB
    avg_temp = None
    avg_room = None
    avg_run_min = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT AVG(temp_f), AVG(room_f) FROM readings WHERE ts > ?",
            ((datetime.now() - timedelta(days=30)).isoformat(),)
        ).fetchone()
        if row:
            avg_temp = round(row[0], 1) if row[0] else None
            avg_room = round(row[1], 1) if row[1] else None
        # Average compressor run time
        runs = conn.execute(
            "SELECT ts, compressor FROM readings WHERE ts > ? ORDER BY ts",
            ((datetime.now() - timedelta(hours=72)).isoformat(),)
        ).fetchall()
        conn.close()
        run_durations = []
        run_start = None
        for ts, comp in runs:
            if comp and run_start is None:
                run_start = ts
            elif not comp and run_start is not None:
                start_dt = datetime.fromisoformat(run_start)
                end_dt = datetime.fromisoformat(ts)
                run_durations.append((end_dt - start_dt).total_seconds() / 60)
                run_start = None
        if run_durations:
            avg_run_min = round(sum(run_durations) / len(run_durations), 1)
    except Exception:
        pass

    return jsonify({
        "current_f": state["current_f"],
        "room_f": state["room_f"],
        "target_f": state["target_f"],
        "upper_f": state["upper_f"],
        "lower_f": state["lower_f"],
        "compressor_on": state["compressor_on"],
        "mode": state["mode"],
        "comp_state": state["comp_state"],
        "watts": state["watts"],
        "kwh_today": state["kwh_today"],
        "avg_kwh": state["avg_kwh"],
        "avg_temp": avg_temp,
        "avg_room": avg_room,
        "avg_run_min": avg_run_min,
        "kasa_ip": state["kasa_ip"],
        "pi_ip": state.get("pi_ip") or get_pi_ip(),
        "plug_reachable": state.get("plug_reachable", False),
    })

@app.route("/api/target", methods=["POST"])
def api_target():
    data = request.get_json()
    if "target_f" in data:
        state["target_f"] = float(data["target_f"])
    if "upper_f" in data:
        state["upper_f"] = float(data["upper_f"])
    if "lower_f" in data:
        state["lower_f"] = float(data["lower_f"])
    save_config({"target_f": state["target_f"], "upper_f": state["upper_f"], "lower_f": state["lower_f"]})
    return jsonify({"target_f": state["target_f"], "upper_f": state["upper_f"], "lower_f": state["lower_f"]})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    data = request.get_json()
    if data["mode"] in ("cool", "off"):
        state["mode"] = data["mode"]
        save_config({"mode": state["mode"]})
    return jsonify({"mode": state["mode"]})

@app.route("/api/kasa", methods=["POST"])
def api_kasa():
    data = request.get_json()
    state["kasa_ip"] = data.get("ip", "")
    save_config({"kasa_ip": state["kasa_ip"]})
    return jsonify({"kasa_ip": state["kasa_ip"]})

@app.route("/api/kasa/discover")
def api_kasa_discover():
    try:
        devices = kasa_discover()
        return jsonify({"devices": devices})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/network")
def api_network():
    """Return network status of Pi and Kasa plug."""
    pi_ip = state.get("pi_ip") or get_pi_ip()
    kasa_ip = state["kasa_ip"]
    plug_ok = check_plug_reachable() if kasa_ip else False
    state["plug_reachable"] = plug_ok
    return jsonify({
        "pi_ip": pi_ip,
        "pi_reachable": True,  # if you can call this endpoint, Pi is reachable
        "kasa_ip": kasa_ip,
        "kasa_reachable": plug_ok,
    })

@app.route("/api/history")
def api_history():
    rows = get_readings()
    return jsonify({
        "timestamps": [r[0] for r in rows],
        "temps": [r[1] for r in rows],
        "room_temps": [r[2] for r in rows],
        "compressor": [r[3] for r in rows],
        "states": [r[4] for r in rows],
        "watts": [r[5] for r in rows],
    })

if __name__ == "__main__":
    threading.Thread(target=control_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
