"""The watcher loop: sample -> controller -> signals. Writes a state file other tools use as a back-pressure signal."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from .config import Config
from .controller import Controller, GREEN
from .probes import read_sample, scoped_procs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_state(cfg: Config, mode: str) -> None:
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_path.with_suffix(".tmp")
    tmp.write_text(f"{mode} {_now_iso()}\n")
    os.replace(tmp, cfg.state_path)  # atomic: readers never see a partial file


def log_event(cfg: Config, **kw) -> None:
    cfg.events_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_path, "a") as f:
        f.write(json.dumps({"t": _now_iso(), **kw}) + "\n")


def read_state(cfg: Config) -> tuple[str, float]:
    """(mode, age_seconds). mode is 'DEAD' when the file is missing or stale (watcher not running)."""
    try:
        text = cfg.state_path.read_text().split()
        age = time.time() - cfg.state_path.stat().st_mtime
    except OSError:
        return "DEAD", float("inf")
    if age > cfg.state_max_age_s:
        return "DEAD", age
    return text[0], age


def signal_pid(pid: int, sig: int, dry: bool) -> bool:
    if dry:
        return True
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def apply(cfg: Config, ctl: Controller, acts: list) -> None:
    for a in acts:
        if a.kind in ("FREEZE", "PAUSE"):
            ok = signal_pid(a.pid, signal.SIGSTOP, cfg.dry_run)
        elif a.kind in ("THAW", "RESUME"):
            ok = signal_pid(a.pid, signal.SIGCONT, cfg.dry_run)
        else:
            ok = True
        log_event(cfg, event=a.kind, pid=a.pid, reason=a.reason, ok=ok, dry=cfg.dry_run, mode=ctl.mode)


def resume_everything(cfg: Config, ctl: Controller) -> None:
    for pid in list(ctl.held):
        signal_pid(pid, signal.SIGCONT, cfg.dry_run)
        ctl.held.pop(pid, None)


def run_watch(cfg: Config, once: bool = False, max_steps: int | None = None) -> None:
    ctl = Controller(cfg)

    def _bye(*_):
        resume_everything(cfg, ctl)  # never leave anything frozen behind
        log_event(cfg, event="STOP")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    log_event(cfg, event="START", dry=cfg.dry_run)
    steps = 0
    while True:
        s, uuids = read_sample(cfg)
        procs = scoped_procs(cfg, uuids)
        ctl.forget_dead({p.pid for p in procs})
        acts = ctl.step(s, procs, time.time())
        apply(cfg, ctl, acts)
        write_state(cfg, ctl.mode)
        steps += 1
        if once or (max_steps and steps >= max_steps):
            return
        time.sleep(cfg.interval_s)
