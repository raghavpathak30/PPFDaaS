# CKKS benchmark harness (`ckks_bench`)

Measures the base CKKS arm (SEAL 4.1.2, n=8192, 16 txns x 256 features per
ciphertext) on one machine, for comparison with the transciphering arm run on
the same machine. Numbers from different machines must not be mixed.

Self-contained: SEAL + OpenMP only, keys generated in-process, no gRPC/Python.
The reductions are the production ones, compiled unchanged from
`vendor_server/src/rotation_hoisting.cpp` (`hoisted_tree_sum` = the 8-rotation
fold, `bsgs_reduction` = 30 rotations in 2 parallel layers).

| Target | Measures | Output |
|---|---|---|
| `parity` | plaintext parity gate (tol 1e-3), both param sets, fold and bsgs | `parity.json` |
| `a1` | per request: client encode, encrypt, vendor multiply_plain, rescale, fold, client decrypt, decode; request/response ciphertext bytes | `a1_stage_breakdown.json` |
| `a2` | fold vs BSGS, interleaved per repetition, same input ciphertext | `a2_fold_vs_bsgs.json` |
| `a3` | one vendor request vs OpenMP threads (default 1,2,4,8,16,20), fold and bsgs | `a3_thread_scaling.json` |
| `a4` | N concurrent worker threads, closed loop for `--duration` s (default N = 1,2,4,8,16,20) | `a4_throughput.json` |

Every timing target runs the parity gate first, per param set and strategy (a3:
per thread count, a4: on every worker's output under concurrency) and skips
timing, exiting 1, if it fails. a1 also records the worst error over all timed
repetitions.

## Run

```
docker build -f tools/ckks/Dockerfile.bench \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" -t ppfdaas-ckks-bench .
scripts/dgx/run_ckks.sh <job> parity|a1|a2|a3|a4|all      # see its header for env vars
```

Native: `cmake -S tools/ckks -B build && cmake --build build`, then
`build/ckks_bench a1 --reps=100 --out=.` (flags in `parse_args`, `ckks_bench.cpp`).

## Reading the results

* `per_transaction_ms = per_request_ms / 16`, applied to median/min/max
  (formula repeated in each JSON's `accounting_note`). a4 additionally gives
  `amortized_wall_ms_per_txn = 1000 / throughput_txn_per_s`.
* The fold is a sequential dependency chain and has no OpenMP regions
  (`rotation_hoisting.cpp:73`); only `bsgs_reduction` does (`:149`, `:161`), and
  SEAL 4.1.2 is single-threaded internally. So A3 should show fold flat and BSGS
  scaling; that is a hypothesis the DGX run tests, not a result.
* a1/a2 use 1 OpenMP thread unless `OMP_THREADS` is set. a3 sets the thread count
  itself. a4 uses 1 OpenMP thread per worker and runs with `OMP_PROC_BIND=false`:
  with binding on, libgomp pins the main thread to one core and every worker
  thread inherits that mask (seen in a containerized test run: a4 did not scale).
* All N a4 workers share one process, evaluator, Galois keys and SEAL's global
  memory pool, like the deployed server's gRPC pool.
* Weight encoding and keygen are one-time (`setup_one_time`), not per request.
* SEAL's keygen/encryption randomness is not seeded; the plaintext workload is
  (`--seed`, default 42). Ciphertexts differ between runs.

## Block-width sweep (not implemented)

`packing.h` expresses the layout in terms of block width `b`: `4096/b` txns per
ciphertext, `256/b` ciphertexts per batch, `log2(b)` rotations, and a generic
`pack()`. Only `b = 256` is supported: `require_deployed_layout()`
(`packing.h:52`, called at `ckks_bench.cpp:206`) rejects anything else, because
`hoisted_tree_sum`/`bsgs_reduction` hard-code 256. The generic `b < 256` path
has not been run. Adding the sweep means a fold taking `b` and a `--block` loop.
