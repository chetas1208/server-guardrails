"""Admission control for heavy jobs: slots + RAM headroom + watcher back-pressure. Queues instead of stacking; never kills anything."""
from __future__ import annotations

import fcntl
import os
import shutil
import time
from pathlib import Path

from .config import Config
from .probes import meminfo
from .watcher import read_state

THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS")


def slot_names(klass: str, cfg: Config) -> list:
    if klass == "cpu":
        return [f"cpu.{i}" for i in range(cfg.cpu_slots)]
    if klass.startswith("gpu"):
        return [f"{klass}.{i}" for i in range(cfg.gpu_slots_per_device)]
    raise ValueError(f"unknown class {klass!r} (use cpu or gpu<N>)")


def try_acquire(klass: str, cfg: Config):
    """Return an open locked file descriptor for a free slot, or None. The lock lives as long as the process (survives exec)."""
    d = Path(cfg.state_dir).expanduser() / "slots"
    d.mkdir(parents=True, exist_ok=True)
    for name in slot_names(klass, cfg):
        fd = os.open(d / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(fd, True)
            return fd
        except BlockingIOError:
            os.close(fd)
    return None


def ram_ok(cfg: Config, job_ram_gib: float) -> bool:
    used, total = meminfo()
    return used + job_ram_gib <= cfg.ram_admission_frac * total


def wait_until_admitted(klass: str, cfg: Config, job_ram_gib: float, poll_s: float = 5.0, timeout_s: float | None = None):
    t0 = time.time()
    fd = None
    while True:
        if fd is None:
            fd = try_acquire(klass, cfg)
        if fd is not None:
            mode, _ = read_state(cfg)
            if mode == "GREEN" and ram_ok(cfg, job_ram_gib):
                return fd
        if timeout_s is not None and time.time() - t0 > timeout_s:
            if fd is not None:
                os.close(fd)
            raise TimeoutError("not admitted in time")
        time.sleep(poll_s)


def build_env(klass: str, cfg: Config, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    for v in THREAD_VARS:
        env[v] = str(cfg.threads_per_job)
    env["TOKENIZERS_PARALLELISM"] = "false"
    if klass.startswith("gpu"):
        env["CUDA_VISIBLE_DEVICES"] = klass[3:]
        env["HIP_VISIBLE_DEVICES"] = klass[3:]
    else:
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def run(klass: str, cfg: Config, cmd: list, job_ram_gib: float = 8.0) -> None:
    """Block until admitted, then exec the command (replaces this process; the slot lock stays held)."""
    wait_until_admitted(klass, cfg, job_ram_gib)
    env = build_env(klass, cfg)
    os.nice(10)
    prefix = ["ionice", "-c3"] if shutil.which("ionice") else []
    os.execvpe((prefix + cmd)[0], prefix + cmd, env)
