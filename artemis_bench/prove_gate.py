"""Prove the gate both ways: the seed passes at tier 0, each injected fault fails.

    python3 artemis_bench/prove_gate.py

Patches ql/pricingengines/vanilla/analytichestonengine.cpp in place one fault at a time,
rebuilds through setup.sh (incremental: one file and a relink), runs gate.py, and restores
the original bytes afterwards (also on error), rebuilding the seed at the end.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "ql", "pricingengines", "vanilla", "analytichestonengine.cpp")


def first(old, new):
    def f(src):
        assert old in src, "pattern missing: %r" % old
        return src.replace(old, new, 1)
    return f


FAULTS = {
    # Plausible "faster" edits that change results: fewer nodes, no control variate, and a
    # tiny systematic error that a loose gate would wave through.
    "default nodes cut from 144 to 32": first(
        "Integration::gaussLaguerre(integrationOrder)", "Integration::gaussLaguerre(Size(32))"),
    "relative error of 1e-11 on every price": first(
        "results_.value = priceVanillaPayoff(payoff, exerciseDate);",
        "results_.value = priceVanillaPayoff(payoff, exerciseDate) * (1.0 + 1e-11);"),
}
PROBES = {
    # Not required to fail: shows which tier a plain switch of the default lands in.
    "default nodes cut from 144 to 64 (probe)": first(
        "Integration::gaussLaguerre(integrationOrder)", "Integration::gaussLaguerre(Size(64))"),
}


def sh(*cmd):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip().splitlines()


def build_and_gate():
    rc, out = sh("sh", os.path.join(HERE, "setup.sh"))
    if rc != 0:
        return rc, ["build failed: " + (out[-1] if out else "")]
    return sh(sys.executable, "-I", os.path.join(HERE, "gate.py"))


def main():
    with open(ENGINE) as fh:
        seed = fh.read()
    ok = True
    try:
        rc, out = build_and_gate()
        ok &= rc == 0 and "tier 0" in out[-1]
        print("seed                                      %s  %s" % ("PASS" if rc == 0 else "FAIL", out[-1]))
        for name, patch in list(FAULTS.items()) + list(PROBES.items()):
            with open(ENGINE, "w") as fh:
                fh.write(patch(seed))
            rc, out = build_and_gate()
            if name in FAULTS:
                ok &= rc != 0
                verdict = "CAUGHT" if rc else "MISSED"
            else:
                verdict = "PASS" if rc == 0 else "FAIL"
            fail = next((l for l in out if l.startswith("FAIL")), out[-1] if out else "")
            print("%-41s %s  %s" % (name, verdict, fail[:90]))
            with open(ENGINE, "w") as fh:
                fh.write(seed)
    finally:
        with open(ENGINE, "w") as fh:
            fh.write(seed)
        sh("sh", os.path.join(HERE, "setup.sh"))
    print("ALL AS EXPECTED" if ok else "SOMETHING UNEXPECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
