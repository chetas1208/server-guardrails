# Case study: a shared 2-GPU workstation that lost power under agent load

**Setup.** One workstation (20-core CPU, 125 GiB RAM, two 24 GB GPUs with 420 W limits), shared by an experiment pipeline in which about 20 autonomous coding agents each launched their own jobs.

**What went wrong.** Within roughly 20 minutes the machine reached ~44 concurrent GPU processes, a CPU load average near 110 on 20 cores and both GPU memories full. The last logged samples before the machine disappeared:

| Time (UTC) | GPU0 | GPU1 | Summed |
|---|---|---|---|
| 07:22:08 | 398 W | 388 W (82 C) | **786 W** |
| 07:22:42 | 405 W | 356 W | 761 W |
| 07:22:48 | 372 W | 361 W (81 C) | 734 W |

All logging stopped at 07:22:48 and the machine stayed down for hours. The boot history showed **no clean-shutdown record**, so it was an abrupt power-off rather than a reboot. The probable cause is a PSU over-power trip (two cards near their 420 W limits plus a loaded CPU); the system journal for that boot was not readable by the unprivileged user, so this remains an inference, not a proof.

**Collateral damage.** About 740 small checkpoint metadata files were left zero-byte (data written but not flushed before power loss); fixing it meant `fsync` on every write.

**What the guard changed.** Admission control (one job per GPU, four CPU jobs, three threads each) removed the pile-up; the soft cap duty-cycles a single job to ~70% of the power limit; a RED alarm freezes everything when the summed draw approaches the PSU-trip region and thaws it staggered afterwards.

**Lessons that shaped the defaults**
1. Log-only monitoring did not help: nothing acted on the numbers. Enforcement has to be automatic.
2. A single heavy job alone can reach 380-410 W on a 420 W card, so a per-card RED line near the limit (95%) trips on single jobs; the soft cap, not RED, must handle one job.
3. A *frozen* GPU job still holds the card at ~125 W (context resident), so the "calm" test after a freeze cannot demand near-zero power. The default is 40% of summed capacity.
4. Thawing everything at once recreates the spike that caused the freeze; resume one process at a time.
5. Kill-free control matters: every job here was resumable, so pausing cost time but no work.
