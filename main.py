import glob
import time
from collections import deque

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

# If relay behaves backwards, change active_high to False
relay = OutputDevice(23, active_high=True, initial_value=False)

# Thresholds
COOLING_HIGH = 30.0
NORMAL_LOW = 28.0
OVERHEAT_DELAY = 20.0

# Persistent state: timer only
overheat_start_time = None
sensor_fault_count = 0
SENSOR_FAULT_LIMIT = 3

# Internal history
temperature_history = deque(maxlen=300)


def read_temp() -> float:
    matches = glob.glob("/sys/bus/w1/devices/28*")
    if not matches:
        raise RuntimeError("DS18B20 sensor not detected")

    device_file = matches[0] + "/w1_slave"

    for _ in range(5):
        with open(device_file) as f:
            lines = f.readlines()

        if len(lines) >= 2 and lines[0].strip().endswith("YES") and "t=" in lines[1]:
            return float(lines[1].split("t=")[1]) / 1000.0

        time.sleep(0.2)

    raise RuntimeError("Invalid DS18B20 sensor data")


def apply_normal_state() -> None:
    green_led.on()
    blue_led.off()
    red_led.off()
    buzzer.off()
    relay.off()


def apply_cooling_state() -> None:
    green_led.off()
    blue_led.on()
    red_led.off()
    buzzer.off()
    relay.on()


def apply_failure_state() -> None:
    green_led.off()
    blue_led.off()
    red_led.on()
    buzzer.on()
    relay.off()


@app.get("/")
def root():
    return {"message": "Thermal Control Backend Running"}


@app.get("/state")
def get_state():
    global overheat_start_time, sensor_fault_count

    now = time.time()

    try:
        temp = read_temp()
        sensor_fault_count = 0
        sensor_ok = True
    except Exception:
        temp = None
        sensor_ok = False
        sensor_fault_count += 1

    failure_active = False
    failure_mode = "NONE"
    mode = "NORMAL"
    time_above_setpoint = 0.0

    if not sensor_ok:
        if sensor_fault_count >= SENSOR_FAULT_LIMIT:
            apply_failure_state()
            failure_active = True
            failure_mode = "SENSOR_FAILURE"
            mode = "FAILURE"
        else:
            apply_normal_state()
        overheat_start_time = None

    else:
        # STATE 1: Above 60 -> blue + pump immediately
        if temp > COOLING_HIGH:
            if overheat_start_time is None:
                overheat_start_time = now

            time_above_setpoint = round(now - overheat_start_time, 2)

            # After 20 continuous seconds above 60 -> failure
            if time_above_setpoint >= OVERHEAT_DELAY:
                apply_failure_state()
                failure_active = True
                failure_mode = "CRITICAL_OVERHEAT"
                mode = "FAILURE"
            else:
                apply_cooling_state()
                mode = "COOLING"

        # STATE 2: Below 60 but still >= 58 -> red/screen OFF, blue ON
        elif temp >= NORMAL_LOW:
            overheat_start_time = None
            apply_cooling_state()
            mode = "RECOVERY"

        # STATE 3: Below 58 -> blue OFF, green ON only
        else:
            overheat_start_time = None
            apply_normal_state()
            mode = "NORMAL"

        temperature_history.append({
            "timestamp": round(now, 2),
            "temperature": round(temp, 3)
        })

    return {
        "current_temperature": temp if temp is not None else -1.0,
        "setpoint": COOLING_HIGH,
        "trip_status": failure_active,
        "heater_on": False,
        "pump_on": bool(relay.value),
        "relay_on": bool(relay.value),
        "buzzer_on": buzzer.is_active,
        "led_heating": blue_led.is_lit,
        "led_holding": False,
        "led_fault": red_led.is_lit,
        "led_ok": green_led.is_lit,
        "mode": mode,
        "failure_mode": failure_mode,
        "screen_of_death": failure_active,
        "time_above_setpoint": time_above_setpoint
    }


@app.post("/reset")
def reset_trip():
    global overheat_start_time, sensor_fault_count

    overheat_start_time = None
    sensor_fault_count = 0
    apply_normal_state()

    return {"message": "System reset completed"}


@app.get("/history")
def get_history():
    return {"history": list(temperature_history)}
