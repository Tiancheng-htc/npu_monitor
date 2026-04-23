#!/usr/bin/env python3
"""
NPU Memory Holder.

Claims most of the HBM on every Ascend NPU chip on the local machine and
blocks until killed. Run on the target server by the npu_monitor GUI
(transferred over SSH stdin), or manually:

    python3 hold_npu.py --percent 90

Each child process sets its comm to ``NPU_HOLD_phyXX`` so it is easy to
spot in ``npu-smi info`` and in ``ps -ef``. The parent comm is
``NPU_HOLD_run``.

To release everything manually:

    pkill -9 -f NPU_HOLD
    # or
    pkill -9 -f npu_monitor_hold
"""
import argparse
import ctypes
import multiprocessing as mp
import os
import re
import subprocess
import sys
import time


PR_SET_NAME = 15


def set_proc_name(name: str) -> None:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(name.encode()[:15] + b"\x00")
        libc.prctl(PR_SET_NAME, buf, 0, 0, 0)
    except Exception:
        pass


def get_hbm_totals_mb() -> dict:
    try:
        out = subprocess.run(
            ["npu-smi", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        print(f"npu-smi failed: {e}", file=sys.stderr)
        return {}
    if out.returncode != 0:
        print(f"npu-smi exit {out.returncode}: {out.stderr}", file=sys.stderr)
        return {}

    det_re = re.compile(
        r"^\|\s*(\d+)\s+(\d+)\s*\|\s*[\w:.]+\s*\|\s*\d+\s+\d+\s*/\s*\d+\s+\d+\s*/\s*(\d+)\s*\|"
    )
    totals = {}
    for line in out.stdout.splitlines():
        m = det_re.match(line)
        if m:
            phy_id = int(m.group(2))
            totals[phy_id] = int(m.group(3))
    return totals


def holder_proc(phy_id: int, target_mb: int) -> None:
    set_proc_name(f"NPU_HOLD_phy{phy_id}")
    os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(phy_id)
    os.environ["ASCEND_VISIBLE_DEVICES"] = str(phy_id)

    try:
        import torch
        import torch_npu  # noqa: F401
    except Exception as e:
        print(f"[phy{phy_id}] torch/torch_npu import failed: {e}", flush=True)
        sys.exit(2)

    dev = torch.device("npu:0")
    try:
        torch.ones(1, device=dev)
    except Exception as e:
        print(f"[phy{phy_id}] warmup failed: {e}", flush=True)
        sys.exit(3)

    remaining = int(target_mb) * 1024 * 1024
    chunk = 1024 * 1024 * 1024
    bufs = []
    while remaining > 0:
        sz = min(chunk, remaining)
        try:
            bufs.append(torch.empty(sz, dtype=torch.uint8, device=dev))
            remaining -= sz
        except Exception as e:
            print(f"[phy{phy_id}] alloc stopped at "
                  f"{(target_mb * 1024 * 1024 - remaining) // 1024 // 1024} MB: {e}",
                  flush=True)
            break
    held_mb = sum(b.numel() for b in bufs) // 1024 // 1024
    print(f"[phy{phy_id}] holding {held_mb} MB", flush=True)

    while True:
        time.sleep(3600)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--percent", type=float, default=90.0,
                    help="Percent of each chip's HBM to claim (default 90)")
    ap.add_argument("--reserve-mb", type=int, default=2048,
                    help="Per-chip MB to leave unused regardless of --percent")
    args = ap.parse_args()

    set_proc_name("NPU_HOLD_run")

    totals = get_hbm_totals_mb()
    if not totals:
        print("No NPU chips detected via npu-smi.", file=sys.stderr)
        sys.exit(1)

    print(f"Detected {len(totals)} NPU chip(s):")
    for phy, mb in sorted(totals.items()):
        print(f"  phy{phy}: total={mb} MB")

    ctx = mp.get_context("spawn")
    procs = []
    for phy_id in sorted(totals):
        total_mb = totals[phy_id]
        target = int(total_mb * args.percent / 100.0)
        target = min(target, max(total_mb - args.reserve_mb, 0))
        p = ctx.Process(
            target=holder_proc,
            args=(phy_id, target),
            name=f"NPU_HOLD_phy{phy_id}",
            daemon=False,
        )
        p.start()
        procs.append(p)
        print(f"  started phy{phy_id} pid={p.pid} target={target} MB", flush=True)

    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            p.join(timeout=5)


if __name__ == "__main__":
    main()
