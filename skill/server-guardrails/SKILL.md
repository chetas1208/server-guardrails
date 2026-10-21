---
name: server-guardrails
description: Protect a shared GPU/CPU server from overload and power trips when launching heavy or parallel jobs (multi-agent runs, training, benchmarks). Sets soft power/RAM caps, a red-alert freeze of all jobs, a staggered thaw, and an admission-controlled launcher. Use when asked to "save the server", cap GPU power or RAM, stop the box from shutting down, queue jobs safely, or design low-load process schedules.
---

# server-guardrails

Use this skill whenever work would put real load on a machine you share: many concurrent jobs or agents, GPU training or inference, big benchmarks. It never kills anything: it **pauses** (SIGSTOP) and **resumes** (SIGCONT).

## 1. Inspect before changing anything
```bash
python -m guardrails doctor        # GPUs, power limits, RAM, temperature sensors, whether a hardware power limit can be set
python -m guardrails status        # GREEN / RED / THAW / DEAD (DEAD = watcher not running)
```
Pick a profile from `configs/profiles/` (gpu-workstation, multi-gpu-server, cpu-only, laptop, shared-cloud-vm, ci-runner) and copy it; do not invent thresholds. Thresholds are fractions of detected capacity.

## 2. Start the watcher (and keep it alive)
```bash
python -m guardrails -c configs/profiles/gpu-workstation.toml watch &     # or install examples/guardrails.service (systemd)
```
Prefer a supervisor (systemd `Restart=always` or `examples/supervisor.sh`). If a hardware power limit can be set (`doctor` says so), also set it: it reacts faster than software.

## 3. Route EVERY heavy job through the launcher
```bash
python -m guardrails -c <profile> run gpu0 --ram 12 -- python train.py
python -m guardrails -c <profile> run cpu  --ram 4  -- python preprocess.py
```
It queues for a slot (one job per GPU, N CPU jobs), waits for GREEN and RAM headroom, pins thread counts, lowers priority, then execs the job. Run **one job at a time yourself**: no background fan-out, no process swarms. Never bypass the launcher for convenience.

## 4. Behave correctly when the watcher intervenes
- A paused or stalled job is **not a failure**: wait; do not relaunch or kill it.
- RED means everything is frozen on purpose; it thaws one process at a time after a cooldown. Do not restart jobs during RED/THAW.
- Make jobs resumable (checkpoints, append-only results, `fsync` after writes).
- If the state file says DEAD, stop launching and tell the user the watcher is down.

## 5. Report
Say which profile is active, the current state, the number of RED events and pauses (`events.jsonl`), and any job that never ran. Do not claim the server is "safe" beyond what the watcher measured; software caps cannot stop spikes shorter than the sample interval.

## Safety rules
Pausing only affects processes in the configured scope. Never widen the scope to other users' processes without explicit permission. Never send signals other than SIGSTOP/SIGCONT.
