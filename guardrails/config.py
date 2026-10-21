"""Configuration: every threshold is a FRACTION of detected capacity, so one profile works across hardware."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


@dataclass
class Config:
    # --- accelerator power (fraction of each device's own power limit) ---
    gpu_power_soft_frac: float = 0.70       # above this: pause the youngest job on that device
    gpu_power_resume_frac: float = 0.48     # below this: resume one paused job
    gpu_power_red_frac: float = 1.02        # per-card RED only if a card exceeds its own limit; single jobs near the cap are normal (soft cap handles them)
    total_gpu_power_red_frac: float = 0.76  # summed draw / summed limits above this: RED
    calm_total_power_frac: float = 0.40     # summed draw must fall below this before thawing
    # --- temperatures (deg C) ---
    gpu_temp_soft_c: float = 80.0
    gpu_temp_resume_c: float = 72.0
    gpu_temp_red_c: float = 84.0
    cpu_temp_red_c: float = 95.0
    # --- memory (fraction of total RAM) ---
    ram_soft_frac: float = 0.50
    ram_resume_frac: float = 0.42
    ram_red_frac: float = 0.56
    ram_admission_frac: float = 0.50        # launcher refuses to start a job if used + job_ram would exceed this
    # --- CPU pressure ---
    load_red_per_cpu: float = 4.0           # load1 / cpus above this: RED (0 disables)
    # --- timing ---
    interval_s: float = 1.0
    soft_act_gap_s: float = 3.0
    red_cooldown_s: float = 120.0           # minimum frozen time; doubles on repeated RED, capped below
    red_cooldown_max_s: float = 600.0       # cap: a 16-minute freeze cost more than the alarm saved
    red_forgive_s: float = 600.0            # a calm GREEN this long resets the escalation counter
    calm_samples: int = 15                  # consecutive calm samples required before thawing
    thaw_spacing_s: float = 8.0             # staggered resume: one process per spacing (avoids inrush)
    # --- which processes the guard may pause ---
    scope_mode: str = "cwd"                 # cwd | user | all
    scope_root: str = "."
    scope_users: list = field(default_factory=list)
    include_cmd_regex: str = r"python|java|julia|Rscript|ffmpeg|torchrun|cargo|\bmake\b"
    exclude_cmd_regex: str = r"guardrails|sshd|claude"
    # --- launcher ---
    gpu_slots_per_device: int = 1
    cpu_slots: int = 4
    threads_per_job: int = 3
    state_dir: str = "~/.cache/guardrails"
    state_max_age_s: float = 60.0           # a state file older than this means the watcher is dead
    dry_run: bool = False

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "STATE"

    @property
    def events_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "events.jsonl"


def load_config(path: str | None = None, **overrides) -> Config:
    data: dict = {}
    if path:
        p = Path(path)
        if p.suffix == ".json":
            data = json.loads(p.read_text())
        else:
            if tomllib is None:
                raise RuntimeError("TOML configs need Python 3.11+; use a .json config instead")
            data = tomllib.loads(p.read_text())
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    data.update({k: v for k, v in overrides.items() if v is not None})
    cfg = Config(**data)
    validate(cfg)
    return cfg


def validate(c: Config) -> None:
    """Reject configs whose thresholds are not ordered resume < soft < red (they would thrash or never trigger)."""
    checks = [
        (c.gpu_power_resume_frac < c.gpu_power_soft_frac < c.gpu_power_red_frac, "gpu power: need resume < soft < red"),
        (c.gpu_temp_resume_c < c.gpu_temp_soft_c < c.gpu_temp_red_c, "gpu temp: need resume < soft < red"),
        (c.ram_resume_frac < c.ram_soft_frac < c.ram_red_frac <= 1.0, "ram: need resume < soft < red <= 1"),
        (0 < c.interval_s <= 30, "interval_s must be in (0, 30]"),
        (c.scope_mode in ("cwd", "user", "all"), "scope_mode must be cwd|user|all"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        raise ValueError("; ".join(bad))
