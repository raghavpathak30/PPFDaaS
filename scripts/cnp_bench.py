#!/usr/bin/env python3
"""Strategy x lanes x requests benchmark harness: {fold, bsgs, naive, cnp}.

PREPARED, NOT RUN AT SCALE. This file only produces a measurement protocol and
a --smoke self-check; it makes no performance claim by itself.

Every row carries a `scope` (never silently mixed):
  server_compute_only  ALL FOUR strategies, measured the same way by
                 vendor_server/build/benchmark_160 --strategy=fold|bsgs|naive|cnp:
                 multiply_plain, rescale, reduction (slot) or fresh output mask (cnp),
                 and the bias add; excludes (de)serialization, encrypt and decrypt,
                 gRPC and the client. Slot circuits always process 16 lane slots; the
                 cnp circuit takes --lanes. Key counts/keygen come from the same binary.
  e2e_grpc       fold and cnp only: a real vendor_server_160 process per packing mode
                 (the mode is fixed per process; fold = PPFD_PACKING=slot, cnp =
                 PPFD_PACKING=cnp) driven through the real bank-client path, with
                 client/server/e2e stage timings and wire bytes. The deployed server only
                 implements the 8-key fold, so bsgs/naive have no e2e row.
                 server_stage_us.compute_only_us (= multiply_plain + rotation + mask) is the
                 e2e rows' counterpart of the server_compute_only rows.
Slot-based strategies pack at most 16 lanes (4096 slots / 256); lanes=32 is CNP-only and is
recorded as skipped for the others.

Every gRPC configuration is parity-gated BEFORE timing: a stratified sample
(seeded; includes frauds) is run through the same strategy at the same lane
count and compared against X@w+b; a failure (> 1e-3) aborts that configuration
and no timings are recorded for it. AUC in the output is over that gate sample.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cnp_common as C  # noqa: E402

PARITY_TOL = 1e-3
ALL_STRATEGIES = ("fold", "bsgs", "naive", "cnp")
GATE_N = 64  # stratified parity-gate sample: 32 frauds + 32 non-frauds


# ───────────────────────────── helpers ─────────────────────────────
def pct(a, q):
    return float(np.percentile(np.asarray(a, dtype=np.float64), q))


def stat(a) -> dict:
    a = np.asarray(a, dtype=np.float64)
    return {"mean_us": float(a.mean()), "median_us": pct(a, 50), "p50_us": pct(a, 50), "p95_us": pct(a, 95),
            "p99_us": pct(a, 99), "min_us": float(a.min()), "max_us": float(a.max()), "n": int(a.size)}


def parse_cores(spec: str | None):
    if not spec:
        return None
    cores = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            cores.update(range(int(lo), int(hi) + 1))
        else:
            cores.add(int(part))
    return cores


def machine_state(server_cores, client_cores, omp_threads) -> dict:
    def rd(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    govs = sorted({rd(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor") or "unknown"
                   for i in range(os.cpu_count() or 1)})
    cpu = next((l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines()
                if l.startswith("model name")), "unknown")
    commit = os.environ.get("PPFDAAS_REPO_SHA", "")[:7]  # baked into the Docker image (no git inside)
    if not commit or commit == "unknown":
        try:
            commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=C.REPO_ROOT, capture_output=True,
                                    text=True).stdout.strip() or "unknown"
        except OSError:
            commit = "unknown"
    return {
        "cpu_governor": govs[0] if len(govs) == 1 else govs,
        "intel_no_turbo": rd("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "cpu_model": cpu, "logical_cores": os.cpu_count(), "kernel": platform.release(),
        "omp_num_threads": str(omp_threads) if omp_threads is not None else os.environ.get("OMP_NUM_THREADS", "unset"),
        "server_cores_pinned": sorted(server_cores) if server_cores else "unpinned",
        "client_cores_pinned": sorted(client_cores) if client_cores else "unpinned",
        "client_affinity_now": sorted(os.sched_getaffinity(0)),
        "git_commit": commit, "seal_version": "4.1.2",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def gate_sample(X, y, seed=20260628):
    rng = np.random.default_rng(seed)
    pos = rng.choice(np.where(y == 1)[0], GATE_N // 2, replace=False)
    neg = rng.choice(np.where(y == 0)[0], GATE_N // 2, replace=False)
    idx = np.concatenate([pos, neg])
    rng.shuffle(idx)
    return idx


def keygen_time_us(strategy: str) -> float:
    """Wall time to generate the key material a deployment of this strategy needs."""
    w = C.get_wrapper()  # same module object the BankClient uses (see cnp_common.get_wrapper)
    t0 = time.perf_counter()
    if strategy == "cnp":
        w.generate_keys_160_no_galois()  # pk + sk, no Galois keys
    else:
        w.generate_keys_160()  # pk + sk + 8 fold Galois keys
    return (time.perf_counter() - t0) * 1e6


# ───────────────────────────── gRPC configurations (fold, cnp) ─────────────────────────────
def run_one_request(client, chunk, mode):
    """One instrumented request; returns per-stage client + server timings and wire sizes."""
    import uuid

    from generated import inference_pb2
    from scipy.special import expit

    n = chunk.shape[0]
    w = client._wrapper
    t0 = time.perf_counter()
    if mode == "cnp":
        ct = w.encrypt_cnp_batch(np.ascontiguousarray(chunk, dtype=np.float64))
    else:
        buf = np.zeros((C.SLOT_BATCH, 256))
        buf[:n] = chunk
        ct = w.encrypt_batch(np.ascontiguousarray(buf.ravel(), dtype=np.float64))
    t1 = time.perf_counter()
    req = inference_pb2.InferenceRequest(
        ciphertext=ct, request_id=str(uuid.uuid4()), institution_id="BENCH", n_transactions=n,
        packing_mode=inference_pb2.CNP if mode == "cnp" else inference_pb2.SLOT)
    t2 = time.perf_counter()
    resp = client._stub.RunInference(req, timeout=30.0)
    t3 = time.perf_counter()
    if resp.status != inference_pb2.InferenceStatus.OK:
        raise RuntimeError(f"vendor error {resp.status}: {resp.error_message}")
    raw = (w.decrypt_cnp_batch(resp.result_ciphertext, n) if mode == "cnp"
           else w.decrypt_batch(resp.result_ciphertext, C.SLOT_BATCH)[:n])
    t4 = time.perf_counter()
    expit(raw)
    t5 = time.perf_counter()
    td = resp.timing
    return {
        "raw": np.asarray(raw),
        "encode_encrypt_us": (t1 - t0) * 1e6, "grpc_roundtrip_us": (t3 - t2) * 1e6,
        "decode_decrypt_us": (t4 - t3) * 1e6, "sigmoid_us": (t5 - t4) * 1e6, "e2e_us": (t5 - t0) * 1e6,
        "network_and_grpc_overhead_us": (t3 - t2) * 1e6 - td.total_inference_us,
        "server": {"deserialization_us": td.deserialization_us, "multiply_plain_us": td.multiply_plain_us,
                   "rotation_hoisting_us": td.rotation_hoisting_us, "mask_us": td.mask_us,
                   "serialization_us": td.serialization_us, "total_inference_us": td.total_inference_us},
        "bytes_up": len(ct), "bytes_down": len(resp.result_ciphertext),
    }


def grpc_config(client, strategy, lanes, n_requests, warmup, X, y, w, bias, gate_idx) -> dict:
    from sklearn.metrics import roc_auc_score
    mode = "cnp" if strategy == "cnp" else "slot"
    row = {"strategy": strategy, "scope": "e2e_grpc", "lanes": lanes, "requests": n_requests, "warmup": warmup}

    # ── parity gate (before ANY timing) ──
    gX, gy = X[gate_idx], y[gate_idx]
    plain = gX @ w + bias
    enc = np.concatenate([run_one_request(client, gX[lo:lo + lanes], mode)["raw"]
                          for lo in range(0, len(gX), lanes)])
    err = float(np.abs(enc - plain).max())
    row.update(parity_gate={"n": int(len(gX)), "max_abs_error": err, "tolerance": PARITY_TOL,
                            "passed": bool(err < PARITY_TOL)},
               max_abs_score_error=err,
               auc_gate_sample=float(roc_auc_score(gy, enc)), auc_gate_sample_plaintext=float(roc_auc_score(gy, plain)))
    if err >= PARITY_TOL:
        row["status"] = "PARITY_GATE_FAILED -- no timings recorded"
        return row

    # ── timing ──
    pool = X[: max(lanes * (n_requests + warmup), lanes)]
    reqs = [pool[i * lanes:(i + 1) * lanes] for i in range(n_requests + warmup)]
    for ch in reqs[:warmup]:
        run_one_request(client, ch, mode)
    rec = [run_one_request(client, ch, mode) for ch in reqs[warmup:]]
    cs = ("encode_encrypt_us", "grpc_roundtrip_us", "network_and_grpc_overhead_us", "decode_decrypt_us",
          "sigmoid_us", "e2e_us")
    row["client_stage_us"] = {k: stat([r[k] for r in rec]) for k in cs}
    row["server_stage_us"] = {k: stat([r["server"][k] for r in rec]) for k in rec[0]["server"]}
    row["server_stage_us"]["compute_only_us"] = stat(
        [r["server"]["multiply_plain_us"] + r["server"]["rotation_hoisting_us"] + r["server"]["mask_us"]
         for r in rec])
    row["bytes_up"] = int(np.median([r["bytes_up"] for r in rec]))
    row["bytes_down"] = int(np.median([r["bytes_down"] for r in rec]))
    row["summary_us"] = {"server_p50": row["server_stage_us"]["total_inference_us"]["p50_us"],
                         "server_p99": row["server_stage_us"]["total_inference_us"]["p99_us"],
                         "client_p50": row["client_stage_us"]["e2e_us"]["p50_us"] - row["server_stage_us"]["total_inference_us"]["p50_us"],
                         "e2e_p50": row["client_stage_us"]["e2e_us"]["p50_us"],
                         "e2e_p99": row["client_stage_us"]["e2e_us"]["p99_us"]}
    row["status"] = "OK"
    return row


def run_grpc_strategy(strategy, lanes_list, n_requests, warmup, server_cores, client_cores, omp, X, y, w, bias,
                      port=C.DEFAULT_PORT):
    mode = "cnp" if strategy == "cnp" else "slot"
    rows = []
    kg = keygen_time_us(strategy)
    gk_bytes = 0 if strategy == "cnp" else (C.ART / "galois_keys_160.bin").stat().st_size
    pk_bytes = (C.ART / "public_key_160.bin").stat().st_size
    proc, addr = C.launch_server(mode, "prod", omp_threads=omp, pin_cores=server_cores, port=port)  # one process per mode
    try:
        t0 = time.perf_counter()
        client = C.make_client(addr, mode)  # slot: ProvisionGaloisKeys + canary; cnp: nothing to provision
        setup_us = (time.perf_counter() - t0) * 1e6
        gate_idx = gate_sample(X, y)
        for lanes in lanes_list:
            if mode == "slot" and lanes > C.SLOT_BATCH:
                rows.append({"strategy": strategy, "scope": "e2e_grpc", "lanes": lanes,
                             "status": f"SKIPPED: slot packing holds at most {C.SLOT_BATCH} lanes"})
                continue
            row = grpc_config(client, strategy, lanes, n_requests, warmup, X, y, w, bias, gate_idx)
            row.update(key_bytes={"galois_keys": int(gk_bytes), "public_key": int(pk_bytes)},
                       keygen_us=kg, client_setup_us=setup_us,
                       rotations_per_request=8 if strategy == "fold" else 0)
            rows.append(row)
    finally:
        C.stop_server(proc)
    return rows


# ───────────────────────────── server-compute-only circuits (all four) ─────────────────────────────
def run_local_circuit(strategy, lanes, n_requests, server_cores, omp) -> dict:
    exe = C.BUILD_DIR / "vendor_server" / "benchmark_160"
    slot = strategy != "cnp"
    row = {"strategy": strategy, "scope": "server_compute_only", "lanes": C.SLOT_BATCH if slot else lanes,
           "requests": n_requests, "client_stage_us": None, "server_stage_us": None, "bytes_up": None,
           "bytes_down": None, "bytes_note": "no gRPC / serialization in this scope"}
    if slot:
        row["lanes_note"] = "slot circuit always processes all 16 lane slots; not a per-lane measurement"
    if not exe.exists():
        row["status"] = f"SKIPPED: {exe} not built"
        return row
    env = dict(os.environ, PPFD_BENCHMARK_ROUNDS=str(n_requests))
    # benchmark_160's context wants a client Galois-key file at a compile-time path; this is the
    # documented benchmark-only escape hatch (latency only; the circuits below use their own keys).
    env.setdefault("PPFDAAS_ALLOW_LOCAL_GALOIS", "1")
    if omp is not None:
        env["OMP_NUM_THREADS"] = str(omp)
    args = [str(exe), f"--strategy={strategy}"] + ([] if slot else [f"--lanes={lanes}"])
    proc = subprocess.run(args, capture_output=True, text=True, env=env, cwd=exe.parent,
                          preexec_fn=(lambda: os.sched_setaffinity(0, server_cores)) if server_cores else None)
    out = proc.stdout
    end = out.rfind("}")
    if end == -1:
        row["status"] = f"FAILED: no JSON from benchmark_160 ({proc.stderr[-200:]})"
        return row
    r = json.loads(out[: end + 1])
    row.update(stages=r["stages"],
               parity_gate={"max_abs_error": r["correctness_max_abs_error"], "passed": bool(r["correctness_passed"]),
                            "note": "benchmark_160 in-band gate (synthetic random oracle, includes bias)"},
               max_abs_score_error=r["correctness_max_abs_error"], auc_gate_sample=None,
               key_bytes={"galois_key_count": r["galois_keys"], "galois_keys": "not measured in this scope"},
               keygen_us=r["galois_keygen_us"], rotations_per_request=r["rotations"])
    if not r["correctness_passed"]:
        row["status"] = "PARITY_GATE_FAILED -- no timings recorded"
        return row
    lat = r["latency_us"]
    row["circuit_us"] = {"mean_us": lat["mean"], "p50_us": lat["p50"], "median_us": lat["p50"], "p95_us": lat["p95"],
                         "p99_us": lat["p99"], "min_us": lat["min"], "max_us": lat["max"], "n": r["n"]}
    row["status"] = "OK"
    return row


# ───────────────────────────── driver ─────────────────────────────
def run_matrix(strategies, lanes_list, n_requests, warmup, server_cores=None, client_cores=None, omp=None,
               out=None, port=C.DEFAULT_PORT) -> dict:
    if client_cores:
        os.sched_setaffinity(0, client_cores)
    X, y = C.load_test_set()
    w, bias = C.load_model()
    rows = []
    for s in strategies:
        print(f"[cnp_bench] strategy={s} ...", flush=True)
        # server-compute-only row(s): every strategy, same binary, same stages
        if s == "cnp":
            rows += [run_local_circuit(s, ln, n_requests, server_cores, omp) for ln in lanes_list]
        else:
            rows.append(run_local_circuit(s, C.SLOT_BATCH, n_requests, server_cores, omp))
        # end-to-end gRPC rows: fold and cnp only
        if s in ("fold", "cnp"):
            rows += run_grpc_strategy(s, lanes_list, n_requests, warmup, server_cores, client_cores, omp, X, y, w,
                                      bias, port)
    result = {
        "framing": {
            "description": "strategy x lanes x requests harness; every row is parity-gated before timing.",
            "scope_legend": {
                "server_compute_only": "benchmark_160, all four strategies, same stages: multiply_plain + rescale + "
                                       "reduction/mask + bias add. No (de)serialization, no encrypt/decrypt, no gRPC.",
                "e2e_grpc": "fold and cnp only: real server process + real bank-client path, client+server+wire. "
                            "Compare its server_stage_us.compute_only_us with the server_compute_only rows, not its e2e_us.",
            },
            "server_port": port,
            "machine_state": machine_state(server_cores, client_cores, omp),
            "requests_per_config": n_requests, "warmup_per_config": warmup,
            "percentile_caveat": "p99 over fewer than ~100 requests is just the maximum-ish order statistic",
        },
        "rows": rows,
    }
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, indent=2))
    return result


def print_table(result: dict) -> None:
    print(f"\n{'strategy':8} {'scope':20} {'lanes':>5} {'status':8} {'srv p50':>9} {'srv p99':>9} {'e2e p50':>9} "
          f"{'e2e p99':>9} {'up B':>8} {'down B':>8} {'max err':>9}")
    for r in result["rows"]:
        st = str(r.get("status", "?"))[:8]
        head = f"{r['strategy']:8} {r['scope']:20} {r.get('lanes', '-'):>5} {st:8}"
        if r.get("summary_us"):
            s = r["summary_us"]
            c = r["server_stage_us"]["compute_only_us"]
            print(f"{head} {s['server_p50']:>9.0f} {s['server_p99']:>9.0f} {s['e2e_p50']:>9.0f} {s['e2e_p99']:>9.0f} "
                  f"{r['bytes_up']:>8} {r['bytes_down']:>8} {r['max_abs_score_error']:>9.1e}   (compute-only p50 {c['p50_us']:.0f})")
        elif r.get("circuit_us"):
            c = r["circuit_us"]
            print(f"{head} {c['p50_us']:>9.0f} {c['p99_us']:>9.0f} {'-':>9} {'-':>9} {'-':>8} {'-':>8} "
                  f"{r['max_abs_score_error']:>9.1e}")
        else:
            print(f"{r['strategy']:8} {r['scope']:20} {r.get('lanes', '-'):>5} {r.get('status', '?')}")


def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strategies", default=",".join(ALL_STRATEGIES), help="comma list of fold,bsgs,naive,cnp")
    ap.add_argument("--lanes", default="1,4,8,16,32")
    ap.add_argument("--requests", type=int, default=1000, help="timed requests per configuration")
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--port", type=int, default=C.DEFAULT_PORT,
                    help="port for the e2e gRPC server processes (started one at a time); fails clearly if in use")
    ap.add_argument("--omp-threads", type=int, default=None)
    ap.add_argument("--server-cores", default=None, help="e.g. 0-7 (pins the server / benchmark_160 process)")
    ap.add_argument("--client-cores", default=None, help="e.g. 8-11 (pins this client process)")
    ap.add_argument("--out", default=None, help="default artifacts/cnp_bench.json (smoke: no file unless given)")
    ap.add_argument("--smoke", action="store_true", help="10 requests, 2 warmup; writes no file unless --out is given")
    a = ap.parse_args(argv)
    strategies = [s for s in a.strategies.split(",") if s]
    bad = [s for s in strategies if s not in ALL_STRATEGIES]
    if bad:
        ap.error(f"unknown strategies {bad}")
    n, warm = (10, 2) if a.smoke else (a.requests, a.warmup)
    out = a.out if (a.out or a.smoke) else str(C.ART / "cnp_bench.json")
    res = run_matrix(strategies, [int(x) for x in a.lanes.split(",")], n, warm, parse_cores(a.server_cores),
                     parse_cores(a.client_cores), a.omp_threads, out, a.port)
    print_table(res)
    print(f"\n[cnp_bench] wrote {out}" if out else "\n[cnp_bench] smoke run: no file written")
    failed = [r for r in res["rows"] if str(r.get("status", "")).startswith(("PARITY", "FAILED"))]
    if failed:
        print(f"[cnp_bench] {len(failed)} row(s) FAILED: " + "; ".join(f"{r['strategy']}/{r.get('lanes')}" for r in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(cli())
