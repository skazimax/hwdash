#!/usr/bin/env python3
"""hwdash — a tiny self-hosted dashboard for hardware temperatures and load.

One process, Python standard library only: a collector thread samples hwmon
sensors, NVIDIA/AMD GPU, CPU, memory and disk every few seconds into SQLite,
and a small HTTP server serves a single-page UI plus a JSON API.

API:
  GET /api/now                 current values + sensor metadata
  GET /api/history?range=24h   bucketed history (1h|6h|24h|48h|7d) + baselines
  GET /api/config              tile settings made in the UI (hidden / large / names)
  PUT /api/config              save tile settings

With [sunshine] enabled (see sunshine.py) also:
  GET  /api/topology           network map with live reachability (needs [topology])
  GET  /api/stream             Moonlight session, paired devices, schedules, today
  POST /api/stream/login       {"pin"} -> {"token"} for changes (X-Hwdash-Token header)
  PUT  /api/stream/policy      {"uuid", "policy"} — mode, weekly windows, daily limits
  POST /api/stream/action      {"uuid", "action", "minutes"} — extend, pause, end, clear, close_game
"""
import argparse
import getpass
import glob
import json
import logging
import math
import os
import random
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sunshine
import topology

__version__ = "1.3.0"

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_CONFIG = Path.home() / ".config/hwdash/config.toml"
SERVER_DEFAULTS = {
    "bind": "0.0.0.0",
    "port": 8090,
    "step": 10,  # seconds between samples
    "retention_hours": 168,
    "db": "~/.local/share/hwdash/metrics.db",
    "ui_config": "~/.config/hwdash/ui.json",
    "title": "",  # page title; empty = hostname
    "disk": "",  # block device for I/O stats; empty = the one holding disk_mount
    "disk_mount": "/",
}
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "48h": 48 * 3600, "7d": 7 * 24 * 3600}
MAX_POINTS = 288  # per series per history response
BASELINE_TTL = 600  # baselines are recomputed every 10 min
BASELINE_SAMPLES = 20000  # more rows than this are thinned out
SENSOR_FIELDS = {"title", "group", "warn", "focus", "hidden"}

# Built-in defaults for common hwmon drivers:
# (driver regex, label regex or None = any, title, group, warn °C or None, flags).
# The title may use regex groups ({0}) and {driver}/{label}. warn None = take
# temp*_max / temp*_crit from sysfs, else 80. Flags: "focus" — large tile at the
# top, "hidden" — hidden until enabled in the UI. List order = tile order.
SENSOR_RULES = [
    (r"k10temp", r"Tctl", "CPU", "cpu", 90, "focus"),
    (r"zenpower", r"Tdie", "CPU", "cpu", 90, "focus"),
    (r"coretemp", r"Package id (\d+)", "CPU package {0}", "cpu", None, "focus"),
    (r"nvidia", None, "GPU", "gpu", 83, "focus"),
    (r"k10temp|zenpower", r"Tccd(\d+)", "CPU CCD{0}", "cpu", 90, ""),
    (r"k10temp", r"Tdie", "CPU die", "cpu", 90, ""),
    (r"coretemp", r"Core (\d+)", "CPU core {0}", "cpu", None, "hidden"),
    (r"amdgpu", r"edge", "AMD GPU", "gpu", 90, ""),
    (r"amdgpu", r"junction", "AMD GPU hotspot", "gpu", 100, ""),
    (r"amdgpu", r"mem", "AMD GPU memory", "gpu", 95, ""),
    (r"nct6|nct67|it8|w83|f71|asus|gigabyte|dell|thinkpad", None, "{driver} {label}", "board", None, ""),
    (r"acpitz", None, "ACPI zone", "board", None, ""),
    (r"nvme", r"Composite", "NVMe", "storage", None, ""),
    (r"nvme", r"Sensor (\d+)", "NVMe sensor {0}", "storage", None, ""),
    (r"drivetemp", None, "Drive", "storage", 55, ""),
    (r"spd5118|jc42", None, "Memory DIMM", "memory", 55, ""),
    (r"iwlwifi|mt79|mt76|ath1\d|rtw", None, "Wi-Fi", "net", 80, ""),
    (r"r8169|r8125|igc|igb|e1000e|atlantic|ixgbe|mlx|bnxt", None, "Ethernet", "net", None, ""),
]

log = logging.getLogger("hwdash")


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def load_settings(path):
    """Settings from config.toml: [server], [sunshine], [topology] and [sensors."<key>"] overrides."""
    settings = {"server": dict(SERVER_DEFAULTS), "sunshine": dict(sunshine.SUNSHINE_DEFAULTS),
                "topology": dict(topology.TOPOLOGY_DEFAULTS), "sensors": {}}
    if path is None:
        return settings
    with open(path, "rb") as f:
        data = tomllib.load(f)
    server, stream, sensors = data.get("server", {}), data.get("sunshine", {}), data.get("sensors", {})
    net = data.get("topology", {})
    for name, section, defaults in (("server", server, SERVER_DEFAULTS), ("sunshine", stream, sunshine.SUNSHINE_DEFAULTS),
                                    ("topology", net, topology.TOPOLOGY_DEFAULTS)):
        unknown = set(section) - set(defaults)
        if unknown:
            raise SystemExit(f"{path}: unknown [{name}] options: {', '.join(sorted(unknown))}")
    for key, opts in sensors.items():
        bad = set(opts) - SENSOR_FIELDS
        if bad:
            raise SystemExit(f"{path}: [sensors.\"{key}\"] unknown fields: {', '.join(sorted(bad))}")
    settings["server"].update(server)
    settings["sunshine"].update(stream)
    settings["topology"].update(net)
    settings["sensors"] = sensors
    return settings


def sensor_meta(driver, label, kind, value, base=None):
    """Default title, group, threshold and flags for a sensor (before config overrides)."""
    if kind == "fan":
        m = re.fullmatch(r"fan(\d+)", label)
        return {"kind": "fan", "title": f"Fan {m[1]}" if m else label, "group": "fans",
                "order": int(m[1]) if m else 99, "focus": False, "hidden": value == 0}
    for order, (drv, lbl, title, group, warn, flags) in enumerate(SENSOR_RULES):
        if not re.match(drv, driver):
            continue
        m = re.fullmatch(lbl, label) if lbl else re.match("", label)
        if not m:
            continue
        if warn is None:
            limit = base and (read_text(base + "_max") or read_text(base + "_crit"))
            warn = int(limit) / 1000 if limit and limit.lstrip("-").isdigit() else 0
            warn = warn if 20 < warn < 150 else 80
        return {"kind": "temp", "title": title.format(*m.groups(), driver=driver, label=label),
                "group": group, "warn": warn, "order": order, "focus": "focus" in flags,
                "hidden": "hidden" in flags or not 0 < value < 127}
    return {"kind": "temp", "title": f"{driver} {label}", "group": "other", "warn": 80,
            "order": len(SENSOR_RULES), "focus": False, "hidden": not 0 < value < 127}


def root_block_device(mount):
    """Whole-disk block device name (e.g. nvme0n1) holding the given mount point."""
    st = os.stat(mount)
    path = f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    if not os.path.exists(path):  # btrfs/zfs report an anonymous device: look up the mount source
        for line in (read_text("/proc/self/mountinfo") or "").splitlines():
            left, right = line.split(" - ")
            if left.split()[4] == mount and right.split()[1].startswith("/dev/"):
                path = "/sys/class/block/" + os.path.basename(os.path.realpath(right.split()[1]))
                break
        else:
            return None
    path = os.path.realpath(path)
    if os.path.exists(os.path.join(path, "partition")):
        path = os.path.dirname(path)
    return os.path.basename(path)


def clean_config(obj):
    """Validates UI tile settings: {"hidden"|"focus": {key: bool}, "names": {key: str}}."""
    if not isinstance(obj, dict):
        raise ValueError("config must be an object")
    hidden, focus, names = obj.get("hidden", {}), obj.get("focus", {}), obj.get("names", {})
    if not all(isinstance(x, dict) for x in (hidden, focus, names)) or len(hidden) + len(focus) + len(names) > 1500:
        raise ValueError("bad config shape")
    return {
        "hidden": {k: bool(v) for k, v in hidden.items() if len(k) <= 100},
        "focus": {k: bool(v) for k, v in focus.items() if len(k) <= 100},
        "names": {k: v.strip()[:60] for k, v in names.items()
                  if len(k) <= 100 and isinstance(v, str) and v.strip()},
    }


class UiConfig:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            return clean_config(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return {"hidden": {}, "focus": {}, "names": {}}

    def save(self, cfg):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
        tmp.replace(self.path)


class Collector:
    """Takes one set of metrics per sample(); keeps previous counters for rates."""

    def __init__(self, server, overrides):
        self.overrides = overrides
        self.disk_mount = server["disk_mount"]
        self.disk_dev = server["disk"] or root_block_device(self.disk_mount)
        self.prev_cpu = None
        self.prev_disk = None
        self.meta = {}
        self.dupes = {}
        self.info = {
            "host": socket.gethostname(),
            "title": server["title"] or socket.gethostname(),
            "cpu_model": self._cpu_model(),
            "cpu_threads": os.cpu_count(),
            "gpu_name": None,
            "disk": self.disk_dev,
        }

    @staticmethod
    def _cpu_model():
        for line in (read_text("/proc/cpuinfo") or "").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
        return None

    def register(self, key, driver, label, kind, value, base=None):
        """Metadata for a sensor seen for the first time; config.toml fields win."""
        if key in self.meta:
            return
        meta = sensor_meta(driver, label, kind, value, base)
        n = self.dupes[meta["title"]] = self.dupes.get(meta["title"], 0) + 1
        if n > 1:  # two NVMe drives, two DIMMs
            meta["title"] += f" #{n}"
        meta.update(self.overrides.get(key, {}))
        self.meta[key] = meta

    def hwmon(self, values):
        seen = {}
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=lambda p: int(p.rsplit("hwmon", 1)[1])):
            driver = read_text(f"{d}/name")
            if not driver:
                continue
            for kind in ("temp", "fan"):
                for inp in sorted(glob.glob(f"{d}/{kind}*_input")):
                    base = inp[: -len("_input")]
                    raw = read_text(inp)
                    if raw is None or not raw.lstrip("-").isdigit():
                        continue
                    label = read_text(base + "_label") or os.path.basename(base)
                    key = f"{kind[0]}:{driver}/{label}"
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] > 1:  # same driver+label twice (second NVMe, duplicated channels)
                        key += f"#{seen[key]}"
                    value = int(raw) / 1000 if kind == "temp" else int(raw)
                    values[key] = value
                    self.register(key, driver, label, kind, value, base)

    def nvidia(self, values):
        fields = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"
        try:
            out = subprocess.run(
                ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            return
        if not out:
            return
        parts = [p.strip() for p in out[0].split(",")]
        self.info["gpu_name"] = parts[0].removeprefix("NVIDIA ").removeprefix("GeForce ")
        for key, raw in zip(("t:gpu", "gpu_util", "gpu_mem", "gpu_mem_total", "gpu_w", "gpu_fan"), parts[1:]):
            try:
                values[key] = float(raw)
            except ValueError:
                pass
        if "t:gpu" in values and "t:gpu" not in self.meta:
            self.register("t:gpu", "nvidia", "gpu", "temp", values["t:gpu"])
            if "title" not in self.overrides.get("t:gpu", {}):
                self.meta["t:gpu"]["title"] = f"GPU {self.info['gpu_name']}"

    def amd_gpu(self, values):
        """Load card for an AMD GPU (the one with the most VRAM) when there is no NVIDIA GPU."""
        best = None
        for dev in glob.glob("/sys/class/drm/card[0-9]*/device"):
            busy = read_text(f"{dev}/gpu_busy_percent")
            total = read_text(f"{dev}/mem_info_vram_total")
            if busy and busy.isdigit() and total and total.isdigit() and (not best or int(total) > best[2]):
                best = (dev, int(busy), int(total))
        if not best:
            return
        dev, busy, total = best
        values["gpu_util"] = float(busy)
        values["gpu_mem_total"] = total / 2**20
        used = read_text(f"{dev}/mem_info_vram_used")
        if used and used.isdigit():
            values["gpu_mem"] = int(used) / 2**20
        for power in glob.glob(f"{dev}/hwmon/hwmon*/power1_average") + glob.glob(f"{dev}/hwmon/hwmon*/power1_input"):
            raw = read_text(power)
            if raw and raw.isdigit():
                values["gpu_w"] = int(raw) / 1e6
                break
        self.info["gpu_name"] = self.info["gpu_name"] or read_text(f"{dev}/product_name") or "AMD GPU"

    def cpu(self, values):
        nums = [int(x) for x in (read_text("/proc/stat") or "").splitlines()[0].split()[1:]]
        idle, total = nums[3] + nums[4], sum(nums[:8])
        if self.prev_cpu and total > self.prev_cpu[1]:
            values["cpu"] = round(100 * (1 - (idle - self.prev_cpu[0]) / (total - self.prev_cpu[1])), 1)
        self.prev_cpu = (idle, total)
        freqs = [read_text(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        freqs = [int(f) for f in freqs if f and f.isdigit()]
        if freqs:
            values["cpu_mhz"] = round(sum(freqs) / len(freqs) / 1000)
        values["load1"] = float((read_text("/proc/loadavg") or "0").split()[0])

    @staticmethod
    def memory(values):
        mem = {}
        for line in (read_text("/proc/meminfo") or "").splitlines():
            name, rest = line.split(":", 1)
            mem[name] = int(rest.split()[0]) * 1024
        values["mem_total"] = mem["MemTotal"]
        values["mem_used"] = mem["MemTotal"] - mem["MemAvailable"]
        values["mem_cache"] = mem.get("Cached", 0) + mem.get("Buffers", 0)
        values["swap_total"] = mem.get("SwapTotal", 0)
        values["swap_used"] = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)

    def disk(self, values, now):
        st = os.statvfs(self.disk_mount)
        values["disk_total"] = st.f_blocks * st.f_frsize
        values["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
        for line in (read_text("/proc/diskstats") or "").splitlines():
            f = line.split()
            if f[2] == self.disk_dev:
                rd, wr = int(f[5]) * 512, int(f[9]) * 512
                if self.prev_disk and now > self.prev_disk[0]:
                    dt = now - self.prev_disk[0]
                    values["disk_r"] = round((rd - self.prev_disk[1]) / dt)
                    values["disk_w"] = round((wr - self.prev_disk[2]) / dt)
                    values["disk_io"] = values["disk_r"] + values["disk_w"]
                self.prev_disk = (now, rd, wr)
                break

    def sample(self):
        now = time.time()
        values = {}
        for step in (self.hwmon, self.nvidia, self.cpu, self.memory):
            try:
                step(values)
            except Exception:
                log.exception("collector step %s failed", step.__name__)
        try:
            if "gpu_util" not in values:
                self.amd_gpu(values)
            self.disk(values, now)
        except Exception:
            log.exception("collector step failed")
        return int(now), values


# Demo mode: a made-up machine with a daily pattern, a CPU-heavy afternoon and a
# GPU-heavy evening (UTC). Used for screenshots and to try the UI without hardware.
# (driver, label, kind, base, daily swing, extra under load, load source)
DEMO_SENSORS = [
    ("k10temp", "Tctl", "temp", 44, 4, 38, "cpu"),
    ("k10temp", "Tccd1", "temp", 39, 4, 42, "cpu"),
    ("nct6798", "CPUTIN", "temp", 36, 3, 16, "cpu"),
    ("nct6798", "SYSTIN", "temp", 33, 2, 4, "both"),
    ("nct6798", "AUXTIN0", "temp", 63, 2, 5, "both"),
    ("amdgpu", "edge", "temp", 41, 2, 6, "cpu"),
    ("nvme", "Composite", "temp", 38, 3, 9, "cpu"),
    ("nvme", "Sensor 2", "temp", 51, 2, 7, "cpu"),
    ("spd5118", "temp1", "temp", 37, 2, 6, "cpu"),
    ("spd5118", "temp1", "temp", 39, 2, 6, "cpu"),
    ("iwlwifi_1", "temp1", "temp", 46, 2, 1, "both"),
    ("r8169_0_c00:00", "temp1", "temp", 44, 1, 1, "both"),
    ("nct6798", "fan1", "fan", 720, 40, 900, "cpu"),
    ("nct6798", "fan2", "fan", 880, 40, 500, "gpu"),
    ("nct6798", "fan3", "fan", 0, 0, 0, "cpu"),
]
DEMO_OVERRIDES = {
    "t:nct6798/CPUTIN": {"title": "CPU VRM", "warn": 95, "focus": True},
    "t:nct6798/AUXTIN0": {"title": "Chipset", "warn": 90},
    "t:nct6798/SYSTIN": {"title": "System", "warn": 60},
    "f:nct6798/fan1": {"title": "CPU fan"},
    "f:nct6798/fan2": {"title": "Case fan"},
}


class DemoCollector(Collector):
    def __init__(self, server, overrides):
        super().__init__({**server, "title": server["title"] or "demo"}, {**DEMO_OVERRIDES, **overrides})
        self.info.update(host="demo", cpu_model="AMD Ryzen 7 7700 8-Core Processor", cpu_threads=16,
                         gpu_name="RTX 4070", disk="nvme0n1")
        values = self.values_at(time.time())
        for key, (driver, label, kind, *_rest) in self._keys():
            self.register(key, driver, label, kind, values[key])
        self.register("t:gpu", "nvidia", "gpu", "temp", values["t:gpu"])
        self.meta["t:gpu"]["title"] = "GPU RTX 4070"

    @staticmethod
    def _keys():
        seen = {}
        for sensor in DEMO_SENSORS:
            key = f"{sensor[2][0]}:{sensor[0]}/{sensor[1]}"
            seen[key] = seen.get(key, 0) + 1
            yield (key + (f"#{seen[key]}" if seen[key] > 1 else "")), sensor

    @staticmethod
    def load_at(t):
        hour = t % 86400 / 3600
        spike = random.Random(int(t) // 600).random() < 0.012
        cpu = 1.0 if 13 <= hour < 15.5 else 0.35 if spike else 0.0
        gpu = 1.0 if 19.5 <= hour < 22 else 0.0
        return cpu, gpu, math.sin((hour - 9) / 24 * 2 * math.pi)

    def values_at(self, t):
        cpu, gpu, day = self.load_at(t)
        rnd = random.Random(int(t))
        busy = {"cpu": cpu, "gpu": gpu, "both": max(cpu, gpu)}
        values = {}
        for key, (_, _, kind, base, swing, extra, src) in self._keys():
            v = base + day * swing / 2 + busy[src] * extra + rnd.uniform(-1, 1) * swing / 4 if base else 0
            values[key] = round(v, 1) if kind == "temp" else int(v)
        gb = 2**30
        values.update({
            "t:gpu": round(33 + day + gpu * 35 + cpu * 3 + rnd.uniform(-0.5, 0.5), 1),
            "gpu_util": round(min(100, gpu * 92 + rnd.uniform(0, 4)), 1),
            "gpu_mem": round(380 + gpu * 7800), "gpu_mem_total": 12282.0,
            "gpu_w": round(12 + gpu * 165 + rnd.uniform(0, 3), 1), "gpu_fan": round(gpu * 55),
            "cpu": round(min(100, 2 + cpu * 88 + gpu * 18 + rnd.uniform(0, 3)), 1),
            "cpu_mhz": round(3800 + cpu * 1300 + rnd.uniform(0, 300)), "load1": round(0.2 + cpu * 15 + gpu * 3, 2),
            "mem_total": 32 * gb, "mem_used": round((6.5 + cpu * 9 + gpu * 5 + rnd.uniform(0, 0.4)) * gb),
            "mem_cache": 14 * gb, "swap_total": 8 * gb, "swap_used": 0,
            "disk_total": 2 * 10**12, "disk_used": 612 * 10**9,
            "disk_r": round(cpu * 180e6 * rnd.random() + 2e5), "disk_w": round(cpu * 90e6 * rnd.random() + 4e5),
        })
        values["disk_io"] = values["disk_r"] + values["disk_w"]
        return values

    def sample(self):
        now = time.time()
        return int(now), self.values_at(now)


class Store:
    def __init__(self, path, step):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.step = step
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        self.cache = {}
        self.base_cache = None

    def add(self, ts, values):
        self.add_many([(ts, values)])

    def add_many(self, rows):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO samples VALUES (?, ?)",
                                ((ts, json.dumps(values)) for ts, values in rows))
            self.db.commit()

    def prune(self, before):
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (before,))
            self.db.commit()

    def history(self, seconds, now=None):
        """Per-bucket averages (≤ MAX_POINTS per series) + min/max over raw samples."""
        now = int(now or time.time())
        bucket = self.step * math.ceil(seconds / self.step / MAX_POINTS)
        cached = self.cache.get(seconds)
        if cached and now - cached[0] < max(self.step, bucket // 4):
            return cached[1]
        start = (now - seconds) // bucket * bucket
        n = (now - start) // bucket + 1
        sums, counts, lo, hi = {}, {}, {}, {}
        with self.lock:  # iterate the cursor: a week of samples is never loaded at once
            for ts, data in self.db.execute("SELECT ts, data FROM samples WHERE ts >= ? ORDER BY ts", (start,)):
                i = min((ts - start) // bucket, n - 1)
                for key, v in json.loads(data).items():
                    s = sums.setdefault(key, [0.0] * n)
                    c = counts.setdefault(key, [0] * n)
                    s[i] += v
                    c[i] += 1
                    lo[key] = min(lo.get(key, v), v)
                    hi[key] = max(hi.get(key, v), v)
        series = {
            key: [round(s / c, 2) if c else None for s, c in zip(sums[key], counts[key])]
            for key in sums
        }
        result = {
            "start": start, "step": bucket, "n": n,
            "series": series, "min": lo, "max": hi, "base": self.baseline(now),
        }
        self.cache[seconds] = (now, result)
        return result

    def baseline(self, now=None):
        """Baseline from all stored data: median and usual range (p10–p90) per metric."""
        now = int(now or time.time())
        if self.base_cache and now - self.base_cache[0] < BASELINE_TTL:
            return self.base_cache[1]
        vals = {}
        with self.lock:
            total, first = self.db.execute("SELECT COUNT(*), MIN(ts) FROM samples").fetchone()
            stride = max(1, total // BASELINE_SAMPLES)
            for i, (data,) in enumerate(self.db.execute("SELECT data FROM samples ORDER BY ts")):
                if i % stride == 0:
                    for key, v in json.loads(data).items():
                        vals.setdefault(key, []).append(v)
        keys = {}
        for key, xs in vals.items():
            if len(xs) < 30:
                continue
            xs.sort()
            pick = lambda p: round(xs[min(len(xs) - 1, int(p * len(xs)))], 2)
            keys[key] = {"p10": pick(0.1), "p50": pick(0.5), "p90": pick(0.9)}
        result = {"span_h": round((now - first) / 3600, 1) if first else 0, "keys": keys}
        self.base_cache = (now, result)
        return result


def collector_loop(collector, store, state, step, retention_h):
    last_prune = 0
    while True:
        time.sleep(step - time.time() % step)
        try:
            ts, values = collector.sample()
            if "cpu" in values:  # the first sample has no rates yet
                store.add(ts, values)
            state["now"] = {"ts": ts, "values": values}
            if ts - last_prune > 3600:
                store.prune(ts - retention_h * 3600)
                last_prune = ts
        except Exception:
            log.exception("collector loop failed")


def make_handler(collector, store, ui_config, state, step, stream=None, net=None):
    index = STATIC / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"hwdash/{__version__}"

        def log_message(self, *args):
            pass

        def send_body(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send_body(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 65536:
                raise ValueError("body is empty or too large")
            return json.loads(self.rfile.read(length))  # JSONDecodeError is a ValueError

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self.send_body(200, index.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/now":
                uptime = float((read_text("/proc/uptime") or "0").split()[0])
                self.send_json({**state.get("now", {}), "meta": collector.meta,
                                "info": {**collector.info, "uptime": int(uptime), "step": step,
                                         "stream": stream is not None, "topology": net is not None}})
            elif url.path == "/api/stream" and stream:
                self.send_json(stream.snapshot(self.headers.get("X-Hwdash-Token")))
            elif url.path == "/api/topology" and net:
                self.send_json(net.snapshot())
            elif url.path == "/api/config":
                self.send_json(ui_config.load())
            elif url.path == "/api/history":
                rng = parse_qs(url.query).get("range", ["24h"])[0]
                if rng not in RANGES:
                    self.send_body(400, b"bad range", "text/plain")
                    return
                self.send_json(store.history(RANGES[rng]))
            else:
                self.send_body(404, b"not found", "text/plain")

        def do_PUT(self):
            path = urlparse(self.path).path
            if path == "/api/stream/policy" and stream:
                self.stream_change(lambda body: stream.set_policy(str(body["uuid"]), body["policy"]))
                return
            if path != "/api/config":
                self.send_body(404, b"not found", "text/plain")
                return
            try:
                cfg = clean_config(self.read_json())
            except ValueError as e:
                self.send_body(400, str(e).encode(), "text/plain")
                return
            ui_config.save(cfg)
            self.send_json(cfg)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/stream/login" and stream:
                try:
                    pin = str(self.read_json().get("pin", ""))
                except (ValueError, AttributeError):
                    self.send_body(400, b"bad request", "text/plain")
                    return
                if not stream.pin_path.exists():
                    self.send_json({"error": "no_pin"}, 403)
                    return
                token, wait = stream.login(pin)
                self.send_json({"token": token, "ttl": sunshine.TOKEN_TTL} if token else {"error": "bad_pin", "wait": wait},
                               200 if token else 403)
            elif path == "/api/stream/action" and stream:
                self.stream_change(lambda body: stream.apply_action(str(body["uuid"]), body["action"], body.get("minutes")))
            else:
                self.send_body(404, b"not found", "text/plain")

        def stream_change(self, change):
            """Changes to Moonlight access need a token from /api/stream/login."""
            if not stream.authorized(self.headers.get("X-Hwdash-Token")):
                self.send_json({"error": "auth"}, 401)
                return
            try:
                change(self.read_json())
            except (ValueError, KeyError, TypeError) as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_json(stream.snapshot(self.headers.get("X-Hwdash-Token")))

    return Handler


def seed_demo(collector, store, hours=26, every=60):
    now = int(time.time())
    store.add_many((ts, collector.values_at(ts)) for ts in range(now - hours * 3600, now, every))


def main():
    parser = argparse.ArgumentParser(description="Tiny dashboard for hardware temperatures and load.")
    parser.add_argument("command", nargs="?", choices=["serve", "set-pin", "gate-start", "gate-end"], default="serve",
                        help="serve (default); set-pin: PIN for Moonlight access changes; "
                             "gate-start / gate-end: Sunshine global_prep_cmd do / undo")
    parser.add_argument("-c", "--config", type=Path,
                        help=f"config file (default: {DEFAULT_CONFIG} if it exists)")
    parser.add_argument("--bind", help="address to listen on (overrides config)")
    parser.add_argument("--port", type=int, help="port to listen on (overrides config)")
    parser.add_argument("--demo", action="store_true",
                        help="serve made-up data from a temporary database; nothing is saved")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # The demo ignores your own config unless one is named: screenshots must not show the real machine
    config = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() and not args.demo else None)
    settings = load_settings(config)
    server = settings["server"]
    for opt in ("bind", "port"):
        if getattr(args, opt) is not None:
            server[opt] = getattr(args, opt)
    step = int(server["step"])
    stream_cfg = settings["sunshine"]

    if args.command in ("gate-start", "gate-end"):
        sys.exit(sunshine.run_gate(stream_cfg, Path(server["db"]).expanduser(), args.command[5:]))
    if args.command == "set-pin":
        pin = getpass.getpass("New PIN (4+ characters): ")
        if len(pin) < 4 or pin != getpass.getpass("Repeat: "):
            raise SystemExit("PINs are shorter than 4 characters or do not match")
        sunshine.write_private(Path(stream_cfg["pin"]).expanduser(), sunshine.hash_pin(pin) + "\n")
        print(f"PIN saved to {stream_cfg['pin']}")
        return

    stream = net = None
    net_cfg = settings["topology"]
    if args.demo:
        tmp = Path(tempfile.mkdtemp(prefix="hwdash-demo-"))
        collector = DemoCollector(server, settings["sensors"])
        store = Store(tmp / "metrics.db", step)
        ui_config = UiConfig(tmp / "ui.json")
        seed_demo(collector, store)
        stream = sunshine.make_demo(tmp, stream_cfg)
        net = topology.make_demo(net_cfg)
    else:
        collector = Collector(server, settings["sensors"])
        store = Store(Path(server["db"]).expanduser(), step)
        ui_config = UiConfig(Path(server["ui_config"]).expanduser())
        collector.sample()  # baseline counters for CPU / disk rates
        time.sleep(1)
        if stream_cfg["enabled"]:
            stream = sunshine.StreamManager(stream_cfg, Path(server["db"]).expanduser())
        if net_cfg["enabled"]:
            try:
                net = topology.TopologyMonitor(net_cfg)
            except ValueError as e:
                raise SystemExit(f"{config}: [topology]: {e}")
            net.start()
    ts, values = collector.sample()
    state = {"now": {"ts": ts, "values": values}}
    threading.Thread(target=collector_loop, args=(collector, store, state, step, server["retention_hours"]),
                     daemon=True).start()
    if stream:
        stream.start()
    httpd = ThreadingHTTPServer((server["bind"], server["port"]),
                                make_handler(collector, store, ui_config, state, step, stream, net))
    log.info("hwdash %s on http://%s:%d%s, config %s", __version__, server["bind"], server["port"],
             " (demo)" if args.demo else "", config or "none")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
