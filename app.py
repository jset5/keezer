import os, time, json, sqlite3, threading
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

# --- Config ---
DB_PATH = os.path.expanduser("~/keezer/keezer.db")
SIMULATE = not os.path.exists("/sys/bus/w1/devices")
READ_INTERVAL = 30  # seconds
COMPRESSOR_MIN_OFF = 180  # 3 minutes
COMPRESSOR_MIN_ON = 120   # 2 minutes
HYSTERESIS = 2.0  # °F above/below target

app = Flask(__name__)

# --- State ---
# Modes: "cool" (normal control), "off" (logging only)
state = {
    "target_f": 36.0,
    "current_f": None,
    "room_f": None,
    "compressor_on": False,
    "last_compressor_off": 0,
    "last_compressor_on": 0,
    "mode": "cool",
    "comp_state": "idle",  # idle, cooling, wait_cool, min_cool
}

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS readings (
        ts TEXT PRIMARY KEY, temp_f REAL, room_f REAL,
        compressor INTEGER, state TEXT)""")
    conn.commit()
    conn.close()

def log_reading(temp_f, room_f, compressor_on, comp_state):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
                 (datetime.now().isoformat(), temp_f, room_f,
                  int(compressor_on), comp_state))
    conn.execute("DELETE FROM readings WHERE ts < ?",
                 ((datetime.now() - timedelta(hours=72)).isoformat(),))
    conn.commit()
    conn.close()

def get_readings(hours=72):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, temp_f, room_f, compressor, state FROM readings WHERE ts > ? ORDER BY ts",
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
    if not SIMULATE:
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
    while True:
        beer_t, room_t = read_temps()
        if beer_t is not None:
            state["current_f"] = beer_t
            state["room_f"] = room_t

            if state["mode"] == "off":
                if state["compressor_on"]:
                    set_compressor(False)
                state["comp_state"] = "idle"
            else:
                target = state["target_f"]
                off_elapsed = time.time() - state["last_compressor_off"]
                on_elapsed = time.time() - state["last_compressor_on"]

                if state["compressor_on"]:
                    if beer_t < target - HYSTERESIS:
                        if on_elapsed >= COMPRESSOR_MIN_ON:
                            set_compressor(False)
                            state["comp_state"] = "idle"
                        else:
                            state["comp_state"] = "min_cool"
                    else:
                        state["comp_state"] = "cooling"
                else:
                    if beer_t > target + HYSTERESIS:
                        if off_elapsed >= COMPRESSOR_MIN_OFF:
                            set_compressor(True)
                            state["comp_state"] = "cooling"
                        else:
                            state["comp_state"] = "wait_cool"
                    else:
                        state["comp_state"] = "idle"

            log_reading(beer_t, room_t, state["compressor_on"], state["comp_state"])
        time.sleep(READ_INTERVAL)

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "current_f": state["current_f"],
        "room_f": state["room_f"],
        "target_f": state["target_f"],
        "compressor_on": state["compressor_on"],
        "mode": state["mode"],
        "comp_state": state["comp_state"],
    })

@app.route("/api/target", methods=["POST"])
def api_target():
    data = request.get_json()
    state["target_f"] = float(data["target_f"])
    return jsonify({"target_f": state["target_f"]})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    data = request.get_json()
    if data["mode"] in ("cool", "off"):
        state["mode"] = data["mode"]
    return jsonify({"mode": state["mode"]})

@app.route("/api/history")
def api_history():
    rows = get_readings()
    return jsonify({
        "timestamps": [r[0] for r in rows],
        "temps": [r[1] for r in rows],
        "room_temps": [r[2] for r in rows],
        "compressor": [r[3] for r in rows],
        "states": [r[4] for r in rows],
    })

if __name__ == "__main__":
    threading.Thread(target=control_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
