"""Hardware/OS probes. Standard library only. Every probe degrades gracefully: missing tool => feature unavailable, never a crash."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .controller import Device, Proc, Sample


def _run(cmd: list, timeout: float = 10.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---- accelerators -----------------------------------------------------------------------------------------------
def nvidia_devices() -> tuple[list, dict]:
    """Returns (devices, uuid->index). Empty when no NVIDIA driver."""
    if not shutil.which("nvidia-smi"):
        return [], {}
    out = _run(["nvidia-smi", "--query-gpu=index,uuid,temperature.gpu,power.draw,power.limit", "--format=csv,noheader,nounits"])
    devs, uuids = [], {}
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        try:
            devs.append(Device(int(p[0]), float(p[3]), float(p[4]), float(p[2])))
            uuids[p[1]] = int(p[0])
        except (ValueError, IndexError):
            continue  # "[N/A]" fields on some virtualized GPUs
    return devs, uuids


def nvidia_pid_device(uuids: dict) -> dict:
    out = _run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"])
    m = {}
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 2 and p[1].isdigit():
            m[int(p[1])] = uuids.get(p[0])
    return m


def amd_devices() -> list:
    """Best effort via rocm-smi (experimental)."""
    if not shutil.which("rocm-smi"):
        return []
    out = _run(["rocm-smi", "--showpower", "--showtemp", "--showmaxpower", "--csv"])
    devs = []
    for line in out.strip().splitlines()[1:]:
        p = [x.strip() for x in line.split(",")]
        nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", ",".join(p[1:]))]
        if len(nums) >= 3 and p[0].startswith("card"):
            devs.append(Device(int(re.sub(r"\D", "", p[0]) or 0), nums[1], nums[2], nums[0]))
    return devs


# ---- host -------------------------------------------------------------------------------------------------------
def meminfo() -> tuple[float, float]:
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":")
        d[k] = int(v.split()[0])
    total = d["MemTotal"] / 1048576
    return total - d.get("MemAvailable", d["MemFree"]) / 1048576, total


def cpu_temp_c() -> float | None:
    best = None
    for z in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            t = int(z.read_text()) / 1000
            best = t if best is None else max(best, t)
        except (OSError, ValueError):
            continue
    return best


def read_sample(cfg: Config) -> tuple[Sample, dict]:
    devs, uuids = nvidia_devices()
    if not devs:
        devs = amd_devices()
    used, total = meminfo()
    load1 = float(Path("/proc/loadavg").read_text().split()[0])
    return Sample(devs, used, total, load1, os.cpu_count() or 1, cpu_temp_c()), uuids


# ---- processes in scope -----------------------------------------------------------------------------------------
def scoped_procs(cfg: Config, uuids: dict) -> list:
    """Processes the guard is allowed to pause: matches include regex, not exclude regex, and the scope (cwd under root / user / all)."""
    inc, exc = re.compile(cfg.include_cmd_regex), re.compile(cfg.exclude_cmd_regex)
    root = Path(cfg.scope_root).expanduser().resolve()
    pid_dev = nvidia_pid_device(uuids) if uuids else {}
    me, out = os.getpid(), []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:300]
            if not cmd or not inc.search(cmd) or exc.search(cmd):
                continue
            if cfg.scope_mode == "cwd":
                cwd = Path(os.readlink(d / "cwd")).resolve()
                if root != cwd and root not in cwd.parents:
                    continue
            elif cfg.scope_mode == "user":
                import pwd
                if pwd.getpwuid(os.stat(d).st_uid).pw_name not in cfg.scope_users:
                    continue
            status = (d / "status").read_text()
            rss = int(re.search(r"VmRSS:\s+(\d+)", status).group(1)) / 1048576 if "VmRSS" in status else 0.0
            stopped = re.search(r"State:\s+([A-Z])", status).group(1) in ("T", "Z")
            out.append(Proc(int(d.name), os.stat(d).st_ctime, rss, pid_dev.get(int(d.name)), stopped))
        except (OSError, AttributeError, KeyError):
            continue
    return out


def doctor() -> dict:
    """What can this machine measure? Used by `guardrails doctor`."""
    devs, _ = nvidia_devices()
    amd = [] if devs else amd_devices()
    used, total = meminfo()
    rapl = list(Path("/sys/class/powercap").glob("intel-rapl:*/energy_uj"))
    return {
        "nvidia_gpus": [{"id": d.id, "limit_w": d.limit_w, "power_w": d.power_w, "temp_c": d.temp_c} for d in devs],
        "amd_gpus": len(amd), "cpu_cores": os.cpu_count(), "ram_total_gib": round(total, 1), "ram_used_gib": round(used, 1),
        "cpu_temp_c": cpu_temp_c(), "rapl_readable": any(os.access(p, os.R_OK) for p in rapl),
        "can_set_hw_power_limit": os.geteuid() == 0, "loadavg": os.getloadavg(),
    }
