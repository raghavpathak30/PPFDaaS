#!/usr/bin/env python3
"""Same-session laptop comparison: FV-slot baseline vs CNP-B vs HalfBoot lower bound (HERA and Rubato).

  python3 scripts/bfv_slot_baseline/run_laptop_compare.py --data <prepare_inputs.py dir> [--logn 10 12] [--reps 5]

Refuses to measure on battery and writes a STOP record instead. Refuses LogN > 12 (this host has 15.99 GB and a
full-scale run has OOM-killed the shell). Sets GOMEMLIMIT=11GiB and kills any arm whose RSS reaches 11 GB.
Writes under artifacts/bfv_slot_baseline/laptop_<UTC>/ and never reuses a directory.

  --compare-only <dir>   rebuild comparison.json / comparison.md from an existing run directory.

Arms, one process each, in this order per (LogN, cipher): slot, cnp, halfboot (CNP_ARMS="cnp baseline": the
existing harness only runs the baseline after CNP), slot_repeat (detects drift inside the session). The CNP and
HalfBoot code paths are the existing TestCNPRun, unmodified.
"""
import argparse
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATCH_DIR = REPO / "tools/transciphering/toy_correctness/testdata/ckks_fv_patch"
RTF_DIR = REPO / "third_party/RtF-Transciphering/ckks_fv"
OUT_ROOT = REPO / "artifacts/bfv_slot_baseline"
CIPHERS = ("hera", "rubato")
MAX_LOGN = 12
RSS_LIMIT_KB = 11_000_000  # 11 GB, decimal: the stricter reading of the 11 GB limit
MEM_AVAILABLE_FLOOR_KB = 1_000_000
ARMS = ("slot", "cnp", "halfboot", "slot_repeat")


class StopRun(Exception):
    """A stop condition from the brief. The message goes into the STOP record."""


# ---- machine state ----------------------------------------------------------------------------------


def read_power(root="/sys/class/power_supply"):
    """Same rule as MachineState (tools/transciphering/bench/main.go): any online=1 supply means AC.

    Otherwise a discharging battery means battery. Unknown is NOT treated as AC: the caller stops on it.
    """
    root = Path(root)
    online, statuses = {}, {}
    if root.is_dir():
        for d in sorted(root.iterdir()):
            f = d / "online"
            if f.exists():
                online[d.name] = f.read_text().strip()
            f = d / "status"
            if f.exists():
                statuses[d.name] = f.read_text().strip()
    if any(v == "1" for v in online.values()):
        source = "AC"
    elif "Discharging" in statuses.values():
        source = "battery"
    else:
        source = "unknown"
    return {"power_source": source, "online": online, "battery_status": statuses}


def machine_state():
    gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    mhz = []
    for p in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")):
        try:
            mhz.append(int(p.read_text()) / 1000)
        except OSError:
            pass
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    return {
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu_governor": gov.read_text().strip() if gov.exists() else "unknown",
        **read_power(),
        "load_avg_1_5_15": list(os.getloadavg()),
        "cpu_count": os.cpu_count(),
        "scaling_cur_mhz_min": min(mhz) if mhz else None,
        "scaling_cur_mhz_max": max(mhz) if mhz else None,
        "mem_total_kb": mem.get("MemTotal"),
        "mem_available_kb": mem.get("MemAvailable"),
        "swap_free_kb": mem.get("SwapFree"),
        "kernel": platform.release(),
    }


def require_ac(where):
    p = read_power()
    if p["power_source"] != "AC":
        raise StopRun(f"power source is {p['power_source']!r} at {where}: {p}")


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


# ---- running one arm ----------------------------------------------------------------------------------


def arm_env(arm, cipher, logn, reps, data, rows, results):
    env = dict(os.environ)
    for k in ("CNP_ARMS", "CNP_STC_MODE", "CNP_SCALEUP", "SLOT_EVAL_LEVEL", "CNP_HEADROOM_GATE_BITS", "E2E_OUT"):
        env.pop(k, None)  # defaults only: no loosened gate or changed mode can leak in from the shell
    env.update(E2E_RUN="1", E2E_CIPHER=cipher, E2E_LOGN=str(logn), E2E_REPS=str(reps), CNP_DATA_DIR=str(data),
               CNP_ROWS=rows, RESULTS_DIR=str(results), GOMEMLIMIT="11GiB",
               PPFDAAS_REPO_SHA=git("rev-parse", "HEAD"))
    if arm == "cnp":
        env["CNP_ARMS"] = "cnp"
    elif arm == "halfboot":
        env["CNP_ARMS"] = "cnp baseline"
        env["CNP_ROWS"] = "16"  # timing comes from the stages; its AUC is not used (the 2x+1 circuit is not the LR)
    return env


def run_arm(binary, arm, cipher, logn, reps, data, rows, results):
    results.mkdir(parents=True, exist_ok=False)
    require_ac(f"start of {arm} {cipher} LogN {logn}")
    (results / "machine_state_start.json").write_text(json.dumps(machine_state(), indent=1))
    test = "^TestSlotRun$" if arm.startswith("slot") else "^TestCNPRun$"
    log = (results / "test.log").open("w")
    proc = subprocess.Popen([str(binary), "-test.count=1", "-test.v", "-test.timeout", "6h", "-test.run", test],
                            cwd=results, env=arm_env(arm, cipher, logn, reps, data, rows, results),
                            stdout=log, stderr=subprocess.STDOUT)
    status = {"peak_rss_kb": 0, "killed": None, "lost_ac": False}

    def watch():
        pth = Path(f"/proc/{proc.pid}/status")
        while proc.poll() is None:
            try:
                rss = 0
                for line in pth.read_text().splitlines():
                    if line.startswith("VmHWM:"):
                        status["peak_rss_kb"] = max(status["peak_rss_kb"], int(line.split()[1]))
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                avail = 0
                for line in Path("/proc/meminfo").read_text().splitlines():
                    if line.startswith("MemAvailable:"):
                        avail = int(line.split()[1])
                if rss >= RSS_LIMIT_KB or avail < MEM_AVAILABLE_FLOOR_KB:
                    status["killed"] = f"rss_kb={rss} mem_available_kb={avail}"
                    proc.kill()
                if read_power()["power_source"] != "AC":
                    status["lost_ac"] = True
                    status["killed"] = "AC lost during the arm"
                    proc.kill()
            except OSError:
                pass
            time.sleep(0.5)

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    rc = proc.wait()
    th.join()
    log.close()
    (results / "machine_state_end.json").write_text(json.dumps(machine_state(), indent=1))
    status["exit_code"] = rc
    (results / "run_status.json").write_text(json.dumps(status, indent=1))
    if status["lost_ac"]:
        raise StopRun(f"AC power lost during {arm} {cipher} LogN {logn}")
    if status["killed"]:
        raise MemoryError(f"{arm} {cipher} LogN {logn} killed: {status['killed']}")
    if rc != 0:
        raise StopRun(f"{arm} {cipher} LogN {logn} exited {rc}: see {results / 'test.log'}")
    return status


def score(data, results, cipher, arm):
    subprocess.run([sys.executable, str(REPO / "scripts/cnp_b/score.py"), "--data", str(data), "--results", str(results),
                    "--cipher", cipher, "--arm", arm], check=True, capture_output=True)


# ---- comparison ----------------------------------------------------------------------------------------


def load(path):
    return json.loads(Path(path).read_text())


def cnp_timing(d):
    s = d["cnp_stages"]
    crypt, stc = s["keystream_crypt (offline)"]["median_ms"], s["stc_modswitch (offline)"]["median_ms"]
    outs, lanes = d["outputs_per_crypt"], d["lanes_per_output_ct"]
    return {"crypt_ms": crypt, "stc_ms": stc, "offline_per_ct_ms": crypt / outs + stc, "outputs_per_crypt": outs, "lanes_per_ct": lanes}


def arm_row(arm, d, sc, key_bytes_each, keys):
    """One comparison row. d = the arm's JSON, sc = its score JSON or None."""
    r = {"arm": arm, "peak_rss_kb": d.get("vm_hwm_kb_end"), "rotation_keys": keys, "rotation_key_bytes": keys * key_bytes_each}
    if arm == "slot":
        r.update(offline_per_ct_ms=d["per_keystream_ct_ms"]["offline"], online_per_ct_ms=d["per_keystream_ct_ms"]["online"],
                 offline_per_txn_ms=d["per_transaction_ms"]["offline"], online_per_txn_ms=d["per_transaction_ms"]["online"],
                 total_per_txn_ms=d["per_transaction_ms"]["total"], lanes_per_output_ct=d["lanes_per_output_ct_mean"],
                 output_cts_per_crypt=d["output_cts_per_crypt"], lanes_per_crypt=d["lanes_per_crypt"],
                 max_abs_integer_diff=d["correctness"].get("max_abs_integer_diff"), bit_exact=d["correctness"].get("bit_exact"),
                 headroom_gate=d["headroom_gate"])
    else:
        t = cnp_timing(d)
        online = d["per_output_ct_ms"]["total"] if arm == "cnp" else d["baseline_lower_bound_per_output_ct_ms"]["total"]
        lanes = t["lanes_per_ct"]
        r.update(offline_per_ct_ms=t["offline_per_ct_ms"], online_per_ct_ms=online, offline_per_txn_ms=t["offline_per_ct_ms"] / lanes,
                 online_per_txn_ms=online / lanes, total_per_txn_ms=(t["offline_per_ct_ms"] + online) / lanes, lanes_per_output_ct=lanes,
                 output_cts_per_crypt=t["outputs_per_crypt"], lanes_per_crypt=t["outputs_per_crypt"] * lanes)
        if arm == "cnp":
            r.update(max_abs_integer_diff=d["correctness"].get("max_abs_integer_diff"), bit_exact=d["correctness"].get("bit_exact"),
                     headroom_gate=d["headroom_gate"])
        else:
            r["max_abs_error_ckks_2x_plus_1"] = d["baseline_halfboot_stages"]["_max_abs_error_check"]["median_ms"]
    if sc:
        r.update(rows=sc["rows"], auc_encrypted=sc["roc_auc_encrypted"], auc_plaintext=sc["roc_auc_plaintext_float"],
                 delta_auc=sc["delta_roc_auc"], max_abs_logit_error=sc["max_abs_logit_error"],
                 bit_exact_vs_expected_i64=sc["bit_exact_vs_integer_simulation"])
    return r


def ratio(a, b):
    return a / b if b else math.inf


def compare_cell(cell_dir):
    cell = Path(cell_dir)
    cipher = cell.name
    slot = load(cell / "slot" / f"slot_{cipher}.json")
    cnp = load(cell / "cnp" / f"cnp_{cipher}.json")
    hb = load(cell / "halfboot" / f"cnp_{cipher}.json")
    rep = load(cell / "slot_repeat" / f"slot_{cipher}.json")
    each = slot["rotation_key_bytes_allocated"] / slot["rotation_key_counts"]["slot_rotations"]
    sc = {a: load(cell / a / f"{a}_{cipher}_score.json") for a in ("slot", "cnp")}
    rows = {
        "slot": arm_row("slot", slot, sc["slot"], each, slot["rotation_key_counts"]["slot_rotations"]),
        "cnp": arm_row("cnp", cnp, sc["cnp"], each, cnp["rotation_key_counts"]["stc_only"]),
        "halfboot": arm_row("halfboot", hb, None, each, hb["rotation_key_counts"]["stc_only"] + hb["rotation_key_counts"]["baseline_halfboot_extra"]),
    }
    a, b = rows["cnp"], rows["slot"]
    drift = ratio(rep["per_transaction_ms"]["total"], slot["per_transaction_ms"]["total"])
    return {
        "cipher": cipher, "log_n": slot["param_state"]["log_n"], "n": slot["n"], "t": slot["plain_modulus"],
        "arms": rows,
        "stages_slot_per_batch_median_ms": {k: v["median_ms"] for k, v in slot["slot_stages_per_crypt_batch"].items()},
        "stages_cnp_median_ms": {k: v["median_ms"] for k, v in cnp["cnp_stages"].items()},
        "ratios_cnp_over_slot_gt1_means_slot_faster": {
            "online_per_txn": ratio(a["online_per_txn_ms"], b["online_per_txn_ms"]),
            "offline_per_txn": ratio(a["offline_per_txn_ms"], b["offline_per_txn_ms"]),
            "total_per_txn": ratio(a["total_per_txn_ms"], b["total_per_txn_ms"])},
        "ratios_halfboot_over_slot": {"total_per_txn": ratio(rows["halfboot"]["total_per_txn_ms"], b["total_per_txn_ms"])},
        "ratios_halfboot_over_cnp": {"online_per_txn": ratio(rows["halfboot"]["online_per_txn_ms"], a["online_per_txn_ms"]),
                                     "total_per_txn": ratio(rows["halfboot"]["total_per_txn_ms"], a["total_per_txn_ms"])},
        "session_drift_slot_repeat_over_slot_total": drift,
        "sources": {k: str((cell / d / f).relative_to(REPO)) if (cell / d / f).resolve().is_relative_to(REPO) else str(cell / d / f)
                    for k, d, f in (("slot", "slot", f"slot_{cipher}.json"), ("cnp", "cnp", f"cnp_{cipher}.json"),
                                    ("halfboot", "halfboot", f"cnp_{cipher}.json"), ("slot_repeat", "slot_repeat", f"slot_{cipher}.json"),
                                    ("slot_score", "slot", f"slot_{cipher}_score.json"), ("cnp_score", "cnp", f"cnp_{cipher}_score.json"))},
    }


def comparison_md(cells):
    lines = ["# Same-session comparison (generated by run_laptop_compare.py)", ""]
    for c in cells:
        lines += [f"## LogN {c['log_n']}, {c['cipher']} (N={c['n']})", "",
                  "| Arm | Offline ms/ct | Online ms/ct | Offline ms/txn | Online ms/txn | Total ms/txn | Lanes/output ct | Output cts/Crypt | Galois keys | Key bytes | Peak RSS KB | Max int err | Delta AUC |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for a in ("slot", "cnp", "halfboot"):
            r = c["arms"][a]
            lines.append(f"| {a} | {r['offline_per_ct_ms']:.3f} | {r['online_per_ct_ms']:.3f} | {r['offline_per_txn_ms']:.4f} | {r['online_per_txn_ms']:.5f} | "
                         f"{r['total_per_txn_ms']:.4f} | {r['lanes_per_output_ct']:.1f} | {r['output_cts_per_crypt']} | {r['rotation_keys']} | {int(r['rotation_key_bytes'])} | "
                         f"{r['peak_rss_kb']} | {r.get('max_abs_integer_diff', 'n/a')} | {r.get('delta_auc', 'n/a')} |")
        rr = c["ratios_cnp_over_slot_gt1_means_slot_faster"]
        lines += ["", f"CNP / slot (above 1 means slot is faster): online {rr['online_per_txn']:.3f}, offline {rr['offline_per_txn']:.3f}, total {rr['total_per_txn']:.3f}. "
                  f"Slot repeat / slot total: {c['session_drift_slot_repeat_over_slot_total']:.3f}.", ""]
    return "\n".join(lines)


def write_comparison(out_dir, data=None):
    out_dir = Path(out_dir)
    cells = []
    for d in sorted(out_dir.glob("logn*/*")):
        if d.is_dir() and d.name in CIPHERS and all((d / a / "run_status.json").exists() for a in ARMS):
            cells.append(compare_cell(d))
    (out_dir / "comparison.json").write_text(json.dumps(cells, indent=1))
    (out_dir / "comparison.md").write_text(comparison_md(cells))
    return cells


# ---- main ------------------------------------------------------------------------------------------------


def stop_record(reason):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = OUT_ROOT / f"STOPPED_{stamp}.json"
    p.write_text(json.dumps({"stopped": reason, "machine_state": machine_state(), "repo_sha": git("rev-parse", "HEAD")}, indent=1))
    return p


def check_logn(logns):
    for n in logns:
        if n > MAX_LOGN:
            raise SystemExit(f"LogN {n} refused: this host must never run above LogN {MAX_LOGN} (15.99 GB; a full-scale run OOM-killed the shell)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--logn", type=int, nargs="+", default=[10, 12])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--rows", default="strat", help="CNP_ROWS for the slot and cnp arms")
    ap.add_argument("--compare-only")
    a = ap.parse_args(argv)
    if a.compare_only:
        write_comparison(a.compare_only)
        return 0
    if not a.data:
        ap.error("--data is required")
    check_logn(a.logn)
    data = Path(a.data).resolve()
    if not (data / "meta.json").exists():
        ap.error(f"{data} has no meta.json (run scripts/cnp_b/prepare_inputs.py)")

    try:
        require_ac("session start")
    except StopRun as e:
        p = stop_record(str(e))
        print(f"STOP: {e}\nNothing was measured. Record: {p}", file=sys.stderr)
        return 3

    if git("status", "--porcelain", "--", "tools", "scripts", "third_party/patches"):
        raise SystemExit("working tree has changes under tools/, scripts/ or third_party/patches/: commit first so the JSON's repo_sha is meaningful")
    for f in sorted(PATCH_DIR.glob("*.go")):
        if f.read_bytes() != (RTF_DIR / f.name).read_bytes():
            raise SystemExit(f"{f.name}: tracked harness differs from the staged copy; run third_party/fetch_rtf.sh or copy it")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_ROOT / f"laptop_{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    binary = out / "ckks_fv.test"
    subprocess.run(["go", "test", "-c", "-o", str(binary), "."], cwd=RTF_DIR, check=True)
    session = {"started": machine_state(), "repo_sha": git("rev-parse", "HEAD"), "reps": a.reps, "rows": a.rows, "logn": a.logn,
               "gomemlimit": "11GiB", "rss_limit_kb": RSS_LIMIT_KB, "data": str(data), "go_version": subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip()}
    (out / "session.json").write_text(json.dumps(session, indent=1))
    rc = 0
    try:
        for logn in a.logn:
            for cipher in CIPHERS:
                for arm in ARMS:
                    res = out / f"logn{logn}" / cipher / arm
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] LogN {logn} {cipher} {arm}", flush=True)
                    run_arm(binary, arm, cipher, logn, a.reps, data, a.rows, res)
                    if arm in ("slot", "cnp"):
                        score(data, res, cipher, arm)
                    elif arm == "slot_repeat":
                        score(data, res, cipher, "slot")
    except StopRun as e:
        session["stopped"] = str(e)
        rc = 3
        print(f"STOP: {e}", file=sys.stderr)
    except MemoryError as e:
        session["memory_limit_hit"] = str(e)
        rc = 4
        print(f"Memory limit: {e}. LogN above this was not attempted.", file=sys.stderr)
    session["ended"] = machine_state()
    (out / "session.json").write_text(json.dumps(session, indent=1))
    binary.unlink(missing_ok=True)
    write_comparison(out)
    print(f"Results in {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
