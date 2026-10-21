import json
import os

import pytest

from guardrails.config import Config, load_config
from guardrails.launcher import build_env, slot_names, try_acquire
from guardrails.watcher import read_state, write_state


def test_default_config_is_valid_and_ordered():
    c = Config()
    assert c.gpu_power_resume_frac < c.gpu_power_soft_frac < c.gpu_power_red_frac


def test_unordered_thresholds_are_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"gpu_power_soft_frac": 0.99}))
    with pytest.raises(ValueError):
        load_config(str(p))


def test_unknown_keys_are_rejected(tmp_path):
    p = tmp_path / "typo.json"
    p.write_text(json.dumps({"gpu_powr_soft_frac": 0.5}))
    with pytest.raises(ValueError):
        load_config(str(p))


def test_shipped_profiles_all_load():
    import glob
    files = glob.glob(os.path.join(os.path.dirname(__file__), "..", "configs", "profiles", "*.toml"))
    assert files
    for f in files:
        load_config(f)


def test_slots_are_exclusive(tmp_path):
    cfg = Config(state_dir=str(tmp_path), gpu_slots_per_device=1, cpu_slots=2)
    a = try_acquire("gpu0", cfg)
    assert a is not None and try_acquire("gpu0", cfg) is None   # only one job per GPU
    assert try_acquire("gpu1", cfg) is not None                  # other GPU unaffected
    assert try_acquire("cpu", cfg) is not None and try_acquire("cpu", cfg) is not None and try_acquire("cpu", cfg) is None


def test_bad_class_rejected(tmp_path):
    with pytest.raises(ValueError):
        slot_names("tpu", Config(state_dir=str(tmp_path)))


def test_env_pins_threads_and_device():
    e = build_env("gpu1", Config(threads_per_job=3), base={})
    assert e["OMP_NUM_THREADS"] == "3" and e["CUDA_VISIBLE_DEVICES"] == "1"
    assert build_env("cpu", Config(), base={})["CUDA_VISIBLE_DEVICES"] == ""


def test_state_file_roundtrip_and_stale_means_dead(tmp_path):
    cfg = Config(state_dir=str(tmp_path), state_max_age_s=60)
    assert read_state(cfg)[0] == "DEAD"          # no watcher yet
    write_state(cfg, "GREEN")
    assert read_state(cfg)[0] == "GREEN"
    old = os.path.getmtime(cfg.state_path) - 3600
    os.utime(cfg.state_path, (old, old))
    assert read_state(cfg)[0] == "DEAD"          # stale file => watcher died => launcher must block
