"""guardrails CLI: watch | run | status | doctor | simulate"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .controller import Controller, Device, Proc, Sample


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="guardrails", description="Keep shared servers alive: soft power/RAM caps, red-alert freeze, staggered thaw.")
    ap.add_argument("-c", "--config", help="TOML/JSON profile (see configs/profiles)")
    ap.add_argument("--dry-run", action="store_true", help="log decisions but never signal any process")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="run the watcher loop"); w.add_argument("--once", action="store_true")
    r = sub.add_parser("run", help="run a heavy job through admission control")
    r.add_argument("klass", help="cpu | gpu<N>"); r.add_argument("--ram", type=float, default=8.0, help="expected job RAM in GiB")
    r.add_argument("job", nargs=argparse.REMAINDER, help="-- command ...")
    sub.add_parser("status", help="print watcher state")
    sub.add_parser("doctor", help="what can this machine measure? suggests a profile")
    s = sub.add_parser("simulate", help="replay a JSON list of samples through the controller (no signals sent)")
    s.add_argument("samples", help="JSON file: [{\"t\":0,\"devices\":[{\"id\":0,\"power_w\":300,\"limit_w\":420,\"temp_c\":60}],\"ram_used_gib\":10,\"ram_total_gib\":125}, ...]")
    a = ap.parse_args(argv)
    cfg = load_config(a.config, dry_run=True if a.dry_run else None)

    if a.cmd == "watch":
        from .watcher import run_watch
        run_watch(cfg, once=a.once)
    elif a.cmd == "run":
        from .launcher import run
        job = [x for x in a.job if x != "--"] if a.job and a.job[0] == "--" else a.job
        if not job:
            ap.error("no command given after --")
        run(a.klass, cfg, job, a.ram)
    elif a.cmd == "status":
        from .watcher import read_state
        mode, age = read_state(cfg)
        print(f"{mode} (state file age {age:.0f}s)")
        return 0 if mode == "GREEN" else 1
    elif a.cmd == "doctor":
        from .probes import doctor
        print(json.dumps(doctor(), indent=2))
    elif a.cmd == "simulate":
        ctl = Controller(cfg)
        procs = [Proc(1, 1.0, 2.0, 0), Proc(2, 2.0, 2.0, 0), Proc(3, 3.0, 2.0, 1)]
        for row in json.load(open(a.samples)):
            smp = Sample([Device(**d) for d in row.get("devices", [])], row.get("ram_used_gib", 0), row.get("ram_total_gib", 1),
                         row.get("load1", 0), row.get("ncpu", 1), row.get("cpu_temp_c"))
            acts = ctl.step(smp, [p for p in procs if p.pid not in ctl.held], row["t"])
            print(f"t={row['t']:>6} mode={ctl.mode:<5} " + ("; ".join(f"{x.kind} {x.pid or ''} {x.reason}" for x in acts) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
