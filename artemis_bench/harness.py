"""Shared helpers for gate.py and bench.py. Standard library only; never links QuantLib.

The driver is built by setup.sh into a cached build folder outside the checkout
(QL_BENCH_DIR, default ~/.cache/artemis-ql-heston), because a runner checks out
every version fresh and a full QuantLib build takes most of an hour. setup.sh
syncs the checkout into that folder with rsync --checksum, so unchanged files
keep their timestamps and only edited files recompile, then records a hash of
the sources it built. Every parent checks that hash against this checkout before
trusting the driver.
"""
import hashlib
import json
import os
import resource
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH_DIR = os.environ.get("QL_BENCH_DIR") or os.path.expanduser("~/.cache/artemis-ql-heston")
DRIVER = os.path.join(BENCH_DIR, "build", "heston_driver")
TEST_SUITE = os.path.join(BENCH_DIR, "build", "test-suite", "quantlib-test-suite")
GOLDEN = os.path.join(HERE, "golden")
HARNESS = ("bench.py", "gate.py", "harness.py", "driver.cpp", "setup.sh")
WORKER_MEMORY = 4 << 30


def tree_hash(paths):
    """sha256 over the relative path and bytes of every file under the given roots."""
    h = hashlib.sha256()
    files = []
    for p in paths:
        full = os.path.join(ROOT, p)
        if os.path.isfile(full):
            files.append(full)
        for d, dirs, names in os.walk(full):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            files += [os.path.join(d, n) for n in names]
    for f in sorted(files):
        h.update(os.path.relpath(f, ROOT).encode())
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def source_hash():
    """What setup.sh must have built: the library sources plus the driver."""
    return tree_hash(["ql", os.path.join("artemis_bench", "driver.cpp")])


def check_build():
    stamp = os.path.join(BENCH_DIR, "built-from")
    try:
        with open(stamp) as fh:
            built = fh.read().strip()
    except FileNotFoundError:
        sys.exit("harness: no build in %s; run artemis_bench/setup.sh first" % BENCH_DIR)
    if built != source_hash():
        sys.exit("harness: the cached build is not of this checkout; run artemis_bench/setup.sh")
    if not os.access(DRIVER, os.X_OK):
        sys.exit("harness: %s missing" % DRIVER)


def harness_digest():
    """One hash over the harness scripts and every golden file."""
    return tree_hash([os.path.join("artemis_bench", f) for f in HARNESS] +
                     [os.path.join("artemis_bench", "golden")])


def limit_worker_memory():
    """preexec_fn for every driver run: a hard address-space cap."""
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY, WORKER_MEMORY))


def cpu_seconds(pid):
    """utime+stime of the process and its reaped children, all threads (/proc/<pid>/stat)."""
    with open("/proc/%d/stat" % pid) as fh:
        fields = fh.read().rsplit(")", 1)[1].split()
    return sum(int(x) for x in fields[11:15]) / os.sysconf("SC_CLK_TCK")


def run_untimed(mode, timeout=1800):
    """Run a driver mode with no handshake and return its JSON output."""
    r = subprocess.run([DRIVER, mode], capture_output=True, text=True, timeout=timeout,
                       preexec_fn=limit_worker_memory, cwd=BENCH_DIR)
    if r.returncode != 0:
        sys.exit("harness: driver %s exited %d: %s" % (mode, r.returncode, r.stderr[-500:]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def load_golden(name):
    with open(os.path.join(GOLDEN, name + ".json")) as fh:
        return json.load(fh)


def price_tier(prices, seed, reference):
    """(tier, max_abs, max_rel, failures) for the stress grid.

    tier 0: every price bit-identical to the seed; 1: within 1e-14 absolute;
    2: within 1e-12 absolute and 1e-10 relative. Beyond tier 2 fails. Independently,
    no price may be further from the Gauss-Lobatto 1e-12 reference than the seed's
    price was, plus 1e-12."""
    fails = []
    if len(prices) != len(seed):
        return None, None, None, ["grid: %d prices, seed %d" % (len(prices), len(seed))]
    diffs = [abs(c - s) for c, s in zip(prices, seed)]
    rels = [d / abs(s) if s else d for d, s in zip(diffs, seed)]
    max_abs, max_rel = max(diffs), max(rels)
    if all(c == s for c, s in zip(prices, seed)):
        tier = 0
    elif max_abs <= 1e-14:
        tier = 1
    elif max_abs <= 1e-12 and max_rel <= 1e-10:
        tier = 2
    else:
        tier = None
        fails.append("grid: max abs diff %.3g, max rel %.3g vs seed (limit 1e-12 / 1e-10)"
                     % (max_abs, max_rel))
    for i, (c, s, r) in enumerate(zip(prices, seed, reference)):
        if abs(c - r) > abs(s - r) + 1e-12:
            fails.append("grid[%d]: %.17g is further from the reference %.17g than the seed"
                         % (i, c, r))
            break
    return tier, max_abs, max_rel, fails


def run_handshake(mode, timeout=1800):
    """One fresh driver: returns (spawn-to-done s, call s, cpu/wall, JSON output).

    The headline clock runs from spawn to "ARTEMIS_DONE <nonce>", so work moved into
    start-up counts; CPU over wall is the larger of the whole lifetime (rusage, reaped
    children included) and the timed call when it is long enough to judge (>0.5 s)."""
    import secrets
    import time
    spawn = time.perf_counter()
    p = subprocess.Popen([DRIVER, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1, cwd=BENCH_DIR,
                         preexec_fn=limit_worker_memory)
    for line in p.stdout:
        if line.strip() == "ARTEMIS_READY":
            break
    else:
        sys.exit("harness: driver %s never became ready: %s" % (mode, p.stderr.read()[-500:]))
    nonce = secrets.token_hex(16)
    c0, t0 = cpu_seconds(p.pid), time.perf_counter()
    p.stdin.write(nonce + "\n")
    p.stdin.flush()
    t1 = c1 = None
    for line in p.stdout:
        if line.strip() == "ARTEMIS_DONE " + nonce:
            t1, c1 = time.perf_counter(), cpu_seconds(p.pid)
            break
    if t1 is None:
        sys.exit("harness: driver %s ended without finishing the timed call" % mode)
    rest = p.stdout.read()
    _, status, ru = os.wait4(p.pid, 0)
    life = time.perf_counter() - spawn
    if os.waitstatus_to_exitcode(status) != 0:
        sys.exit("harness: driver %s exited %d" % (mode, os.waitstatus_to_exitcode(status)))
    ratio = (ru.ru_utime + ru.ru_stime) / life
    if t1 - t0 > 0.5:
        ratio = max(ratio, (c1 - c0) / (t1 - t0))
    return t1 - spawn, t1 - t0, ratio, json.loads(rest.strip().splitlines()[-1])
