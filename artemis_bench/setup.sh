#!/bin/sh
# Build QuantLib and the Heston benchmark driver for this checkout (Artemis setup command).
#
# A runner checks out every version fresh, and a full QuantLib build takes most of an hour,
# so the build lives in a cached folder outside the checkout (QL_BENCH_DIR, default
# ~/.cache/artemis-ql-heston). This checkout is synced into it with rsync --checksum:
# unchanged files keep their timestamps, so only files a version edited recompile.
# The first run is the long one (estimated 45 to 90 min at -j6); later runs rebuild a
# handful of files and relink. Afterwards it records a hash of the sources it built, which
# gate.py and bench.py check before trusting the driver.
#
# Run it as the runner user. One build at a time (flock).
set -eu

BENCH_DIR="${QL_BENCH_DIR:-$HOME/.cache/artemis-ql-heston}"
JOBS="${QL_BENCH_JOBS:-6}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")

need() { command -v "$1" >/dev/null 2>&1 || { echo "setup.sh: $1 not found ($2)" >&2; exit 1; }; }
need cmake "cmake 3.15 or newer"
need c++ "a C++17 compiler (apt install build-essential)"
need rsync "apt install rsync"
need python3 "the gate and benchmark are Python 3 scripts (standard library only)"
[ -f /usr/include/boost/version.hpp ] || [ -n "${BOOST_ROOT:-}" ] || {
    echo "setup.sh: Boost headers not found (apt install libboost-dev, or set BOOST_ROOT)" >&2
    exit 1
}

mkdir -p "$BENCH_DIR"
exec 9>"$BENCH_DIR/.lock"
flock 9

# Sync the sources; --checksum keeps the timestamps of unchanged files.
rsync -a --checksum --delete --exclude .git --exclude artemis_results.json \
    "$ROOT/" "$BENCH_DIR/src/"
rm -f "$BENCH_DIR/built-from"

if [ ! -f "$BENCH_DIR/build/CMakeCache.txt" ]; then
    cmake -S "$BENCH_DIR/src" -B "$BENCH_DIR/build" -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON -DQL_BUILD_EXAMPLES=OFF -DQL_BUILD_TEST_SUITE=ON \
        -DQL_BUILD_FUZZ_TEST_SUITE=OFF -DCMAKE_CXX_STANDARD=17
fi
cmake --build "$BENCH_DIR/build" --target ql_library ql_test_suite -j "$JOBS"

LIB=$(find "$BENCH_DIR/build/ql" -maxdepth 1 -name 'libQuantLib.so*' -type f | head -1)
[ -n "$LIB" ] || { echo "setup.sh: libQuantLib.so not found after the build" >&2; exit 1; }
c++ -O2 -std=c++17 -I "$BENCH_DIR/src" -I "$BENCH_DIR/build" \
    "$BENCH_DIR/src/artemis_bench/driver.cpp" -L "$BENCH_DIR/build/ql" -lQuantLib \
    -Wl,-rpath,"$BENCH_DIR/build/ql" -o "$BENCH_DIR/build/heston_driver"

# What was built, for gate.py and bench.py to check against the checkout they run in.
python3 -I -c "import sys; sys.path.insert(0, sys.argv[1]); import harness; print(harness.source_hash())" \
    "$HERE" > "$BENCH_DIR/built-from"
echo "setup.sh: built $(cat "$BENCH_DIR/built-from") into $BENCH_DIR"
