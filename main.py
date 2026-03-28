import json
import os
from typing import Any, Dict
import glob
from gpiozero import LED, Buzzer

green_led = LED(17)
blue_led = LED(27)
red_led = LED(22)
relay = LED(23)
buzzer = Buzzer(24)

def read_temp():
	device = glob.glob('/sys/bus/w1/devices/28*')[0]
	with open(device + '/w1_slave') as f:
		lines = f.readlines()
	temp = float(lines[1].split('t=')[1])/1000
	return temp

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from models import ControlState, SetpointUpdate, ModeUpdate
from state import state

STATE_FILE = os.getenv("THERMAL_STATE_FILE", "/tmp/thermal_state.json")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "Thermal Control Backend Running"}

@app.get("/state")
def get_state():
    temp = read_temp()

    if temp > 60:
        red_led.on()
        buzzer.on()
        relay.off()
        green_led.off()
        blue_led.off()
    else:
        red_led.off()
        buzzer.off()
        green_led.on()
        blue_led.off()

    return {
        "current_temperature": temp,
        "setpoint": 60,
        "mode": "OFF",
        "trip_status": red_led.is_lit,
        "heater_on": False,
        "pump_on": True,
        "relay_on": relay.is_lit,
        "buzzer_on": buzzer.is_active,
        "led_heating": blue_led.is_lit,
        "led_holding": False,
        "led_fault": red_led.is_lit,
        "led_ok": green_led.is_lit,
        "failure_mode": "CRITICAL" if red_led.is_lit else "NONE"
    }


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _normalize_live_state(raw: Dict[str, Any]) -> Dict[str, Any]:
    current_temperature = (
        raw.get("current_temperature")
        if raw.get("current_temperature") is not None
        else raw.get("temp", state["current_temperature"])
    )
    relay_on = (
        raw.get("relay_on")
        if raw.get("relay_on") is not None
        else raw.get("relay", state.get("relay_on", state["heater_on"]))
    )

    return {
        "current_temperature": float(current_temperature),
        "setpoint": float(raw.get("setpoint", raw.get("target_high", state["setpoint"]))),
        "mode": raw.get("mode", state["mode"]),
        "trip_status": _coerce_bool(raw.get("trip_status", state["trip_status"])),
        "heater_on": _coerce_bool(raw.get("heater_on", relay_on)),
        "pump_on": _coerce_bool(raw.get("pump_on", state["pump_on"])),
        "relay_on": _coerce_bool(relay_on),
        "buzzer_on": _coerce_bool(raw.get("buzzer_on", False)),
        "led_heating": _coerce_bool(raw.get("led_heating", False)),
        "led_holding": _coerce_bool(raw.get("led_holding", False)),
        "led_fault": _coerce_bool(raw.get("led_fault", False)),
        "led_ok": _coerce_bool(raw.get("led_ok", False)),
        "failure_mode": str(raw.get("failure_mode", raw.get("status", "NONE"))),
    }


def _read_live_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return _normalize_live_state(raw)
    except (OSError, ValueError, TypeError):
        return _normalize_live_state(state)


def _write_live_state(update: Dict[str, Any]) -> None:
    current = _read_live_state()
    current.update(update)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2)
    except OSError:
        # If filesystem write fails, keep process alive with in-memory fallback.
        state.update(current)


@app.get("/state", response_model=ControlState)
def get_state():
    live = _read_live_state()
    state.update(live)
    return live


@app.get("/setpoint")
def get_setpoint():
    return {"setpoint": _read_live_state()["setpoint"]}


@app.post("/setpoint")
def update_setpoint(data: SetpointUpdate):
    live = _read_live_state()
    if live["trip_status"]:
        raise HTTPException(status_code=400, detail="Cannot change setpoint while system is tripped.")

    if data.setpoint < 10 or data.setpoint > 80:
        raise HTTPException(status_code=400, detail="Setpoint must be between 10 and 80.")

    _write_live_state({"setpoint": data.setpoint})
    return {"message": "Setpoint updated", "setpoint": data.setpoint}


@app.get("/mode")
def get_mode():
    return {"mode": _read_live_state()["mode"]}


@app.post("/mode")
def update_mode(data: ModeUpdate):
    live = _read_live_state()
    if live["trip_status"] and data.mode != "OFF":
        raise HTTPException(status_code=400, detail="System is tripped. Only OFF mode is allowed.")

    _write_live_state({"mode": data.mode})
    return {"message": "Mode updated", "mode": data.mode}


@app.post("/reset")
def reset_trip():
    _write_live_state(
        {
            "trip_status": False,
            "heater_on": False,
            "relay_on": False,
            "pump_on": True,
            "buzzer_on": False,
            "led_fault": False,
            "led_ok": True,
            "failure_mode": "NONE",
            "mode": "OFF",
        }
    )
    return {"message": "System reset to safe state"}
