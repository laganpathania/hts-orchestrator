#!/usr/bin/env bash
# Re-records the real command output shown on the demo page. Run from the project folder.
set -u
export PYTHONPATH=src
OUT=demo/capture
W=$(mktemp -d)
mkdir -p "$OUT"
run() { name=$1; shift; { python -m htsorch "$@" 2>&1; echo "[exit $?]"; } | sed "s#$W#out#g" > "$OUT/$name.txt"; }
run validate    validate specs/ctg3d_screen.yaml
run lost_plate  run specs/ctg3d_screen.yaml --out "$W/a" --no-simulate --animl off --scenario scenarios/lost_plate.yaml
run nobody      run specs/ctg3d_screen.yaml --out "$W/b" --no-simulate --animl off --scenario scenarios/nobody_there.yaml
run normal      run specs/ctg3d_screen.yaml --out "$W/n"
run low_tips    run specs/ctg3d_screen.yaml --out "$W/t" --scenario scenarios/low_tips.yaml
run scenarios   scenarios
run qualify     qualify --out "$W/q"
run identify    identify --out "$W/i"
rm -rf "$W"
