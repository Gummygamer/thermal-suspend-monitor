# Thermal Suspend Monitor

A small Linux user service that monitors CPU and NVIDIA GPU temperatures and requests system suspend when configured limits are reached.

## Policy

- CPU readings are checked once per second; NVIDIA GPU readings are checked every five seconds.
- The normal 95 °C threshold must persist for 10 seconds before suspension.
- Emergency thresholds are 100 °C for the CPU and 102 °C for the GPU, sustained for 3 seconds.
- After a thermal suspend and resume, the normal threshold is paused for 60 seconds to avoid rapid suspend/resume cycling. Emergency thresholds remain active.
- The service logs readings and suspend requests to the user journal.

The CPU sensor names currently supported are `TCPU`, `x86_pkg_temp`, and `coretemp`. GPU monitoring uses `nvidia-smi`; if it is unavailable, GPU reads are retried while CPU monitoring continues.

## Install

Copy both files into the matching user paths, then enable the service:

```sh
mkdir -p ~/.local/bin ~/.config/systemd/user
cp thermal-suspend-monitor.py ~/.local/bin/
cp thermal-suspend-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now thermal-suspend-monitor.service
```

View status and logs:

```sh
systemctl --user status thermal-suspend-monitor.service
journalctl --user -u thermal-suspend-monitor.service
```

This user service runs while the user's systemd session is active. It is a best-effort safeguard and depends on the Linux thermal sensors, NVIDIA driver, and system suspend behavior being available.
