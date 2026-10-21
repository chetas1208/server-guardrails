"""The decision logic: a pure state machine. No I/O, no clocks: feed it samples, get actions back (so it is fully testable).

States
  GREEN  normal; soft caps may PAUSE/RESUME single jobs (duty-cycling keeps average power under the soft cap)
  RED    something is out of control: FREEZE every in-scope process (SIGSTOP, nothing is killed, no work is lost)
  THAW   conditions are calm again: resume held processes one at a time, `thaw_spacing_s` apart (no inrush spike)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config

GREEN, RED, THAW = "GREEN", "RED", "THAW"


@dataclass
class Device:
    id: int
    power_w: float
    limit_w: float
    temp_c: float = 0.0


@dataclass
class Sample:
    devices: list = field(default_factory=list)   # list[Device]
    ram_used_gib: float = 0.0
    ram_total_gib: float = 1.0
    load1: float = 0.0
    ncpu: int = 1
    cpu_temp_c: float | None = None


@dataclass
class Proc:
    pid: int
    start: float                 # larger = younger
    rss_gib: float = 0.0
    device: int | None = None    # accelerator the process is using, if any
    stopped: bool = False


@dataclass
class Action:
    kind: str                    # PAUSE | RESUME | FREEZE | THAW | MODE
    pid: int | None = None
    reason: str = ""


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = GREEN
        self.held: dict[int, dict] = {}       # pid -> {"reason": str, "start": float, "device": int|None}
        self.red_count = 0
        self._cool = 0.0
        self._freeze_t = 0.0
        self._calm_n = 0
        self._last_thaw = -1e9
        self._last_red = 0.0
        self._over = {}
        self._under = {}
        self._last_act: dict = {}

    # -- helpers -------------------------------------------------------------------------------------------------
    def red_reasons(self, s: Sample) -> list[str]:
        c, why = self.cfg, []
        for d in s.devices:
            if d.limit_w > 0 and d.power_w > c.gpu_power_red_frac * d.limit_w:
                why.append(f"gpu{d.id} power {d.power_w:.0f}W > {c.gpu_power_red_frac:.0%} of {d.limit_w:.0f}W")
            if d.temp_c >= c.gpu_temp_red_c:
                why.append(f"gpu{d.id} temp {d.temp_c:.0f}C >= {c.gpu_temp_red_c:.0f}C")
        cap = sum(d.limit_w for d in s.devices)
        tot = sum(d.power_w for d in s.devices)
        if cap > 0 and len(s.devices) > 1 and tot > c.total_gpu_power_red_frac * cap:  # one device: the per-device line already covers it
            why.append(f"total gpu power {tot:.0f}W > {c.total_gpu_power_red_frac:.0%} of {cap:.0f}W")
        if s.ram_total_gib > 0 and s.ram_used_gib / s.ram_total_gib > c.ram_red_frac:
            why.append(f"ram {s.ram_used_gib:.1f}/{s.ram_total_gib:.0f} GiB > {c.ram_red_frac:.0%}")
        if s.cpu_temp_c is not None and s.cpu_temp_c >= c.cpu_temp_red_c:
            why.append(f"cpu temp {s.cpu_temp_c:.0f}C >= {c.cpu_temp_red_c:.0f}C")
        if c.load_red_per_cpu > 0 and s.ncpu > 0 and s.load1 / s.ncpu > c.load_red_per_cpu:
            why.append(f"load {s.load1:.0f} > {c.load_red_per_cpu:g} per cpu")
        return why

    def is_calm(self, s: Sample) -> bool:
        c = self.cfg
        cap = sum(d.limit_w for d in s.devices)
        tot = sum(d.power_w for d in s.devices)
        return (all(d.temp_c < c.gpu_temp_resume_c for d in s.devices)
                and (cap <= 0 or tot < c.calm_total_power_frac * cap)
                and s.ram_used_gib / max(s.ram_total_gib, 1e-9) < c.ram_resume_frac)

    def _gap_ok(self, key, now) -> bool:
        return now - self._last_act.get(key, -1e9) >= self.cfg.soft_act_gap_s

    # -- main step -----------------------------------------------------------------------------------------------
    def step(self, s: Sample, procs: list, now: float) -> list:
        c, acts = self.cfg, []
        running = [p for p in procs if not p.stopped and p.pid not in self.held]
        reasons = self.red_reasons(s)

        if self.mode in (GREEN, THAW) and reasons:
            for p in running:
                acts.append(Action("FREEZE", p.pid, "; ".join(reasons)))
                self.held[p.pid] = {"reason": "red", "start": p.start, "device": p.device}
            for pid, h in self.held.items():
                h["reason"] = "red"
            self.mode, self.red_count = RED, self.red_count + 1
            self._cool = min(c.red_cooldown_s * 2 ** (self.red_count - 1), c.red_cooldown_max_s)
            self._freeze_t, self._last_red, self._calm_n = now, now, 0
            acts.append(Action("MODE", None, f"RED: {'; '.join(reasons)} (cooldown {self._cool:.0f}s)"))
            return acts

        if self.mode == RED:
            self._calm_n = self._calm_n + 1 if self.is_calm(s) else 0
            if now - self._freeze_t >= self._cool and self._calm_n >= c.calm_samples:
                self.mode, self._last_thaw = THAW, -1e9
                acts.append(Action("MODE", None, "THAW: calm for %d samples" % self._calm_n))
            return acts

        if self.mode == THAW:
            if now - self._last_thaw >= c.thaw_spacing_s:
                if self.held:
                    pid = min(self.held, key=lambda k: self.held[k]["start"])  # oldest first
                    self.held.pop(pid)
                    self._last_thaw = now
                    acts.append(Action("THAW", pid, "staggered resume"))
                if not self.held:
                    self.mode = GREEN
                    acts.append(Action("MODE", None, "GREEN"))
            return acts

        # GREEN: forgive old REDs after `red_forgive_s` of calm
        if self.red_count and now - self._last_red > c.red_forgive_s:
            self.red_count = 0

        # soft caps per accelerator (duty-cycling): pause the youngest job above the soft cap, resume below the resume mark
        for d in s.devices:
            over = (d.limit_w > 0 and d.power_w > c.gpu_power_soft_frac * d.limit_w) or d.temp_c >= c.gpu_temp_soft_c
            under = (d.limit_w <= 0 or d.power_w < c.gpu_power_resume_frac * d.limit_w) and d.temp_c < c.gpu_temp_resume_c
            self._over[d.id] = self._over.get(d.id, 0) + 1 if over else 0
            self._under[d.id] = self._under.get(d.id, 0) + 1 if under else 0
            if self._over[d.id] >= 1 and self._gap_ok(("dev", d.id), now):
                cand = [p for p in running if p.device == d.id]
                if cand:
                    v = max(cand, key=lambda p: p.start)
                    self.held[v.pid] = {"reason": f"dev{d.id}", "start": v.start, "device": d.id}
                    running = [p for p in running if p.pid != v.pid]
                    self._last_act[("dev", d.id)] = now
                    acts.append(Action("PAUSE", v.pid, f"gpu{d.id} {d.power_w:.0f}W/{d.temp_c:.0f}C over soft cap"))
            elif self._under[d.id] >= 2 and self._gap_ok(("dev", d.id), now):
                mine = [pid for pid, h in self.held.items() if h["reason"] == f"dev{d.id}"]
                if mine:
                    pid = min(mine, key=lambda k: self.held[k]["start"])
                    self.held.pop(pid)
                    self._last_act[("dev", d.id)] = now
                    acts.append(Action("RESUME", pid, f"gpu{d.id} below resume mark"))

        # soft RAM cap
        frac = s.ram_used_gib / max(s.ram_total_gib, 1e-9)
        if frac > c.ram_soft_frac and self._gap_ok("ram", now):
            cand = [p for p in running if p.rss_gib > 0.5]
            if cand:
                v = max(cand, key=lambda p: p.start)
                self.held[v.pid] = {"reason": "ram", "start": v.start, "device": v.device}
                self._last_act["ram"] = now
                acts.append(Action("PAUSE", v.pid, f"ram {frac:.0%} over soft cap"))
        elif frac < c.ram_resume_frac and self._gap_ok("ram", now):
            mine = [pid for pid, h in self.held.items() if h["reason"] == "ram"]
            if mine:
                pid = min(mine, key=lambda k: self.held[k]["start"])
                self.held.pop(pid)
                self._last_act["ram"] = now
                acts.append(Action("RESUME", pid, "ram below resume mark"))
        return acts

    def forget_dead(self, alive: set) -> None:
        for pid in [p for p in self.held if p not in alive]:
            self.held.pop(pid)
