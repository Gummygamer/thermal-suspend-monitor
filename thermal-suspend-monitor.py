#!/usr/bin/python3
"""Suspend this laptop if its CPU or NVIDIA GPU reaches a conservative limit."""
import glob
import logging
import pathlib
import subprocess
import time

LIMIT_C = 95
CPU_LIMIT_MILLI_C = LIMIT_C * 1000
CPU_EMERGENCY_MILLI_C = 100_000
GPU_EMERGENCY_C = 102
SUSTAINED_SECONDS = 10
EMERGENCY_SECONDS = 3
RESUME_COOLDOWN_SECONDS = 60
CPU_ZONE_TYPES = {"TCPU", "x86_pkg_temp"}
GPU_QUERY = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def read_cpu_temperatures():
    readings = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            sensor_type = pathlib.Path(zone, "type").read_text().strip()
            if sensor_type in CPU_ZONE_TYPES:
                value = int(pathlib.Path(zone, "temp").read_text().strip())
                readings.append((sensor_type, value))
        except (OSError, ValueError):
            continue
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if pathlib.Path(hwmon, "name").read_text().strip() != "coretemp":
                continue
            for sensor in glob.glob(hwmon + "/temp*_input"):
                try:
                    value = int(pathlib.Path(sensor).read_text().strip())
                    readings.append(("coretemp/" + pathlib.Path(sensor).name, value))
                except (OSError, ValueError):
                    continue
        except OSError:
            continue
    if not readings:
        raise RuntimeError("No supported CPU temperature sensors are available")
    return readings

def read_gpu_temperatures():
    result = subprocess.run(GPU_QUERY, capture_output=True, text=True, timeout=4, check=True)
    values = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line.lower() not in {"n/a", "[not supported]"}:
            values.append(float(line.split()[0]))
    if not values:
        raise RuntimeError("NVIDIA reports no GPU temperature readings")
    return values

def suspend(reason):
    logging.critical("Temperature limit reached (%s); requesting system suspend", reason)
    try:
        result = subprocess.run(["/usr/bin/systemctl", "suspend"], timeout=30, check=False)
        if result.returncode != 0:
            logging.error("Suspend request failed with exit status %s", result.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.exception("Suspend request failed: %s", exc)
    return time.monotonic()

def update_timer(started, is_hot, now):
    if not is_hot:
        return None
    return now if started is None else started

def main():
    next_gpu_read = 0.0
    last_status = 0.0
    last_gpu_error = 0.0
    gpu_temps = None
    cpu_hot_since = None
    gpu_hot_since = None
    cpu_emergency_since = None
    gpu_emergency_since = None
    resume_cooldown_until = 0.0
    while True:
        now = time.monotonic()
        cpu = read_cpu_temperatures()
        cpu_max = max(value for _, value in cpu)
        cpu_hot_since = update_timer(cpu_hot_since, cpu_max >= CPU_LIMIT_MILLI_C, now)
        cpu_emergency_since = update_timer(cpu_emergency_since, cpu_max >= CPU_EMERGENCY_MILLI_C, now)
        if now >= next_gpu_read:
            try:
                gpu_temps = read_gpu_temperatures()
                next_gpu_read = time.monotonic() + 5
                gpu_max = max(gpu_temps)
                gpu_hot_since = update_timer(gpu_hot_since, gpu_max >= LIMIT_C, time.monotonic())
                gpu_emergency_since = update_timer(gpu_emergency_since, gpu_max >= GPU_EMERGENCY_C, time.monotonic())
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                gpu_temps = None
                gpu_hot_since = None
                gpu_emergency_since = None
                next_gpu_read = time.monotonic() + 15
                if time.monotonic() - last_gpu_error >= 60:
                    logging.warning("GPU temperature unavailable; will retry: %s", exc)
                    last_gpu_error = time.monotonic()
        emergency_reason = None
        if cpu_emergency_since is not None and now - cpu_emergency_since >= EMERGENCY_SECONDS:
            source, value = max(cpu, key=lambda item: item[1])
            emergency_reason = f"CPU {source} at {value / 1000:.1f} C (emergency limit 100 C)"
        elif gpu_emergency_since is not None and now - gpu_emergency_since >= EMERGENCY_SECONDS:
            emergency_reason = f"NVIDIA GPU at {max(gpu_temps):.1f} C (emergency limit 102 C)"
        if emergency_reason:
            resume_cooldown_until = suspend(emergency_reason) + RESUME_COOLDOWN_SECONDS
            cpu_hot_since = gpu_hot_since = None
            cpu_emergency_since = gpu_emergency_since = None
            logging.warning("Resumed from thermal suspend; normal threshold is paused for %s seconds", RESUME_COOLDOWN_SECONDS)
        elif now >= resume_cooldown_until:
            if cpu_hot_since is not None and now - cpu_hot_since >= SUSTAINED_SECONDS:
                source, value = max(cpu, key=lambda item: item[1])
                resume_cooldown_until = suspend(f"CPU {source} at {value / 1000:.1f} C sustained for {SUSTAINED_SECONDS} seconds (limit {LIMIT_C} C)") + RESUME_COOLDOWN_SECONDS
                cpu_hot_since = gpu_hot_since = None
                cpu_emergency_since = gpu_emergency_since = None
                logging.warning("Resumed from thermal suspend; normal threshold is paused for %s seconds", RESUME_COOLDOWN_SECONDS)
            elif gpu_hot_since is not None and now - gpu_hot_since >= SUSTAINED_SECONDS:
                resume_cooldown_until = suspend(f"NVIDIA GPU at {max(gpu_temps):.1f} C sustained (limit {LIMIT_C} C)") + RESUME_COOLDOWN_SECONDS
                cpu_hot_since = gpu_hot_since = None
                cpu_emergency_since = gpu_emergency_since = None
                logging.warning("Resumed from thermal suspend; normal threshold is paused for %s seconds", RESUME_COOLDOWN_SECONDS)
        if now - last_status >= 60:
            cpu_summary = ", ".join(f"{name}={value / 1000:.1f} C" for name, value in cpu)
            gpu_summary = "unavailable" if gpu_temps is None else ", ".join(f"{value:.1f} C" for value in gpu_temps)
            logging.info("CPU sensors: %s; NVIDIA GPU: %s; suspend limit: %s C", cpu_summary, gpu_summary, LIMIT_C)
            last_status = now
        time.sleep(1)

if __name__ == "__main__":
    main()
