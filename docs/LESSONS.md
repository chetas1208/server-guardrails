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

## 8. 2026-10-21: soft-cap chatter let two cards spike together into RED
**What happened (logged).** In 6 seconds the production governor did: pause card 1 (392 W) at 07:36:33, pause card 0 at :34, resume both at :36 and :37 (a paused job idles at ~125-140 W, below the resume mark, so the resume fired after two samples), summed draw 464 -> 623 -> 638 -> **675 W** at :40, then RED froze everything (11 processes, 120 s). Of 11 RED events logged 06:45-07:37 that day, 9 were a single card at 382-414 W (lesson 1), one was at 54 W summed (reason not logged) and this was the only summed-power one.
**Fix.** Resume is gated: `soft_resume_spacing_s` (8 s) between resumes on any device, and with 2+ devices a projected summed draw (job back at its pre-pause power) must stay under `total_gpu_power_resume_frac` (0.68, below the 0.76 RED line). Regression tests: `test_resume_is_spaced_across_devices`, `test_resume_blocked_when_projected_total_would_reach_red_zone`. Not yet validated on live hardware (the production governor still has the old rule): UNVERIFIED that chatter drops.

## 9. 2026-10-21: three shipped profiles had the summed RED line set to 1.02 (disabled)
**What happened.** When the per-card RED line was raised to 1.02 (lesson 1), the same value was also written to `total_gpu_power_red_frac` in the `gpu-workstation`, `multi-gpu-server` and `shared-cloud-vm` profiles. A summed draw can never exceed ~100% of summed limits, so the summed RED rule was silently off in exactly the profiles meant for GPU boxes, while their comments promised 76%.
**Fix.** `gpu-workstation` back to 0.76 (the logged 675 W event is 80% of 840 W; the 2026-10-20 power-off was at 734-786 W, 87-94%); `multi-gpu-server` and `shared-cloud-vm` 0.70 (a deliberately tighter choice, not validated on hardware: UNVERIFIED). Regression test `test_gpu_profiles_keep_a_real_summed_power_red_line`.

## 10. 2026-10-21: a language model as overload predictor lost to "current summed power" (rules beat the model here)
Replay of the production power log (1,931 rows, label = summed draw exceeds 500 W within 15 s; leave-one-block-out over 6 blocks, blocks 0-2 before the guard, 3-5 guarded with only 15, 31 and 53 positives). A local open-weights text decision model (CPU, zero-shot on a JSON state of the current sample; 2,230 s to score the log) was compared with three simple predictors:

| Predictor | Pooled AUC [95% CI] | Guarded-blocks AUC | Recall at 5% FPR |
|---|---|---|---|
| persistence (current summed power) | 0.955 [0.924, 0.974] | 0.788 | 0.85 |
| slope rule | 0.863 [0.819, 0.902] | 0.664 | 0.68 |
| logistic regression, 6 features | 0.960 [0.916, 0.985] | 0.621 | 0.91 |
| text model, zero-shot | 0.924 [0.871, 0.957] | 0.749 | 0.68 |
| text model + LR recalibration | 0.923 [0.867, 0.958] | 0.749 | 0.66 |

The text model is below the best simple baseline on the pooled score (CIs overlap, difference not shown to exist) and on the guarded blocks (0.749 vs 0.788; no CI computed there, positives are few). It does not beat the baseline by any margin that would justify an advisor, so none is built. The slope rule and the logistic model also failed to generalize to the guarded regime. Reading: a threshold on current summed power is the predictor to beat, and the guard already is one. Caveats: one machine, one 15 s horizon and 500 W label, tiny guarded positive counts, zero-shot prompt only; the label is nearly a function of the current power, which favors persistence.
