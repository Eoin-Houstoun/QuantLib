#!/bin/sh
# Build QuantLib and the Heston benchmark driver for the checkout in the current directory.
#
#   sh artemis_bench/setup.sh --seed   # once, by a person, on the unmodified seed:
#                                      # trusted harness copy + pristine seed build
#   sh "$QL_BENCH_DIR/trusted/setup.sh"  # every version (the Script's setup command)
#
# Layout under QL_BENCH_DIR (default ~/.cache/artemis-ql-heston):
#   trusted/   read-only copy of artemis_bench/ made by --seed. The Script runs the setup,
#              gate and benchmark from here, so a version can't edit the harness that judges it.
#   pristine/  read-only snapshot of the seed's sources and full build, with a content
#              manifest whose hash is kept in trusted/pristine.sha256.
#   work/      this version's build. Replaced from pristine/ every time, so nothing one
#              version leaves behind feeds into the next. The paths inside the CMake build
#              always point at work/, so a copy of pristine/ is a valid build tree.
#
# A version's own sources are synced into work/src with rsync --checksum: unchanged files
# keep the seed's timestamps, so only files the version edited recompile, then relink.
# The first (--seed) build is the long one, estimated 45 to 90 min at -j6.
set -eu

BENCH_DIR="${QL_BENCH_DIR:-$HOME/.cache/artemis-ql-heston}"
JOBS="${QL_BENCH_JOBS:-6}"
ROOT=$(pwd)
TRUSTED="$BENCH_DIR/trusted"

need() { command -v "$1" >/dev/null 2>&1 || { echo "setup.sh: $1 not found ($2)" >&2; exit 1; }; }
need cmake "cmake 3.15 or newer"
need c++ "a C++17 compiler (apt install build-essential)"
need rsync "apt install rsync"
need python3 "the gate and benchmark are Python 3 scripts (standard library only)"
need bwrap "bubblewrap sandboxes the build and the runs (apt install bubblewrap)"
[ -f /usr/include/boost/version.hpp ] || [ -n "${BOOST_ROOT:-}" ] || {
    echo "setup.sh: Boost headers not found (apt install libboost-dev, or set BOOST_ROOT)" >&2
    exit 1
}
[ -d "$ROOT/ql" ] && [ -f "$ROOT/CMakeLists.txt" ] || {
    echo "setup.sh: run from the root of a QuantLib checkout" >&2; exit 1; }

manifest() {
    # content hash of every file under $1 (paths relative to it); fails on any unreadable file
    tmp=$(mktemp -d) || return 1
    if (cd "$1" && find . -type f -exec sha256sum {} + > "$tmp/files"); then
        LC_ALL=C sort "$tmp/files" | sha256sum | cut -d' ' -f1
        rm -rf "$tmp"
    else
        rm -rf "$tmp"; echo "setup.sh: could not hash $1" >&2; return 1
    fi
}

sync_sources() {
    rsync -a --checksum --delete --exclude .git --exclude artemis_results.json \
        "$ROOT/" "$BENCH_DIR/work/src/"
}

# The version's code is compiled (and CMake run) in a sandbox where only work/ is writable,
# with no network, its own PID/IPC/user namespaces, /run and the rest of $HOME hidden: it
# can't reach trusted/, pristine/, host sockets or other processes. System cmake and compiler only.
sandbox() {
    bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run --tmpfs "$HOME" \
        --bind "$BENCH_DIR/work" "$BENCH_DIR/work" --unshare-all --new-session --die-with-parent \
        --cap-drop ALL --clearenv --setenv PATH /usr/local/bin:/usr/bin:/bin \
        --setenv HOME "$BENCH_DIR/work" --chdir "$BENCH_DIR/work" "$@"
}

build() {
    sandbox cmake --build "$BENCH_DIR/work/build" --target ql_library ql_test_suite -j "$JOBS"
    lib=$(find "$BENCH_DIR/work/build/ql" -maxdepth 1 -name 'libQuantLib.so*' -type f | head -1)
    [ -n "$lib" ] || { echo "setup.sh: libQuantLib.so not found after the build" >&2; exit 1; }
    # The driver comes from the trusted copy, never from the version's checkout.
    sandbox c++ -O2 -std=c++17 -I "$BENCH_DIR/work/src" -I "$BENCH_DIR/work/build" \
        "$TRUSTED/driver.cpp" -L "$BENCH_DIR/work/build/ql" -lQuantLib \
        -Wl,-rpath,"$BENCH_DIR/work/build/ql" -o "$BENCH_DIR/work/build/heston_driver"
    # What was built, for gate.py and bench.py to check against the checkout they run in.
    python3 -I -c "import sys; sys.path.insert(0, sys.argv[1]); import harness; print(harness.build_stamp())" \
        "$TRUSTED" > "$BENCH_DIR/work/built-from"
}

mkdir -p "$BENCH_DIR"
exec 9>"$BENCH_DIR/.lock"
flock 9

if [ "${1:-}" = "--seed" ]; then
    [ ! -e "$TRUSTED" ] || { echo "setup.sh: $TRUSTED exists; remove $BENCH_DIR to reseed" >&2; exit 1; }
    mkdir -p "$TRUSTED"
    cp -a "$ROOT/artemis_bench/." "$TRUSTED/"
    mkdir -p "$BENCH_DIR/work/src"
    sync_sources
    mkdir -p "$BENCH_DIR/work/build"
    sandbox cmake -S "$BENCH_DIR/work/src" -B "$BENCH_DIR/work/build" -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON -DQL_BUILD_EXAMPLES=OFF -DQL_BUILD_TEST_SUITE=ON \
        -DQL_BUILD_FUZZ_TEST_SUITE=OFF -DCMAKE_CXX_STANDARD=17
    build
    # Golden outputs from the seed, written into the trusted copy before it is locked.
    python3 -I "$TRUSTED/gate.py" --record
    rm -rf "$BENCH_DIR/pristine"
    cp -a "$BENCH_DIR/work" "$BENCH_DIR/pristine"
    find "$BENCH_DIR/pristine" ! -type l -exec chmod a-w {} +
    manifest "$BENCH_DIR/pristine" > "$TRUSTED/pristine.sha256"
    find "$TRUSTED" ! -type l -exec chmod a-w {} +
    echo "setup.sh: seed built; trusted harness in $TRUSTED"
else
    [ -f "$TRUSTED/pristine.sha256" ] || { echo "setup.sh: no seed build; run --seed first" >&2; exit 1; }
    [ "$(manifest "$BENCH_DIR/pristine")" = "$(cat "$TRUSTED/pristine.sha256")" ] || {
        echo "setup.sh: the pristine seed build changed since it was made; refusing" >&2; exit 1; }
    if [ -d "$BENCH_DIR/work" ]; then chmod -R u+w "$BENCH_DIR/work"; rm -rf "$BENCH_DIR/work"; fi
    cp -a "$BENCH_DIR/pristine" "$BENCH_DIR/work"
    chmod -R u+w "$BENCH_DIR/work"
    sync_sources
    build
fi

echo "setup.sh: built $(cat "$BENCH_DIR/work/built-from")"
