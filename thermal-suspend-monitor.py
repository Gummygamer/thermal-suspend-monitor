#!/usr/bin/python3
"""Suspend this laptop if its CPU or NVIDIA GPU reaches a conservative limit."""
import csv
import datetime
import glob
import logging
import os
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

STATE_DIR = pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local/state") / "thermal-suspend-monitor"
EVENT_LOG = STATE_DIR / "threshold-events.csv"
EVENT_FIELDS = ["sensor", "source", "started_at", "ended_at", "duration_seconds", "peak_c", "outcome"]

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

def begin_episode(sensor, source, temperature, now):
    return {
        "sensor": sensor,
        "source": source,
        "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "started_monotonic": now,
        "peak_c": temperature,
    }

def update_episode(episode, source, temperature):
    if temperature > episode["peak_c"]:
        episode["peak_c"] = temperature
        episode["source"] = source

def close_episode(episode, outcome, now):
    if episode is None:
        return
    duration = max(0.0, now - episode["started_monotonic"])
    ended_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        new_file = not EVENT_LOG.exists() or EVENT_LOG.stat().st_size == 0
        with EVENT_LOG.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow({
                "sensor": episode["sensor"],
                "source": episode["source"],
                "started_at": episode["started_at"],
                "ended_at": ended_at,
                "duration_seconds": f"{duration:.1f}",
                "peak_c": f"{episode['peak_c']:.1f}",
                "outcome": outcome,
            })
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        logging.exception("Could not write threshold episode to %s", EVENT_LOG)
        return
    logging.info("Threshold episode ended: %s %s, %.1f seconds, peak %.1f C, outcome=%s (log: %s)",
                 episode["sensor"], episode["source"], duration, episode["peak_c"], outcome, EVENT_LOG)

def suspend(reason, episodes, outcome, now):
    for sensor, episode in episodes.items():
        close_episode(episode, outcome, now)
        episodes[sensor] = None
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
    episodes = {"CPU": None, "NVIDIA GPU": None}
    cpu_emergency_since = None
    gpu_emergency_since = None
    resume_cooldown_until = 0.0
    while True:
        now = time.monotonic()
        cpu = read_cpu_temperatures()
        cpu_max = max(value for _, value in cpu)
        cpu_hot_since = update_timer(cpu_hot_since, cpu_max >= CPU_LIMIT_MILLI_C, now)
        cpu_source, _ = max(cpu, key=lambda item: item[1])
        if cpu_max >= CPU_LIMIT_MILLI_C:
            if episodes["CPU"] is None:
                episodes["CPU"] = begin_episode("CPU", cpu_source, cpu_max / 1000, now)
            else:
                update_episode(episodes["CPU"], cpu_source, cpu_max / 1000)
        elif episodes["CPU"] is not None:
            close_episode(episodes["CPU"], "cooled_below_threshold", now)
            episodes["CPU"] = None
        cpu_emergency_since = update_timer(cpu_emergency_since, cpu_max >= CPU_EMERGENCY_MILLI_C, now)
        if now >= next_gpu_read:
            try:
                gpu_temps = read_gpu_temperatures()
                next_gpu_read = time.monotonic() + 5
                gpu_max = max(gpu_temps)
                gpu_now = time.monotonic()
                gpu_hot_since = update_timer(gpu_hot_since, gpu_max >= LIMIT_C, gpu_now)
                if gpu_max >= LIMIT_C:
                    if episodes["NVIDIA GPU"] is None:
                        episodes["NVIDIA GPU"] = begin_episode("NVIDIA GPU", "nvidia-smi", gpu_max, gpu_now)
                    else:
                        update_episode(episodes["NVIDIA GPU"], "nvidia-smi", gpu_max)
                elif episodes["NVIDIA GPU"] is not None:
                    close_episode(episodes["NVIDIA GPU"], "cooled_below_threshold", gpu_now)
                    episodes["NVIDIA GPU"] = None
                gpu_emergency_since = update_timer(gpu_emergency_since, gpu_max >= GPU_EMERGENCY_C, gpu_now)
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                gpu_temps = None
                gpu_hot_since = None
                gpu_emergency_since = None
                close_episode(episodes["NVIDIA GPU"], "sensor_unavailable", time.monotonic())
                episodes["NVIDIA GPU"] = None
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
            resume_cooldown_until = suspend(emergency_reason, episodes, "emergency_suspend", now) + RESUME_COOLDOWN_SECONDS
            cpu_hot_since = gpu_hot_since = None
            cpu_emergency_since = gpu_emergency_since = None
            logging.warning("Resumed from thermal suspend; normal threshold is paused for %s seconds", RESUME_COOLDOWN_SECONDS)
        elif now >= resume_cooldown_until:
            if cpu_hot_since is not None and now - cpu_hot_since >= SUSTAINED_SECONDS:
                source, value = max(cpu, key=lambda item: item[1])
                resume_cooldown_until = suspend(f"CPU {source} at {value / 1000:.1f} C sustained for {SUSTAINED_SECONDS} seconds (limit {LIMIT_C} C)", episodes, "suspend_requested", now) + RESUME_COOLDOWN_SECONDS
                cpu_hot_since = gpu_hot_since = None
                cpu_emergency_since = gpu_emergency_since = None
                logging.warning("Resumed from thermal suspend; normal threshold is paused for %s seconds", RESUME_COOLDOWN_SECONDS)
            elif gpu_hot_since is not None and now - gpu_hot_since >= SUSTAINED_SECONDS:
                resume_cooldown_until = suspend(f"NVIDIA GPU at {max(gpu_temps):.1f} C sustained (limit {LIMIT_C} C)", episodes, "suspend_requested", now) + RESUME_COOLDOWN_SECONDS
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
