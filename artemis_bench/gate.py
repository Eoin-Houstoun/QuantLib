"""Correctness gate for the Heston calibration benchmark (Artemis test command).

    python3 $QL_BENCH_DIR/trusted/gate.py    # from the checkout root: check it against golden
    python3 artemis_bench/gate.py --record   # once, before setup.sh --seed: write golden

Stress grid: 5 Heston regimes (incl. sigma 1.5 with rho -0.95, very low variance, a
Feller-violating case, positive rho) x 6 maturities (1 week to 10 years) x 7 strikes
(0.5 to 2x spot) x call/put = 420 prices through the default AnalyticHestonEngine.

Tiers, reported so a run shows which versions could merge upstream:
  tier 0  every price bit-identical to the seed
  tier 1  within 1e-14 absolute
  tier 2  within 1e-12 absolute and 1e-10 relative
Beyond tier 2 fails. No price may be further from an adaptive Gauss-Lobatto (1e-12 relative, 1e-14 absolute)
reference than the seed's was (plus 1e-12). The calibration must land on the seed's
parameters within 1e-8 (relative, or absolute below 1) and fit no worse. Then
QuantLib's own HestonModelTests must pass.
"""
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness as h  # noqa: E402

# source_hash() of the seed: QuantLib 27196d2 ql/ plus driver.cpp; --record refuses anything else.
SEED_SOURCE_HASH = "fbcee2f4f98b5fa1d9f058e1b4a794c49d338a3d35983648b4f2c49a736d1b4a"


def main():
    record = "--record" in sys.argv
    h.check_build()
    if not record and not h.checkout_harness_unchanged():
        print("FAIL the version edited artemis_bench/, which it must not touch", file=sys.stderr)
        print("gate: failed (harness edited)")
        return 1
    digest = h.harness_digest()
    grid = h.run_untimed("grid")["prices"]
    _, _, _, calib = h.run_handshake("calib")

    if record:
        if h.source_hash() != SEED_SOURCE_HASH:
            sys.exit("gate --record: ql/ or driver.cpp is not the seed; refusing")
        os.makedirs(h.GOLDEN, exist_ok=True)
        reference = h.run_untimed("reference")["prices"]
        for name, data in (("grid", {"prices": grid}), ("reference", {"prices": reference}),
                           ("calib", calib), ("seed", {"source_hash": h.source_hash()})):
            with open(os.path.join(h.GOLDEN, name + ".json"), "w") as fh:
                json.dump(data, fh)
        worst = max(abs(g - r) for g, r in zip(grid, reference))
        print("recorded golden: %d grid prices (seed vs reference max abs %.3g), params %s"
              % (len(grid), worst, calib["params"]))
        return 0

    seed, reference, seed_calib = (h.load_golden("grid")["prices"],
                                   h.load_golden("reference")["prices"], h.load_golden("calib"))
    tier, max_abs, max_rel, fails = h.price_tier(grid, seed, reference)
    for k, (x, y) in enumerate(zip(calib["params"], seed_calib["params"])):
        if abs(x - y) > 1e-8 * max(1.0, abs(y)):
            fails.append("calib: param %d is %.17g, seed %.17g" % (k, x, y))
    fit, seed_fit = sum(abs(e) for e in calib["errors"]), sum(abs(e) for e in seed_calib["errors"])
    if fit > seed_fit + 1e-10:
        fails.append("calib: total error %.6g, seed %.6g" % (fit, seed_fit))
    if h.harness_digest() != digest:
        fails.append("the harness or its golden data changed during the run")
    for f in fails:
        print("FAIL", f, file=sys.stderr)
    if fails:
        print("gate: failed (%d)" % len(fails))
        return 1
    t = subprocess.run(h.SANDBOX + [h.TEST_SUITE, "--run_test=QuantLibTests/HestonModelTests",
                        "--log_level=nothing", "--report_level=short"],
                       capture_output=True, text=True, timeout=3600,
                       preexec_fn=h.limit_worker_memory, cwd=h.WORK)
    print("HestonModelTests:", (t.stdout + t.stderr).strip().splitlines()[-1:] or ["no output"])
    if t.returncode != 0:
        print("gate: failed (QuantLib HestonModelTests)")
        return 1
    print("gate: pass, tier %d (max abs %.3g, max rel %.3g vs seed)" % (tier, max_abs, max_rel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
