# Thermal Suspend Monitor

A small Linux user service that monitors CPU and NVIDIA GPU temperatures and requests system suspend when configured limits are reached.

It also includes a compact GTK4 glass-style window that plots CPU and NVIDIA GPU temperatures over a rolling 15-minute period, with a translucent surface and crisp graph/text layers.

## Policy

- CPU readings are checked once per second; NVIDIA GPU readings are checked every five seconds.
- The normal 95 °C threshold must persist for 10 seconds before suspension.
- Emergency thresholds are 100 °C for the CPU and 102 °C for the GPU, sustained for 3 seconds.
- After a thermal suspend and resume, the normal threshold is paused for 60 seconds to avoid rapid suspend/resume cycling. Emergency thresholds remain active.
- The service logs readings and suspend requests to the user journal, and appends completed excursions at or above 95 °C to `~/.local/state/thermal-suspend-monitor/threshold-events.csv` (or `$XDG_STATE_HOME/thermal-suspend-monitor/threshold-events.csv`).
- Each CSV row records the sensor/source, local start and end timestamps, measured duration, peak temperature, and outcome (`cooled_below_threshold`, `suspend_requested`, `emergency_suspend`, or `sensor_unavailable`). CPU duration is sampled to about one-second resolution; GPU duration to about five-second resolution. An excursion that is still active when suspension is requested is closed at the request time, so suspended time is excluded.

The CPU sensor names currently supported are `TCPU`, `x86_pkg_temp`, and `coretemp`. GPU monitoring uses `nvidia-smi`; if it is unavailable, GPU reads are retried while CPU monitoring continues.

## Install

Copy both files into the matching user paths, then enable the service:

```sh
mkdir -p ~/.local/bin ~/.config/systemd/user
cp thermal-suspend-monitor.py ~/.local/bin/
cp thermal-suspend-monitor.service ~/.config/systemd/user/
cp thermal-temperature-widget.py ~/.local/bin/
systemctl --user daemon-reload
systemctl --user enable --now thermal-suspend-monitor.service
```

The graph window uses GTK4 and Pycairo (`python3-gi`, `gir1.2-gtk-4.0`, and `python3-cairo` on Ubuntu). Launch it with:

```sh
python3 ~/.local/bin/thermal-temperature-widget.py
```

The graph samples CPU sensors every two seconds and the NVIDIA GPU every five seconds. It displays the maximum supported CPU sensor reading and marks the 95 °C suspend threshold.

View status and logs:

```sh
systemctl --user status thermal-suspend-monitor.service
journalctl --user -u thermal-suspend-monitor.service
column -s, -t < ~/.local/state/thermal-suspend-monitor/threshold-events.csv
```

This user service runs while the user's systemd session is active. It is a best-effort safeguard and depends on the Linux thermal sensors, NVIDIA driver, and system suspend behavior being available.
