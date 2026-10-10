#!/usr/bin/env python3
"""Live CPU and NVIDIA GPU chart in a translucent GTK4 glass-style window."""
from collections import deque
import glob
import io
import pathlib
import queue
import subprocess
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

WINDOW_SECONDS = 15 * 60
SAMPLE_SECONDS = 2
GPU_SAMPLE_SECONDS = 5
GPU_QUERY = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
CPU_ZONE_TYPES = {"TCPU", "x86_pkg_temp"}
LIMIT_C = 95

BG = (0.055, 0.075, 0.095)
GRID = (0.78, 0.86, 0.94)
TEXT = (0.93, 0.96, 1.0)
CPU_COLOR = (0.20, 0.78, 0.98)
GPU_COLOR = (1.00, 0.68, 0.24)
LIMIT_COLOR = (1.00, 0.43, 0.40)

GLASS_CSS = b"""
window.glass-window {
  background-color: rgba(22, 32, 44, 0.24);
  background-image: linear-gradient(145deg,
    rgba(255, 255, 255, 0.23),
    rgba(255, 255, 255, 0.08) 44%,
    rgba(84, 154, 194, 0.12));
  border: 1px solid rgba(255, 255, 255, 0.36);
  border-radius: 18px;
}
headerbar.titlebar {
  background-color: rgba(255, 255, 255, 0.10);
  background-image: linear-gradient(90deg,
    rgba(255, 255, 255, 0.13), rgba(255, 255, 255, 0.025));
  box-shadow: none;
  border-bottom: 1px solid rgba(255, 255, 255, 0.20);
  border-radius: 17px 17px 0 0;
}
.glass-card {
  background-color: rgba(255, 255, 255, 0.105);
  background-image: linear-gradient(135deg,
    rgba(255, 255, 255, 0.14), rgba(255, 255, 255, 0.025));
  border: 1px solid rgba(255, 255, 255, 0.24);
  border-radius: 12px;
}
.dim-label { color: rgba(240, 247, 255, 0.78); }
"""


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


class TemperatureWidget(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.github.gummygamer.ThermalSuspendMonitor")
        self.samples = deque(maxlen=WINDOW_SECONDS // SAMPLE_SECONDS + 2)
        self.gpu_results = queue.SimpleQueue()
        self.gpu_temperature = None
        self.cpu_temperature = None
        self.last_gpu_read = 0.0
        self.gpu_poll_in_flight = False
        self.window = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return

        provider = Gtk.CssProvider()
        provider.load_from_data(GLASS_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.window = Gtk.ApplicationWindow(application=self, title="Thermal Monitor")
        self.window.add_css_class("glass-window")
        self.window.set_default_size(500, 350)
        self.window.set_size_request(400, 290)

        header = Gtk.HeaderBar()
        header.add_css_class("titlebar")
        header.set_title_widget(Gtk.Label(label="Thermal Monitor"))
        self.window.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(10)
        root.set_margin_start(14)
        root.set_margin_end(14)
        self.window.set_child(root)

        title = Gtk.Label()
        title.set_markup('<span size="large" weight="bold">Temperature history</span>')
        title.set_xalign(0)
        root.append(title)
        subtitle = Gtk.Label(label="CPU and NVIDIA GPU · last 15 minutes")
        subtitle.set_xalign(0)
        subtitle.add_css_class("dim-label")
        root.append(subtitle)

        cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        root.append(cards)
        self.cpu_value = self._make_card(cards, "CPU", CPU_COLOR)
        self.gpu_value = self._make_card(cards, "NVIDIA GPU", GPU_COLOR)

        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        legend.set_margin_top(1)
        root.append(legend)
        self._add_legend_item(legend, "CPU", CPU_COLOR)
        self._add_legend_item(legend, "NVIDIA GPU", GPU_COLOR)
        self._add_legend_item(legend, "95 °C suspend", LIMIT_COLOR)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.FILL)
        self.picture.set_size_request(-1, 175)
        self.picture.set_vexpand(True)
        root.append(self.picture)

        footer = Gtk.Label(label="Emergency: CPU 100 °C · GPU 102 °C")
        footer.set_xalign(0)
        footer.add_css_class("dim-label")
        root.append(footer)

        GLib.timeout_add(SAMPLE_SECONDS * 1000, self.update_readings)
        self.update_readings()
        self.window.present()

    @staticmethod
    def _make_card(parent, name, color):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("glass-card")
        card.set_hexpand(True)
        card.set_margin_top(1)
        card.set_margin_bottom(1)
        card.set_margin_start(1)
        card.set_margin_end(1)
        card.set_margin_top(7)
        card.set_margin_bottom(7)
        card.set_margin_start(10)
        card.set_margin_end(10)
        heading = Gtk.Label(label=name)
        heading.set_xalign(0)
        heading.set_markup(f'<span foreground="#{int(color[0]*255):02x}{int(color[1]*255):02x}{int(color[2]*255):02x}" weight="bold">{name}</span>')
        card.append(heading)
        value = Gtk.Label(label="— °C")
        value.set_xalign(0)
        value.set_markup('<span size="x-large" weight="bold">— °C</span>')
        card.append(value)
        parent.append(card)
        return value

    @staticmethod
    def _add_legend_item(parent, label, color):
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        color_hex = "#" + "".join(f"{int(channel * 255):02x}" for channel in color)
        swatch = Gtk.Label()
        swatch.set_markup(f'<span foreground="{color_hex}" weight="bold">━</span>')
        item.append(swatch)
        text = Gtk.Label(label=label)
        text.add_css_class("dim-label")
        item.append(text)
        parent.append(item)

    def update_readings(self):
        now = time.time()
        try:
            self.cpu_temperature = read_cpu_temperature()
            self.cpu_value.set_markup(f'<span size="x-large" weight="bold">{self.cpu_temperature:.1f} °C</span>')
        except (OSError, ValueError, RuntimeError):
            self.cpu_temperature = None
            self.cpu_value.set_text("Unavailable")

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
        if self.gpu_temperature is None:
            self.gpu_value.set_text("Unavailable")
        else:
            self.gpu_value.set_markup(f'<span size="x-large" weight="bold">{self.gpu_temperature:.1f} °C</span>')

        self.samples.append((now, self.cpu_temperature, self.gpu_temperature))
        cutoff = now - WINDOW_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        self.render_chart()
        return GLib.SOURCE_CONTINUE

    def _read_gpu_in_background(self):
        try:
            self.gpu_results.put((read_gpu_temperature(), None))
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            self.gpu_results.put((None, str(exc)))

    @staticmethod
    def _text(context, x, y, value, size=10, alpha=0.88):
        context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        context.set_font_size(size)
        context.set_source_rgba(*TEXT, alpha)
        context.move_to(x, y)
        context.show_text(value)

    def render_chart(self):
        width = max(380, self.picture.get_width())
        height = max(150, self.picture.get_height())
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        context = cairo.Context(surface)
        context.set_operator(cairo.OPERATOR_CLEAR)
        context.paint()
        context.set_operator(cairo.OPERATOR_OVER)
        context.set_source_rgba(*BG, 0.10)
        context.paint()

        left, right, top, bottom = 48, width - 10, 10, height - 27
        plot_width, plot_height = max(1, right - left), max(1, bottom - top)
        low, high = 30.0, 105.0
        now = time.time()

        def y_for(value):
            return bottom - (value - low) / (high - low) * plot_height

        context.set_line_width(1)
        for value in (30, 50, 70, 90, 100):
            y = y_for(value)
            context.set_source_rgba(*GRID, 0.30)
            context.move_to(left, y)
            context.line_to(right, y)
            context.stroke()
            self._text(context, 3, y + 3, f"{value}°", 9, 0.84)
        context.set_source_rgba(*LIMIT_COLOR, 0.9)
        context.set_dash([5, 4])
        context.move_to(left, y_for(LIMIT_C))
        context.line_to(right, y_for(LIMIT_C))
        context.stroke()
        context.set_dash([])

        for minutes in (15, 10, 5, 0):
            x = left + plot_width * (1 - minutes / 15)
            context.set_source_rgba(*GRID, 0.24)
            context.move_to(x, top)
            context.line_to(x, bottom)
            context.stroke()
            label = "Now" if minutes == 0 else f"−{minutes} min"
            self._text(context, x - (17 if minutes == 0 else 21), height - 5, label, 9, 0.84)

        for index, color in ((1, CPU_COLOR), (2, GPU_COLOR)):
            context.set_source_rgb(*color)
            context.set_line_width(2.4)
            context.set_line_join(cairo.LINE_JOIN_ROUND)
            drawing = False
            for stamp, cpu, gpu in self.samples:
                value = cpu if index == 1 else gpu
                if value is None:
                    drawing = False
                    continue
                x = left + plot_width * max(0.0, min(1.0, 1 - (now - stamp) / WINDOW_SECONDS))
                y = y_for(value)
                if not drawing:
                    context.move_to(x, y)
                    drawing = True
                else:
                    context.line_to(x, y)
            context.stroke()

        png = io.BytesIO()
        surface.write_to_png(png)
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png.getvalue()))
        self.picture.set_paintable(texture)


if __name__ == "__main__":
    TemperatureWidget().run(None)
