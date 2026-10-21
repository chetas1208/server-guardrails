# Field notes (kept current as the guard runs in production)

Each entry: what happened, the fix, and where it lives in the code. Newest first.

## 1. A per-card RED line near the limit made the guard the problem
**What happened.** One heavy job on a 420 W card legitimately touches 400-415 W. A per-card RED line at 95-99% of the limit tripped on single jobs. Repeated alarms doubled the cooldown (up to 16 minutes) and froze everything, including the job that mattered most.
**Fix.** Per-card RED now defaults to 1.02x the card's own limit (it only fires if a card exceeds its limit); RED is for the *summed* draw, temperature, RAM and load. The soft cap duty-cycles single jobs. The cooldown cap dropped from 30 to 10 minutes and the escalation counter is forgiven after `red_forgive_s` of calm.
**Code.** `Config.gpu_power_red_frac`, `red_cooldown_max_s`, `red_forgive_s`; `Controller.step`.

## 2. After editing a running supervisor script, restart the supervisor, not just its child
**What happened.** A shell supervisor was edited to new thresholds, but the already-running supervisor kept relaunching the watcher with the old command line.
**Fix.** Keep thresholds in the config file the watcher re-reads at start, not on the supervisor's command line; restart both when changing either. `examples/supervisor.sh` takes the profile path only.

## 3. A frozen GPU job still draws power
**What happened.** A frozen process keeps its CUDA context; the card sits at ~125 W, not idle. A "calm" test that required near-zero power never passed and the freeze lasted forever.
**Fix.** `calm_total_power_frac` is 40% of summed capacity, not a near-zero threshold.

## 4. The summed-power line only makes sense with more than one device
**What happened.** On a single-GPU box the summed line (76%) sat on top of the soft cap (70%), so a small overshoot jumped straight to RED.
**Fix.** The summed rule applies only when two or more devices are present.

## 5. Thaw one process at a time
Resuming everything at once recreates the spike that caused the freeze. `thaw_spacing_s` staggers the restart; the oldest process resumes first.

## 6. Software caps react in seconds, hardware limits in microseconds
Use both when you can: `nvidia-smi -pl <watts>` (needs root) sets a hardware limit; the watcher covers what you cannot set.

## 7. Flush your checkpoints
An abrupt power-off left hundreds of zero-byte files (data written, not flushed). Write-then-rename with `fsync` of the file and its directory.
