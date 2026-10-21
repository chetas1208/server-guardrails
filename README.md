# server-guardrails

**Keep a shared GPU/CPU server alive while jobs and agents hammer it.**
Soft power and RAM caps, a red-alert freeze of every job, and a staggered thaw. It *pauses* processes; it never kills them. Standard library only. Works on a GPU workstation, a multi-GPU node, a CPU-only box, a laptop, a cloud VM or a CI runner.

```text
GREEN ──soft cap──▶ pause youngest job on that device ──calm──▶ resume
  │
  └─red alert──▶ RED: freeze EVERYTHING (SIGSTOP) ──cooldown + calm──▶ THAW: resume one process every 8 s ──▶ GREEN
```

## Why this exists

A 2-GPU workstation running ~20 parallel agents hit a 786 W summed GPU draw, 44 GPU processes and a CPU load of ~110 on 20 cores, then powered off with no shutdown record. Logging alone did not help: nothing acted on the numbers. This tool acts on them. Read the full story in [docs/CASE_STUDY.md](docs/CASE_STUDY.md).

## What you get

| Piece | What it does |
|---|---|
| **Watcher** (`guardrails watch`) | Samples GPUs, RAM, load and temperature every second and runs a tested state machine. |
| **Soft caps** | Above 70% of a card's power limit (or 80 C, or 50% RAM) the *youngest* job on that device is paused; below the resume mark it continues. Duty-cycling keeps the average under the cap without root. |
| **Red alert** | Any card pegged above 99% of its limit, summed power above 76% of summed limits, 84 C, RAM above 56% or load above 4 per core freezes every in-scope process. |
| **Staggered thaw** | After a cooldown and a calm period, processes resume oldest first, one every 8 s, so restarting never recreates the spike. Repeated alerts double the cooldown. |
| **Launcher** (`guardrails run`) | Admission control: one job per GPU, N CPU jobs, RAM headroom check, pinned thread counts, lower priority, and it waits while the watcher is not GREEN. |
| **State file** | `GREEN/RED/THAW <time>`; any scheduler can read it as back-pressure. A stale file means the watcher is dead. |
| **Claude Code skill** | [`skill/server-guardrails/SKILL.md`](skill/server-guardrails/SKILL.md) teaches an agent to route every heavy job through the launcher and to treat pauses as normal. |

## Quick start

```bash
git clone https://github.com/chetas1208/server-guardrails && cd server-guardrails
pip install -e .                      # or just run ./bin/guardrails (no dependencies)

guardrails doctor                     # what can this machine measure? can it set a hardware power limit?
cp configs/profiles/gpu-workstation.toml my.toml
guardrails -c my.toml --dry-run watch # log decisions, send no signals: check them first
guardrails -c my.toml watch &         # enforce
guardrails -c my.toml status          # GREEN / RED / THAW / DEAD

guardrails -c my.toml run gpu0 --ram 12 -- python train.py     # queue for GPU 0
guardrails -c my.toml run cpu  --ram 4  -- python prep.py      # queue for a CPU slot
```

Replay a recorded trace without touching anything: `guardrails -c my.toml simulate samples.json`.

## Profiles

| Profile | For | Highlights |
|---|---|---|
| [`gpu-workstation`](configs/profiles/gpu-workstation.toml) | 1-4 GPUs, shared lab box | 70% soft cap, RED on summed power / temperature / RAM, 1 job per GPU, 4 CPU jobs |
| [`multi-gpu-server`](configs/profiles/multi-gpu-server.toml) | 8-GPU training node | 2 jobs per GPU, 80% total-power line, 16 CPU slots |
| [`cpu-only`](configs/profiles/cpu-only.toml) | build/compute server without accelerators | RAM, load and CPU temperature only |
| [`laptop`](configs/profiles/laptop.toml) | thermally limited machine | early temperature reaction, short cooldown |
| [`shared-cloud-vm`](configs/profiles/shared-cloud-vm.toml) | VM on a host you do not control | conservative caps, long cooldown |
| [`ci-runner`](configs/profiles/ci-runner.toml) | many short build jobs | strict RAM, many slots, fast recovery |

Every threshold is a **fraction of detected capacity** (each card's own power limit, total RAM), so a profile survives a hardware change. Unordered thresholds (resume < soft < red) and unknown keys are rejected at load time.

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `gpu_power_soft_frac` / `_resume_frac` / `_red_frac` | 0.70 / 0.48 / 1.02 | per-card power lines, as a fraction of its limit (the red line only fires if a card exceeds its own limit) |
| `total_gpu_power_red_frac` | 0.76 | summed draw / summed limits that triggers RED |
| `total_gpu_power_resume_frac`, `soft_resume_spacing_s` | 0.68, 8 | a soft RESUME is allowed only if the projected summed draw (job back at its pre-pause power) stays below this, and at most one resume per spacing across devices |
| `calm_total_power_frac` | 0.40 | summed draw must be below this before thawing (a frozen job still holds a card above idle) |
| `gpu_temp_soft_c` / `_resume_c` / `_red_c` | 80 / 72 / 84 | temperatures in C |
| `ram_soft_frac` / `_resume_frac` / `_red_frac` | 0.50 / 0.42 / 0.56 | fractions of total RAM |
| `ram_admission_frac` | 0.50 | launcher refuses to start a job if it would push RAM past this |
| `load_red_per_cpu` | 4.0 | load1 / cores that triggers RED (0 disables) |
| `interval_s`, `soft_act_gap_s` | 1.0, 3.0 | sampling period and minimum gap between soft actions |
| `red_cooldown_s`, `red_cooldown_max_s`, `red_forgive_s` | 120, 600, 600 | minimum frozen time; doubles on repeated RED up to the cap; calm GREEN this long resets the counter |
| `calm_samples`, `thaw_spacing_s` | 15, 8 | calm samples needed; seconds between resumed processes |
| `scope_mode`, `scope_root`, `scope_users` | cwd, ".", [] | which processes may be paused: cwd under a root, owned by users, or all |
| `include_cmd_regex`, `exclude_cmd_regex` | python/java/ffmpeg/..., guardrails/sshd/claude | command-line filters; interactive shells never match |
| `gpu_slots_per_device`, `cpu_slots`, `threads_per_job` | 1, 4, 3 | launcher slots and thread pinning |

## Architecture

Probes (read-only) feed a **pure** controller (no I/O, no clock, fully unit-tested); the watcher turns its actions into `SIGSTOP`/`SIGCONT`; a state file is the only contract with the rest of the world. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the diagram, the state machine, the duty-cycle math and the fail-safe rules.

## Field notes

What went wrong in production and how the defaults changed: [docs/LESSONS.md](docs/LESSONS.md).

## Install the skill for Claude Code

```bash
./skill/install.sh          # copies SKILL.md to ~/.claude/skills/server-guardrails
```

Then ask your agent to "run these jobs safely" or "save the server": it will check `doctor`, pick a profile, start the watcher and route work through `guardrails run`.

## Run it as a service

[`examples/guardrails.service`](examples/guardrails.service) (systemd, `Restart=always`) or [`examples/supervisor.sh`](examples/supervisor.sh). Stopping the watcher thaws everything it froze.

## Limits (read these)

- A software guard cannot stop a spike shorter than the sample interval. Set a **hardware power limit** too when you can (`sudo nvidia-smi -pl <watts>`); it reacts in microseconds.
- Pausing needs permission to signal the jobs: run the watcher as the jobs' user, or as root for other users' jobs.
- A paused process still holds its memory and its GPU context; pausing relieves power and CPU, not memory. Use the launcher's RAM admission for memory.
- CPU package power is only measured where the platform exposes it; CPU pressure is guarded through load, temperature and RAM.
- NVIDIA is the tested path; AMD (`rocm-smi`) is best-effort and experimental.

## Develop

```bash
pip install -e '.[test]' && pytest        # 22 tests: state machine, config validation, slots, state-file freshness
```

## License

MIT, see [LICENSE](LICENSE).
