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

# If relay behaves inverted, change active_high to False
relay = OutputDevice(23, active_high=True, initial_value=False)

# Thresholds
ADVISORY_LOW = 55.0
COOLING_HIGH = 60.0
RECOVERY_LOW = 58.0
EMERGENCY_TRIP = 75.0
OVERHEAT_DELAY = 10.0

# Persistent state
overheat_start_time = None
failure_latched = False
failure_reason = "NONE"

# Internal history buffer, backend only, no frontend change required
temperature_history = deque(maxlen=300)


def read_temp() -> float:
    matches = glob.glob("/sys/bus/w1/devices/28*")
    if not matches:
        raise RuntimeError("DS18B20 sensor not detected")

    device = matches[0]
    with open(device + "/w1_slave") as f:
        lines = f.readlines()

    if len(lines) < 2 or "t=" not in lines[1]:
        raise RuntimeError("Invalid DS18B20 sensor data")

    return float(lines[1].split("t=")[1]) / 1000.0


def apply_normal_state() -> None:
    green_led.on()
    blue_led.off()
    red_led.off()
    buzzer.off()
    relay.off()


def apply_advisory_state() -> None:
    # Keep UI appearance stable: use green as non-fault safe band
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


def latch_failure(reason: str) -> None:
    global failure_latched, failure_reason, overheat_start_time
    failure_latched = True
    failure_reason = reason
    overheat_start_time = None
    apply_failure_state()


@app.get("/")
def root():
    return {"message": "Thermal Control Backend Running"}


@app.get("/state")
def get_state():
    global overheat_start_time, failure_latched, failure_reason

    now = time.time()

    try:
        temp = read_temp()
        sensor_ok = True
    except Exception:
        temp = None
        sensor_ok = False

    # Sensor failure has highest priority
    if not sensor_ok:
        latch_failure("SENSOR_FAILURE")

    # If already latched, stay failed until manual reset
    elif failure_latched:
        apply_failure_state()

    else:
        # Immediate hard trip
        if temp >= EMERGENCY_TRIP:
            latch_failure("EMERGENCY_OVERTEMP")

        # Cooling / timed trip band
        elif temp > COOLING_HIGH:
            apply_cooling_state()

            if overheat_start_time is None:
                overheat_start_time = now
            elif now - overheat_start_time >= OVERHEAT_DELAY:
                latch_failure("CRITICAL_OVERHEAT")

        # Recovery band: below 60 but still above 58
        elif temp >= RECOVERY_LOW:
            overheat_start_time = None
            apply_cooling_state()
            failure_reason = "NONE"

        # Advisory band: 55 to below 58
        elif temp >= ADVISORY_LOW:
            overheat_start_time = None
            apply_advisory_state()
            failure_reason = "NONE"

        # Normal band: below 55
        else:
            overheat_start_time = None
            apply_normal_state()
            failure_reason = "NONE"

    # Store history internally, no frontend change required
    if temp is not None:
        temperature_history.append({
            "timestamp": round(now, 2),
            "temperature": round(temp, 3)
        })

    time_above_setpoint = 0.0
    if (
        overheat_start_time is not None
        and not failure_latched
        and temp is not None
        and temp > COOLING_HIGH
    ):
        time_above_setpoint = round(now - overheat_start_time, 2)

    # Preserve frontend-compatible fields
    if failure_latched:
        mode = "FAILURE"
    elif temp is None:
        mode = "FAILURE"
    elif temp > COOLING_HIGH:
        mode = "COOLING"
    elif temp >= RECOVERY_LOW:
        mode = "RECOVERY"
    elif temp >= ADVISORY_LOW:
        mode = "NORMAL"
    else:
        mode = "NORMAL"

    return {
        "current_temperature": temp if temp is not None else -1.0,
        "setpoint": COOLING_HIGH,
        "trip_status": failure_latched,
        "heater_on": False,
        "pump_on": bool(relay.value),
        "relay_on": bool(relay.value),
        "buzzer_on": buzzer.is_active,
        "led_heating": blue_led.is_lit,
        "led_holding": False,
        "led_fault": red_led.is_lit,
        "led_ok": green_led.is_lit,
        "mode": mode,
        "failure_mode": failure_reason,
        "screen_of_death": failure_latched,
        "time_above_setpoint": time_above_setpoint
    }


@app.post("/reset")
def reset_trip():
    global overheat_start_time, failure_latched, failure_reason

    overheat_start_time = None
    failure_latched = False
    failure_reason = "NONE"
    apply_normal_state()

    return {"message": "System reset completed"}


@app.get("/history")
def get_history():
    # Optional backend-only endpoint for future use
    return {"history": list(temperature_history)}
