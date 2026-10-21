from guardrails.config import Config
from guardrails.controller import GREEN, RED, THAW, Controller, Device, Proc, Sample


def smp(p0=100, p1=100, t0=50, t1=50, ram=10, load=1):
    return Sample([Device(0, p0, 420, t0), Device(1, p1, 420, t1)], ram, 125, load, 20)


def procs():
    return [Proc(10, 1.0, 2.0, 0), Proc(11, 2.0, 2.0, 0), Proc(20, 3.0, 2.0, 1)]


def test_green_when_quiet():
    c = Controller(Config())
    assert c.step(smp(), procs(), 0) == [] and c.mode == GREEN


def test_soft_cap_pauses_youngest_on_that_device_only():
    c = Controller(Config())
    acts = c.step(smp(p0=330), procs(), 0)  # 330 > 70% of 420 = 294
    assert [(a.kind, a.pid) for a in acts] == [("PAUSE", 11)]  # pid 11 is the youngest on device 0
    assert c.mode == GREEN and 11 in c.held


def test_soft_resume_after_power_drops():
    c = Controller(Config())
    c.step(smp(p0=330), procs(), 0)
    assert c.step(smp(p0=100), [p for p in procs() if p.pid != 11], 4) == []        # one calm sample is not enough
    acts = c.step(smp(p0=100), [p for p in procs() if p.pid != 11], 8)
    assert [(a.kind, a.pid) for a in acts] == [("RESUME", 11)] and not c.held


def test_red_freezes_every_process_and_never_kills():
    c = Controller(Config())
    acts = c.step(smp(p0=430), procs(), 0)  # a card above its own 420 W limit
    kinds = [a.kind for a in acts]
    assert c.mode == RED and kinds.count("FREEZE") == 3 and "MODE" in kinds
    assert not any(a.kind in ("KILL", "TERM") for a in acts)


def test_total_power_triggers_red():
    c = Controller(Config())
    c.step(smp(p0=330, p1=330), procs(), 0)  # 660 > 76% of 840 = 638
    assert c.mode == RED


def test_ram_over_red_triggers_red():
    c = Controller(Config())
    c.step(smp(ram=80), procs(), 0)  # 80/125 = 64% > 56%
    assert c.mode == RED


def test_thaw_needs_cooldown_and_calm_then_is_staggered_oldest_first():
    cfg = Config(red_cooldown_s=10, calm_samples=3, thaw_spacing_s=8)
    c = Controller(cfg)
    c.step(smp(p0=430), procs(), 0)
    calm = smp(p0=100, p1=100)
    for t in (1, 2, 3, 4):                       # calm but cooldown (10 s) not elapsed
        assert c.step(calm, [], t) == [] and c.mode == RED
    c.step(calm, [], 11)                         # cooldown over + calm samples reached -> THAW
    assert c.mode == THAW
    order = []
    for t in (12, 13, 20, 28, 36):
        for a in c.step(calm, [], t):
            if a.kind == "THAW":
                order.append((t, a.pid))
    assert order == [(12, 10), (20, 11), (28, 20)]   # oldest first, 8 s apart
    assert c.mode == GREEN


def test_repeated_red_doubles_cooldown_and_is_capped():
    cfg = Config(red_cooldown_s=100, red_cooldown_max_s=250, calm_samples=1)
    c = Controller(cfg)
    c.step(smp(p0=430), procs(), 0)
    assert c._cool == 100
    c.mode = THAW
    c.step(smp(p0=430), procs(), 5)
    assert c._cool == 200
    c.mode = THAW
    c.step(smp(p0=430), procs(), 6)
    assert c._cool == 250  # capped


def test_red_during_thaw_refreezes():
    cfg = Config(red_cooldown_s=1, calm_samples=1, thaw_spacing_s=100)
    c = Controller(cfg)
    c.step(smp(p0=430), procs(), 0)
    c.step(smp(), [], 2)
    assert c.mode == THAW
    acts = c.step(smp(p0=435), [Proc(99, 5.0, 1.0, 0)], 3)
    assert c.mode == RED and any(a.kind == "FREEZE" and a.pid == 99 for a in acts)


def test_cpu_less_server_has_no_gpu_and_still_guards_ram():
    c = Controller(Config())
    s = Sample([], 80, 125, 1, 8)
    c.step(s, [Proc(1, 1.0, 3.0, None)], 0)
    assert c.mode == RED


def test_load_per_cpu_triggers_red():
    c = Controller(Config(load_red_per_cpu=4.0))
    c.step(Sample([], 10, 125, 100, 20), [Proc(1, 1.0, 1.0, None)], 0)  # 100/20 = 5 per cpu
    assert c.mode == RED


def test_single_gpu_machine_soft_pauses_instead_of_red():
    c = Controller(Config())
    s = Sample([Device(0, 330, 420, 60)], 10, 125, 1, 20)   # 78% of the limit: above soft (70%), below red (95%)
    acts = c.step(s, [Proc(1, 1.0, 2.0, 0)], 0)
    assert c.mode == GREEN and [a.kind for a in acts] == ["PAUSE"]


def test_single_job_spiking_near_the_cap_does_not_trigger_red():
    c = Controller(Config())
    c.step(smp(p0=410), procs(), 0)  # 97.6% of the limit: normal for one heavy job; soft cap pauses it, no RED
    assert c.mode == GREEN


def test_escalation_is_forgiven_after_calm():
    cfg = Config(red_cooldown_s=10, calm_samples=1, thaw_spacing_s=1, red_forgive_s=50)
    c = Controller(cfg)
    c.step(smp(p0=430), procs(), 0)
    assert c.red_count == 1
    c.step(smp(), [], 11); c.step(smp(), [], 12); c.step(smp(), [], 13); c.step(smp(), [], 14)
    assert c.mode == GREEN
    c.step(smp(), [], 200)  # calm GREEN for > red_forgive_s
    assert c.red_count == 0


def _pause_both(c, t=0):
    """Both cards over the soft cap: one job paused on each device."""
    acts = c.step(smp(p0=300, p1=300), procs(), t)
    assert {a.kind for a in acts} == {"PAUSE"} and len(c.held) == 2


def test_resume_is_spaced_across_devices():
    """Logged 2026-10-21: both paused jobs were resumed 1 s apart, ramped together and summed to 675 W (RED)."""
    c = Controller(Config(soft_act_gap_s=1))
    _pause_both(c)
    left = [Proc(10, 1.0, 2.0, 0), Proc(11, 2.0, 2.0, 0)]
    idle = smp(p0=125, p1=125)                      # paused jobs idle near 125 W
    kinds = []
    for t in range(1, 8):
        kinds += [(t, a.kind) for a in c.step(idle, left, t)]
    assert [k for _, k in kinds] == ["RESUME"]       # only one resume within soft_resume_spacing_s (8 s)
    assert [a.kind for a in c.step(idle, left, 10)] == ["RESUME"]   # the other one follows after the spacing


def test_resume_blocked_when_projected_total_would_reach_red_zone():
    c = Controller(Config(soft_act_gap_s=1, soft_resume_spacing_s=0))
    c.step(smp(p0=392, p1=392), procs(), 0)         # pause one job per device at ~392 W each
    c.held[10] = {"reason": "dev0", "start": 1.0, "device": 0, "peak_w": 392}
    c.held[20] = {"reason": "dev1", "start": 3.0, "device": 1, "peak_w": 392}
    # projected summed draw if dev0's job returns: 125 + 125 - 125 + 392 = 517 < 0.68 * 840 = 571 -> allowed
    # but with dev1 already ramped to 330 W: 125 + 330 - 125 + 392 = 722 -> blocked
    acts = c.step(smp(p0=125, p1=330), [], 5)
    assert [a for a in acts if a.kind == "RESUME"] == []


def test_resume_gate_does_not_apply_to_a_single_device():
    c = Controller(Config(soft_act_gap_s=1))
    s1 = lambda p: Sample([Device(0, p, 420, 50)], 10, 125, 1, 20)
    c.step(s1(392), [Proc(10, 1.0, 2.0, 0)], 0)
    c.step(s1(125), [], 2)
    assert [a.kind for a in c.step(s1(125), [], 4)] == ["RESUME"]


def test_replay_logged_samples_never_resume_two_devices_within_spacing():
    """Replay of the per-second powers logged just before a 728 W RED (2026-10-21): the old rule resumed card 0 at t=4 and
    card 1 at t=5. The samples are replayed open loop, so this checks the resume spacing, not the physics."""
    seq = [(0, 377.9, 243.1), (2, 195.6, 399.3), (3, 130.1, 174.3), (4, 125.0, 145.3), (5, 366.6, 139.0),
           (6, 341.4, 386.5), (7, 168.6, 188.1), (8, 129.2, 147.0), (9, 124.6, 139.1), (10, 120.2, 132.2)]
    live = [Proc(10, 1.0, 2.0, 0), Proc(20, 2.0, 2.0, 1)]
    c, resumes = Controller(Config(soft_act_gap_s=1)), []
    for t, p0, p1 in seq:
        resumes += [t for a in c.step(smp(p0=p0, p1=p1), [p for p in live if p.pid not in c.held], t) if a.kind == "RESUME"]
    assert len(resumes) <= 1 or all(b - a >= 8 for a, b in zip(resumes, resumes[1:])), resumes
