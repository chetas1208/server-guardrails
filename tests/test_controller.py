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
    acts = c.step(smp(p0=418), procs(), 0)  # > 95% of 420
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
    c.step(smp(p0=418), procs(), 0)
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
    c.step(smp(p0=418), procs(), 0)
    assert c._cool == 100
    c.mode = THAW
    c.step(smp(p0=418), procs(), 5)
    assert c._cool == 200
    c.mode = THAW
    c.step(smp(p0=418), procs(), 6)
    assert c._cool == 250  # capped


def test_red_during_thaw_refreezes():
    cfg = Config(red_cooldown_s=1, calm_samples=1, thaw_spacing_s=100)
    c = Controller(cfg)
    c.step(smp(p0=418), procs(), 0)
    c.step(smp(), [], 2)
    assert c.mode == THAW
    acts = c.step(smp(p0=419), [Proc(99, 5.0, 1.0, 0)], 3)
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
