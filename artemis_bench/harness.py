"""Shared helpers for gate.py and bench.py. Standard library only; never links QuantLib.

The Script runs these from the trusted copy in QL_BENCH_DIR/trusted (made by
`setup.sh --seed`), with the current directory at the version's checkout. So HERE is
the trusted harness and ROOT is the code under test; a version can't edit what judges it,
and any edit it makes to its own artemis_bench/ fails the gate.

setup.sh builds each version in QL_BENCH_DIR/work, a fresh copy of the pristine seed build
with only the version's changed files recompiled, and records a hash of the sources it
built; every parent checks that hash against the checkout before trusting the driver.
"""
import hashlib
import json
import os
import resource
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.getcwd()
BENCH_DIR = os.environ.get("QL_BENCH_DIR") or os.path.expanduser("~/.cache/artemis-ql-heston")
WORK = os.path.join(BENCH_DIR, "work")
DRIVER = os.path.join(WORK, "build", "heston_driver")
TEST_SUITE = os.path.join(WORK, "build", "test-suite", "quantlib-test-suite")
GOLDEN = os.path.join(HERE, "golden")
HARNESS = ("bench.py", "gate.py", "harness.py", "driver.cpp", "setup.sh")
WORKER_MEMORY = 4 << 30
# Every driver and test-suite run is sandboxed: everything read-only, $HOME and /run hidden
# except the build, a private /tmp, no network, and its own PID/IPC/user namespaces.
SANDBOX = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", os.path.expanduser("~"),
           "--ro-bind", WORK, WORK, "--unshare-all", "--new-session", "--die-with-parent",
           "--cap-drop", "ALL", "--clearenv", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
           "--setenv", "HOME", WORK, "--chdir", WORK]


def tree_hash(paths, base=None):
    """sha256 over the relative path and bytes of every file under the given roots."""
    base = base or ROOT
    h = hashlib.sha256()
    files = []
    for p in paths:
        full = os.path.join(base, p)
        if os.path.isfile(full):
            files.append(full)
        for d, dirs, names in os.walk(full):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            files += [os.path.join(d, n) for n in names]
    for f in sorted(files):
        h.update(os.path.relpath(f, base).encode())
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def source_hash():
    """What setup.sh must have built: the checkout's library sources plus the trusted driver."""
    h = hashlib.sha256()
    h.update(tree_hash(["ql"]).encode())
    with open(os.path.join(HERE, "driver.cpp"), "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def built_binaries():
    lib_dir = os.path.join(WORK, "build", "ql")
    libs = sorted(f for f in os.listdir(lib_dir) if f.startswith("libQuantLib.so"))
    return [DRIVER, TEST_SUITE] + [os.path.join(lib_dir, f) for f in libs]


def build_stamp():
    """The checkout's sources plus a hash of each binary built from them."""
    return " ".join([source_hash()] + [file_hash(p) for p in built_binaries()
                                      if os.path.isfile(p) and not os.path.islink(p)])


def check_build():
    try:
        with open(os.path.join(WORK, "built-from")) as fh:
            built = fh.read().strip()
    except FileNotFoundError:
        sys.exit("harness: no build in %s; run the trusted setup.sh first" % WORK)
    if built.split(" ")[0] != source_hash():
        sys.exit("harness: the build is not of this checkout; run the trusted setup.sh")
    if built != build_stamp():
        sys.exit("harness: the driver, test suite or library changed since they were built")
    if not os.access(DRIVER, os.X_OK):
        sys.exit("harness: %s missing" % DRIVER)


def harness_digest():
    """One hash over the trusted harness scripts and every golden file."""
    return tree_hash(list(HARNESS) + ["golden"], base=HERE)


def checkout_harness_unchanged():
    """The version's own artemis_bench/ must match the trusted copy it was made from."""
    for f in HARNESS:
        mine, trusted = os.path.join(ROOT, "artemis_bench", f), os.path.join(HERE, f)
        with open(trusted, "rb") as a:
            want = a.read()
        try:
            with open(mine, "rb") as b:
                if b.read() != want:
                    return False
        except FileNotFoundError:
            return False
    return True


def limit_worker_memory():
    """preexec_fn for every driver run: a hard address-space cap."""
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY, WORKER_MEMORY))


def cpu_seconds(pid):
    """utime+stime of the whole process tree under pid (the driver runs under bwrap), all
    threads, plus the children each has reaped."""
    total, stack = 0, [pid]
    while stack:
        p = stack.pop()
        try:
            with open("/proc/%d/stat" % p) as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            total += sum(int(x) for x in fields[11:15])
            for tid in os.listdir("/proc/%d/task" % p):
                with open("/proc/%d/task/%s/children" % (p, tid)) as fh:
                    stack += [int(c) for c in fh.read().split()]
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total / os.sysconf("SC_CLK_TCK")


def run_untimed(mode, timeout=1800):
    """Run a driver mode with no handshake and return its JSON output."""
    r = subprocess.run(SANDBOX + [DRIVER, mode], capture_output=True, text=True, timeout=timeout,
                       preexec_fn=limit_worker_memory, cwd=WORK)
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
    children included) and the timed call over the whole process tree under bwrap, when
    the call is long enough to judge (>0.5 s)."""
    import secrets
    import time
    spawn = time.perf_counter()
    p = subprocess.Popen(SANDBOX + [DRIVER, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1, cwd=WORK,
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
