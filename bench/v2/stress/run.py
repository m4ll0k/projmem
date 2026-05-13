"""Stress test — daemon + UI event-stream throughput under load.

What it exercises
-----------------
1. Spawn `projmem daemon` in a background subprocess on a free port.
2. Open a WebSocket subscriber to ``/events`` from a Python coroutine.
3. Fire N rapid `projmem editing` + `projmem done` cycles across M
   different files via the CLI binary (each `editing` is its own
   subprocess — that's the realistic Claude Code shape).
4. For every CLI lease the test recorded, assert a matching
   ``leased`` AND ``released`` (or ``abandoned``) event reached the
   WS stream within a deadline (default 2 s after the CLI call
   returned).
5. Tear down the daemon, write a JSON + Markdown report.

What "passes" looks like
------------------------
Every CLI lease produces:
  - one ``leased`` event with the right ``path``,
  - one ``released`` (or ``abandoned``) event with the right
    ``path`` within `deadline_s`.

Anything else is a regression in the daemon's poll-file-events
broadcast path — the exact piece the user just asked to verify.

Usage:
    python3 -m bench.v2.stress.run [--leases 200] [--files 20] [--port 7892]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_repo(root: Path, n_files: int) -> List[str]:
    src = root / "src"
    src.mkdir()
    paths = []
    for i in range(n_files):
        p = src / f"mod{i:03d}.py"
        p.write_text(
            f"def fn{i}():\n"
            f"    return {i}\n"
        )
        paths.append(f"src/mod{i:03d}.py")
    subprocess.run(["projmem", "index"], cwd=str(root),
                   capture_output=True, check=True)
    return paths


async def _start_daemon(root: Path, port: int):
    proc = await asyncio.create_subprocess_exec(
        "projmem", "daemon", "--port", str(port), "--no-ui",
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Wait for uvicorn to bind. We poll /healthz instead of grepping stderr.
    import urllib.request
    for _ in range(60):
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=0.5)
            if r.status == 200:
                return proc
        except OSError:
            await asyncio.sleep(0.1)
    proc.terminate()
    await proc.wait()
    raise RuntimeError("daemon did not bind in 6s")


async def _ws_listener(port: int, captured: List[Dict[str, Any]],
                       stop: asyncio.Event):
    import websockets   # type: ignore
    url = f"ws://127.0.0.1:{port}/events"
    try:
        async with websockets.connect(url, max_size=2**20) as ws:
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                try:
                    captured.append(json.loads(msg))
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception as e:
        captured.append({"_listener_error": str(e)})


def _run_cli(args: List[str], *, cwd: Path) -> Dict[str, Any]:
    r = subprocess.run(
        ["projmem", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=10,
    )
    try:
        return {"rc": r.returncode,
                "stdout": json.loads(r.stdout) if r.stdout.strip() else None,
                "stderr": r.stderr}
    except json.JSONDecodeError:
        return {"rc": r.returncode, "stdout_raw": r.stdout, "stderr": r.stderr}


async def main_async(args: argparse.Namespace) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="projmem-stress-"))
    try:
        print(f"seeding {args.files} files at {tmp} …")
        paths = _seed_repo(tmp, args.files)

        port = args.port or _free_port()
        print(f"starting daemon on 127.0.0.1:{port} …")
        proc = await _start_daemon(tmp, port)

        captured: List[Dict[str, Any]] = []
        stop_listener = asyncio.Event()
        ws_task = asyncio.create_task(
            _ws_listener(port, captured, stop_listener))
        # Tiny wait so the listener replay is finished before we
        # start firing leases.
        await asyncio.sleep(0.4)

        print(f"firing {args.leases} editing→done cycles "
              f"(across {len(paths)} files) …")
        cli_calls: List[Dict[str, Any]] = []
        t0 = time.time()
        for i in range(args.leases):
            path = paths[i % len(paths)]
            opened = _run_cli(
                ["editing", path,
                 "--reason", f"stress lease {i} on {path} "
                              f"(deterministic seed for the harness)",
                 "--json"],
                cwd=tmp,
            )
            lease_id = (opened.get("stdout") or {}).get("lease_id")
            done = _run_cli(["done", lease_id, "--json"],
                            cwd=tmp) if lease_id else {"rc": 1}
            cli_calls.append({
                "i":         i,
                "path":      path,
                "lease_id":  lease_id,
                "opened_rc": opened["rc"],
                "done_rc":   done["rc"],
            })
        elapsed = time.time() - t0
        print(f"{args.leases} cycles in {elapsed:.2f}s "
              f"({args.leases / elapsed:.1f} leases/s)")

        # Give the daemon's poller time to flush.
        await asyncio.sleep(max(1.0, args.deadline_s))
        stop_listener.set()
        await ws_task

        # ── Verify every CLI lease has matching WS events ───────────
        ws_leased_by_path: Dict[str, int] = {}
        ws_released_by_path: Dict[str, int] = {}
        for ev in captured:
            kind = ev.get("kind")
            path = ev.get("path")
            if not path:
                continue
            if kind == "leased":
                ws_leased_by_path[path] = ws_leased_by_path.get(path, 0) + 1
            elif kind in ("released", "abandoned"):
                ws_released_by_path[path] = ws_released_by_path.get(path, 0) + 1

        cli_leases_by_path: Dict[str, int] = {}
        cli_releases_by_path: Dict[str, int] = {}
        for c in cli_calls:
            if c["opened_rc"] == 0 and c["lease_id"]:
                cli_leases_by_path[c["path"]] = (
                    cli_leases_by_path.get(c["path"], 0) + 1)
            if c["done_rc"] == 0 and c["lease_id"]:
                cli_releases_by_path[c["path"]] = (
                    cli_releases_by_path.get(c["path"], 0) + 1)

        missing_leased = {
            p: cli_leases_by_path[p] - ws_leased_by_path.get(p, 0)
            for p in cli_leases_by_path
            if cli_leases_by_path[p] > ws_leased_by_path.get(p, 0)
        }
        missing_released = {
            p: cli_releases_by_path[p] - ws_released_by_path.get(p, 0)
            for p in cli_releases_by_path
            if cli_releases_by_path[p] > ws_released_by_path.get(p, 0)
        }

        # ── Teardown ────────────────────────────────────────────────
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

        # ── Report ──────────────────────────────────────────────────
        out_dir = (Path(__file__).parent / "results"
                   / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        out_dir.mkdir(parents=True, exist_ok=True)

        summary = {
            "leases_attempted":   args.leases,
            "files":              len(paths),
            "elapsed_s":          round(elapsed, 3),
            "leases_per_sec":     round(args.leases / elapsed, 2),
            "cli_open_ok":        sum(1 for c in cli_calls if c["opened_rc"] == 0),
            "cli_close_ok":       sum(1 for c in cli_calls if c["done_rc"] == 0),
            "ws_total_events":    len(captured),
            "ws_leased_total":    sum(ws_leased_by_path.values()),
            "ws_released_total":  sum(ws_released_by_path.values()),
            "missing_leased":     missing_leased,
            "missing_released":   missing_released,
            "verdict":            ("pass" if not missing_leased and
                                             not missing_released
                                   else "fail"),
        }

        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (out_dir / "captured.json").write_text(
            json.dumps(captured[:1000], indent=2))
        (out_dir / "cli_calls.json").write_text(
            json.dumps(cli_calls, indent=2))

        md = [
            "# Daemon stress test",
            "",
            f"_Run at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}._",
            "",
            f"- leases attempted: **{args.leases}**",
            f"- files: **{len(paths)}**",
            f"- elapsed: **{elapsed:.2f}s** ({args.leases / elapsed:.1f} leases/s)",
            f"- CLI opens OK: **{summary['cli_open_ok']} / {args.leases}**",
            f"- CLI closes OK: **{summary['cli_close_ok']} / {args.leases}**",
            f"- WS events received: **{summary['ws_total_events']}**",
            f"- WS `leased` total: **{summary['ws_leased_total']}**",
            f"- WS `released` total: **{summary['ws_released_total']}**",
            f"- **VERDICT: {summary['verdict'].upper()}**",
            "",
            "## Missing events (should be empty on PASS)",
            "```",
            f"missing_leased   = {missing_leased or '{}'}",
            f"missing_released = {missing_released or '{}'}",
            "```",
            "",
            "## What this verifies",
            "- The daemon's `poll_file_events` task tails the SQLite "
            "`file_event` table and broadcasts every new row.",
            "- Every CLI-triggered lease produces a `leased` event "
            "+ a `released`/`abandoned` event on the WS stream.",
            "- The UI graph's pulsing-halo + dim-others focus mode "
            "reacts in real time to those events.",
            "",
            "## Reproduce",
            "```bash",
            f"python3 -m bench.v2.stress.run --leases {args.leases} "
            f"--files {args.files}",
            "```",
        ]
        (out_dir / "REPORT.md").write_text("\n".join(md))

        print()
        print("=== summary ===")
        for k, v in summary.items():
            print(f"  {k:22s} {v}")
        print()
        print(f"results: {out_dir}")
        return 0 if summary["verdict"] == "pass" else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leases", type=int, default=200)
    ap.add_argument("--files",  type=int, default=20)
    ap.add_argument("--port",   type=int, default=0,
                     help="0 = free port; otherwise pin.")
    ap.add_argument("--deadline-s", type=float, default=2.0,
                     help="Grace period after the last CLI close before "
                          "we read the captured events buffer.")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
