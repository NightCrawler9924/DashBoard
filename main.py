import glob
import time
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from gpiozero import LED, Buzzer, OutputDevice

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GPIO mapping
green_led = LED(17)
blue_led = LED(27)
red_led = LED(22)
buzzer = Buzzer(24)

# Relay / pump control
# If your relay behaves backwards, flip active_high to False
relay = OutputDevice(23, active_high=True, initial_value=False)

SETPOINT = 60.0
OVERHEAT_DELAY = 10.0

# Persistent state
overheat_start_time = None
failure_latched = False


def read_temp() -> float:
    device = glob.glob("/sys/bus/w1/devices/28*")[0]
    with open(device + "/w1_slave") as f:
        lines = f.readlines()
    return float(lines[1].split("t=")[1]) / 1000.0


def apply_safe_state() -> None:
    green_led.on()
    blue_led.off()
    red_led.off()
    buzzer.off()
    relay.off()   # relay OFF below 60


def apply_cooling_state() -> None:
    green_led.off()
    blue_led.on()
    red_led.off()
    buzzer.off()
    relay.on()    # relay ON when above 60 and cooling


def apply_failure_state() -> None:
    green_led.off()
    blue_led.off()
    red_led.on()
    buzzer.on()
    relay.off()   # relay OFF during failure, manual shutoff needed


@app.get("/")
def root():
    return {"message": "Thermal Control Backend Running"}


@app.get("/state")
def get_state():
    global overheat_start_time, failure_latched

    temp = read_temp()
    now = time.time()

    if failure_latched:
        apply_failure_state()

    elif temp > SETPOINT:
        apply_cooling_state()

        # Start timer as soon as temperature goes above 60
        if overheat_start_time is None:
            overheat_start_time = now

        # If temperature stays above 60 for 10 seconds, trigger failure
        elif now - overheat_start_time >= OVERHEAT_DELAY:
            failure_latched = True
            apply_failure_state()

    else:
        # Safe region
        overheat_start_time = None
        apply_safe_state()

    time_above_setpoint = 0.0
    if overheat_start_time is not None and not failure_latched and temp > SETPOINT:
        time_above_setpoint = round(now - overheat_start_time, 2)

    return {
        "current_temperature": temp,
        "setpoint": SETPOINT,
        "mode": "FAILURE" if failure_latched else ("COOLING" if temp > SETPOINT else "OFF"),
        "trip_status": failure_latched,
        "heater_on": False,
        "pump_on": relay.value if not failure_latched else False,
        "relay_on": relay.value,
        "buzzer_on": buzzer.is_active,
        "led_heating": blue_led.is_lit,
        "led_holding": False,
        "led_fault": red_led.is_lit,
        "led_ok": green_led.is_lit,
        "failure_mode": "CRITICAL_OVERHEAT" if failure_latched else "NONE",
        "screen_of_death": failure_latched,
        "time_above_setpoint": time_above_setpoint
    }


@app.post("/reset")
def reset_trip():
    global overheat_start_time, failure_latched

    overheat_start_time = None
    failure_latched = False
    apply_safe_state()

    return {"message": "System reset completed"}
