#!/usr/bin/env bash
# Capture real tool outputs for the tokcost benchmark.
# Runs each tool, dumps raw stdout to artifacts/<scenario>/<tool>.out.
set -u
REPO="${REPO:-/tmp/flask}"
OUT="$(cd "$(dirname "$0")" && pwd)/artifacts"
mkdir -p "$OUT"

run() {
    local scenario="$1" tool="$2"; shift 2
    local dir="$OUT/$scenario"
    mkdir -p "$dir"
    ( cd "$REPO" && "$@" ) >"$dir/$tool.out" 2>&1
    local bytes; bytes=$(wc -c < "$dir/$tool.out")
    printf "  %-30s %-10s bytes=%8d\n" "$scenario" "$tool" "$bytes"
}

echo "== S1 find usages of setupmethod =="
run s1_setupmethod grep            grep -rn  "setupmethod" src/
run s1_setupmethod rg              rg   -n   "setupmethod" src/
run s1_setupmethod grep_l          grep -rln "setupmethod" src/
run s1_setupmethod projmem_symbol  projmem symbol setupmethod
run s1_setupmethod projmem_reverse projmem reverse "src/flask/sansio/scaffold.py#setupmethod."

echo "== S2 inspect flask/app.py =="
run s2_read_app    cat             cat src/flask/app.py
run s2_read_app    head200         head -n 200 src/flask/app.py
run s2_read_app    projmem_pack    projmem pack src/flask/app.py

echo "== S3 locate entrypoints of flask =="
run s3_entrypoints find            find src -maxdepth 3 -name "__init__.py"
run s3_entrypoints ls              ls -la src/flask
run s3_entrypoints grep_main       grep -rn "def create_app\|if __name__" src/
run s3_entrypoints projmem_entry   projmem entrypoints

echo "== S4 files list =="
run s4_files       find_all        find src -type f
run s4_files       projmem_files   projmem files

echo "Done. Artifacts in $OUT"
