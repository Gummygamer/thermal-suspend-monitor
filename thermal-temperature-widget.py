#!/usr/bin/env python3
"""Live CPU and NVIDIA GPU temperature chart using Python's Tk GUI toolkit."""
from collections import deque
import glob
import pathlib
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

WINDOW_SECONDS = 15 * 60
SAMPLE_SECONDS = 2
GPU_SAMPLE_SECONDS = 5
GPU_QUERY = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
CPU_ZONE_TYPES = {"TCPU", "x86_pkg_temp"}
LIMIT_C = 95

BG = "#111821"
CARD = "#1b2632"
FG = "#e4edf5"
DIM = "#9cabb9"
GRID = "#34414e"
CPU_COLOR = "#35c7f4"
GPU_COLOR = "#ffad3d"
LIMIT_COLOR = "#ff6e69"


def read_cpu_temperature():
    readings = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            sensor_type = pathlib.Path(zone, "type").read_text().strip()
            if sensor_type in CPU_ZONE_TYPES:
                readings.append(int(pathlib.Path(zone, "temp").read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if pathlib.Path(hwmon, "name").read_text().strip() != "coretemp":
                continue
            for sensor in glob.glob(hwmon + "/temp*_input"):
                try:
                    readings.append(int(pathlib.Path(sensor).read_text().strip()) / 1000)
                except (OSError, ValueError):
                    continue
        except OSError:
            continue
    if not readings:
        raise RuntimeError("No supported CPU temperature sensors are available")
    return max(readings)


def read_gpu_temperature():
    result = subprocess.run(GPU_QUERY, capture_output=True, text=True, timeout=4, check=True)
    values = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line.lower() not in {"n/a", "[not supported]"}:
            values.append(float(line.split()[0]))
    return max(values) if values else None


class TemperatureWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Thermal Monitor")
        self.root.geometry("500x350")
        self.root.minsize(400, 290)
        self.root.configure(background=BG)
        try:
            self.root.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        self.samples = deque(maxlen=WINDOW_SECONDS // SAMPLE_SECONDS + 2)
        self.last_gpu_read = 0.0
        self.gpu_temperature = None
        self.gpu_results = queue.SimpleQueue()
        self.gpu_poll_in_flight = False
        self.cpu_temperature = None
        self._build_ui()
        self.update_readings()

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Sans", 14, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=DIM, font=("Sans", 9))
        style.configure("CPUName.TLabel", background=CARD, foreground=CPU_COLOR, font=("Sans", 9, "bold"))
        style.configure("GPUName.TLabel", background=CARD, foreground=GPU_COLOR, font=("Sans", 9, "bold"))
        style.configure("Value.TLabel", background=CARD, foreground=FG, font=("Sans", 18, "bold"))
        style.configure("Footer.TLabel", background=BG, foreground=DIM, font=("Sans", 8))

        outer = ttk.Frame(self.root, padding=(12, 10))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Temperature history", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="CPU and NVIDIA GPU · last 15 minutes", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 8))

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(0, 8))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        self.cpu_value = self._make_card(cards, 0, "CPU", "CPUName.TLabel")
        self.gpu_value = self._make_card(cards, 1, "NVIDIA GPU", "GPUName.TLabel")

        self.chart = tk.Canvas(outer, background=BG, highlightthickness=0, height=190)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda _event: self.draw_chart())
        ttk.Label(
            outer,
            text="95 °C for 10 s · emergency CPU 100 °C / GPU 102 °C",
            style="Footer.TLabel",
        ).pack(anchor="w", pady=(10, 0))

    @staticmethod
    def _make_card(parent, column, name, name_style):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(10, 6))
        card.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
        ttk.Label(card, text=name, style=name_style).pack(anchor="w")
        value = ttk.Label(card, text="— °C", style="Value.TLabel")
        value.pack(anchor="w", pady=(3, 0))
        return value

    def update_readings(self):
        now = time.time()
        try:
            self.cpu_temperature = read_cpu_temperature()
            self.cpu_value.configure(text=f"{self.cpu_temperature:.1f} °C")
        except (OSError, ValueError, RuntimeError):
            self.cpu_temperature = None
            self.cpu_value.configure(text="Unavailable")

        while True:
            try:
                self.gpu_temperature, _error = self.gpu_results.get_nowait()
                self.gpu_poll_in_flight = False
            except queue.Empty:
                break
        if now - self.last_gpu_read >= GPU_SAMPLE_SECONDS and not self.gpu_poll_in_flight:
            self.last_gpu_read = now
            self.gpu_poll_in_flight = True
            threading.Thread(target=self._read_gpu_in_background, daemon=True).start()
        self.gpu_value.configure(
            text="Unavailable" if self.gpu_temperature is None else f"{self.gpu_temperature:.1f} °C"
        )

        self.samples.append((now, self.cpu_temperature, self.gpu_temperature))
        cutoff = now - WINDOW_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        self.draw_chart()
        self.root.after(SAMPLE_SECONDS * 1000, self.update_readings)

    def _read_gpu_in_background(self):
        try:
            self.gpu_results.put((read_gpu_temperature(), None))
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            self.gpu_results.put((None, str(exc)))

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        left, right, top, bottom = 58, width - 20, 22, height - 42
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        low, high = 30.0, 105.0
        now = time.time()

        def y_for(value):
            return bottom - (value - low) / (high - low) * plot_height

        for value in (30, 50, 70, 90, 95, 100):
            y = y_for(value)
            color = LIMIT_COLOR if value == LIMIT_C else GRID
            dash = (5, 4) if value == LIMIT_C else ()
            canvas.create_line(left, y, right, y, fill=color, dash=dash, width=1)
            canvas.create_text(8, y, anchor="w", text=f"{value}°", fill=DIM, font=("Sans", 9))
        canvas.create_text(right, y_for(LIMIT_C) - 8, anchor="e", text="95 °C suspend limit", fill=LIMIT_COLOR, font=("Sans", 9))

        for minutes in (15, 10, 5, 0):
            x = left + plot_width * (1 - minutes / 15)
            canvas.create_line(x, top, x, bottom, fill=GRID, width=1)
            label = "Now" if minutes == 0 else f"−{minutes} min"
            canvas.create_text(x, height - 16, text=label, fill=DIM, font=("Sans", 9))

        for index, color in ((1, CPU_COLOR), (2, GPU_COLOR)):
            segment = []
            for stamp, cpu, gpu in self.samples:
                value = cpu if index == 1 else gpu
                if value is None:
                    self._draw_segment(canvas, segment, color)
                    segment = []
                    continue
                x = left + plot_width * max(0.0, min(1.0, 1 - (now - stamp) / WINDOW_SECONDS))
                segment.extend((x, y_for(value)))
            self._draw_segment(canvas, segment, color)

        legend_y = top + 14
        canvas.create_line(left + 8, legend_y, left + 28, legend_y, fill=CPU_COLOR, width=3)
        canvas.create_text(left + 34, legend_y, anchor="w", text="CPU", fill=FG, font=("Sans", 9))
        canvas.create_line(left + 92, legend_y, left + 112, legend_y, fill=GPU_COLOR, width=3)
        canvas.create_text(left + 118, legend_y, anchor="w", text="NVIDIA GPU", fill=FG, font=("Sans", 9))

    @staticmethod
    def _draw_segment(canvas, points, color):
        if len(points) >= 4:
            canvas.create_line(*points, fill=color, width=2.5, smooth=True, splinesteps=12)
        elif len(points) == 2:
            x, y = points
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TemperatureWidget().run()
