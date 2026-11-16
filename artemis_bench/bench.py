"""Benchmark: QuantLib's default Heston calibration (Artemis benchmark command).

    python3 artemis_bench/bench.py      # writes ./artemis_results.json

Headline: one 78-option Heston calibration (6 maturities x 13 strikes, default
AnalyticHestonEngine, LevenbergMarquardt) in a fresh driver process, timed by this
parent from spawn to "ARTEMIS_DONE <nonce>", so work moved into start-up counts.
One untimed warm-up, then the median of BENCH_REPEATS (5). Secondary: the stress
grid priced 20 times through the default engine (420 options each).

How it stays honest: this process never links QuantLib; every repeat is a fresh
process with a 4 GB address-space cap; the calibrated parameters of every repeat
must match the seed's within 1e-8; CPU over wall must stay under 1.3 (single
thread); the harness and its golden data must be unchanged after the run; the
stress-grid tier (0 bit-identical, 1 within 1e-14, 2 within 1e-12) is reported
as a metric so upstream-mergeable versions stand out. A pure-Python canary shows
machine noise.
"""
import json
import os
import statistics
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness as h  # noqa: E402

REPEATS = int(os.environ.get("BENCH_REPEATS", "5"))
MAX_CPU_WALL = 1.3


def canary():
    t = time.perf_counter()
    s = 0.0
    for i in range(1, 400000):
        s += (i % 7) ** 0.5
    return time.perf_counter() - t


def cv(xs):
    return statistics.stdev(xs) / statistics.mean(xs) if len(xs) > 1 else 0.0


def main():
    h.check_build()
    digest = h.harness_digest()
    seed_params = h.load_golden("calib")["params"]
    canary()

    totals, calls, engine, canaries, worst = [], [], [], [], 0.0
    for rep in range(REPEATS + 1):
        total, call, ratio, out = h.run_handshake("calib")
        for k, (x, y) in enumerate(zip(out["params"], seed_params)):
            if abs(x - y) > 1e-8 * max(1.0, abs(y)):
                sys.exit("bench: calibrated param %d is %.17g, seed %.17g; not timing a wrong "
                         "answer" % (k, x, y))
        e_total, _, e_ratio, _ = h.run_handshake("engine")
        for r in (ratio, e_ratio):
            if r > MAX_CPU_WALL:
                sys.exit("bench: cpu/wall %.2f > %.2f; the benchmark is single-threaded"
                         % (r, MAX_CPU_WALL))
        if rep == 0:
            continue  # warm-up: checked, not timed
        totals.append(total)
        calls.append(call)
        engine.append(e_total)
        worst = max(worst, ratio, e_ratio)
        canaries.append(canary())

    tier, max_abs, _, fails = h.price_tier(h.run_untimed("grid")["prices"],
                                           h.load_golden("grid")["prices"],
                                           h.load_golden("reference")["prices"])
    if fails:
        sys.exit("bench: stress grid out of tolerance: %s" % fails[0])
    if h.harness_digest() != digest:
        sys.exit("bench: the harness or its golden data changed during the run")

    print("per-repeat calib  %s" % " ".join("%.4f" % x for x in totals), file=sys.stderr)
    print("per-repeat engine %s" % " ".join("%.4f" % x for x in engine), file=sys.stderr)
    res = {
        "heston_calibration_s": statistics.median(totals),
        "heston_calibration_call_only_s": statistics.median(calls),
        "heston_engine_grid_x20_s": statistics.median(engine),
        "price_tier": tier,
        "max_abs_price_dev_vs_seed": max_abs,
        "heston_calibration_cv": cv(totals),
        "canary_s": statistics.median(canaries),
        "canary_cv": cv(canaries),
        "max_cpu_over_wall": worst,
    }
    with open(os.path.join(os.getcwd(), "artemis_results.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
