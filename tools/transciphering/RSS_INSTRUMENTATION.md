# hera.Crypt RSS instrumentation — working document

Phase 7 Task 2 (2026-08-04 crash-recovery session, continuing 2026-07-28's
Task 4 finding that `hera.Crypt` is where the unidentified OOM cost lives).
See `PROJECT_STATE.md`'s 2026-08-04 session block for the full narrative;
this file is the working detail for that block's Task 2 subsections.

Instrumentation source (both files are TEMPORARY, gitignored,
`third_party/RtF-Transciphering/ckks_fv/`, never committed — deleted after
measurement per project convention):

- `rss_checkpoint.go` — `checkpointRSS(label)` helper. Appends one JSON line
  per call to `$RSS_CHECKPOINT_PATH` (default `rss_checkpoints.jsonl`),
  flushed (`f.Sync()`) immediately so a crash doesn't lose prior checkpoints.
- `rss_checkpoint_driver_test.go` — `TestRSSCheckpointHeraCrypt`, mirrors
  `BenchmarkRtFHera128as`'s setup (`RtFHeraParams[3]`, `numRound=5`,
  `radix=2`, `fullCoeffs=false`) through `hera.Crypt`.
- Checkpoint calls inside `fv_hera.go`'s `Crypt()` itself (the file under
  test, patched directly — same temporary/gitignored status).

Output: `artifacts/hera_crypt_rss_checkpoints.jsonl`.

## Confirmed facts (2026-08-04)

| # | Question | Answer | Tag |
|---|---|---|---|
| 2a | Which param set produced the 11-checkpoint trace? | `RtFHeraParams[3]` ("128as"), full LogN=16, direct measurement — not the LogN-10 toy, not extrapolated. | CONFIRMED-SOURCE (`rss_checkpoint_driver_test.go` lines 18-24) |
| 2b | Is `heap_alloc_kb` actually `TotalAlloc` mislabelled? | No — source reads `ms.HeapAlloc` correctly. The field name/label was never wrong. | CONFIRMED-SOURCE (`rss_checkpoint.go:52`, pre-fix) |
| 2b | Then why does `heap_alloc_kb` (14.4 GB) exceed `vm_hwm_kb`/RSS (9.0 GB) at `round_0`? | Not confirmed. Leading hypothesis: partial swap — `HeapAlloc` counts allocated-and-unswept Go heap objects regardless of whether the OS paged them out, and this host has an 8 GB swap partition that was 4.4 GB used when checked (post-crash, not proof of during-crash state). | HYPOTHESIS |

## Fix applied (2b)

`rss_checkpoint.go` now records both:
- `heap_alloc_kb` — `runtime.MemStats.HeapAlloc / 1024` (live heap, unchanged)
- `total_alloc_kb` — `runtime.MemStats.TotalAlloc / 1024` (cumulative
  allocation, new — quantifies allocation churn, which the swap hypothesis
  above implies may be the actual driver of the OOM, not steady-state
  working set)

## Fix applied (2c)

`fv_hera.go`'s `Crypt()` previously checkpointed only at round boundaries.
The 2026-08-04 crash trace shows `entry` → `round_0` → **death** (no
`round_1` checkpoint), meaning the process died somewhere inside round 1's
body: `linLayer` → `cube` → `modSwitch` → `addRoundKey`, none of which were
individually observable. Added a `checkpointRSS` call after each of those
four sub-steps, in both the main loop (`r=1..numRound-1`) and the final
un-looped round, with labels `round_<r>_linlayer`, `round_<r>_cube`,
`round_<r>_modswitch`, `round_<r>` (existing addRoundKey checkpoint, kept).

## Existing trace (unconstrained, partial — died during round 1)

Preserved as-is; do not overwrite. See
`artifacts/hera_crypt_rss_checkpoints.jsonl` (11 lines) and the
PROJECT_STATE.md session block for the full RSS progression table.

## 2d — re-run to completion — [CONFIRMED-RAN] DONE

Run by the user from a bare terminal (VS Code closed), per the plan above:

```
GOMEMLIMIT=11GiB GOGC=50 RSS_CHECKPOINT_PATH=artifacts/hera_crypt_rss_full_run.jsonl \
  go test -run '^TestRSSCheckpointHeraCrypt$' -v -timeout 30m .
```

**Result: PASS**, `logs/hera_full_run.log`: `--- PASS: TestRSSCheckpointHeraCrypt (73.71s)`,
total `go test` wall time 73.981s. Full trace (33 checkpoints, entry
through `test_end`, all with `gomemlimit_env=11GiB`/`gogc_env=50` recorded
per-line): `artifacts/hera_crypt_rss_full_run.jsonl`.

**hera.Crypt completed all 5 HERA rounds without dying — this is the
headline result.** The crash three weeks earlier (see the unconstrained
partial trace, `artifacts/hera_crypt_rss_checkpoints.jsonl`, dead after
`round_0` at `vm_hwm_kb=8,976,116`) did **not** reproduce once (a)
`GOMEMLIMIT=11GiB GOGC=50` were set and (b) VS Code + competing processes
were closed.

Key numbers from the full trace:

| checkpoint | vm_hwm_kb (RSS) | heap_alloc_kb (live) | total_alloc_kb (cumulative) |
|---|---|---|---|
| `entry` (start of `hera.Crypt`) | 9,265,684 | 10,819,389 | 14,669,245 |
| `round_0` | 9,264,592 | 10,874,636 | 15,283,704 |
| `round_5_cube` (peak RSS) | **9,543,896** | 11,141,953 (not peak; peak heap_alloc 11,141,967 at round_4/5_cube) | 22,881,734 |
| `exit` | 9,543,896 | 10,842,149 | 23,143,943 |

- **Peak RSS (VmHWM) for the whole test process: 9,543,896 KB ≈ 9.10 GiB**,
  reached at `round_5_cube` and held flat through `exit`. Well under the
  11 GiB `GOMEMLIMIT` — the limit was never actually hit; the run simply
  didn't need more.
- **`hera.Crypt` itself (entry → exit) only grew RSS by ~278 MB**
  (9,265,684 → 9,543,896 KB) across all 5 rounds, in ~51.8s. The ~9.1 GB
  steady state is overwhelmingly the **setup phase** (key material +
  StC precompute, matching the 2026-07-28 Task 4 finding), not the round
  loop itself.
- **`total_alloc_kb` (cumulative, new field from the 2b fix) grew by
  ~8.5 GB during `hera.Crypt` alone** (14,669,245 → 23,143,943 KB) while
  live heap and RSS stayed essentially flat — confirms real allocation
  churn inside the round loop (temporaries per `linLayer`/`cube`/
  `modSwitch`/`addRoundKey`), but GC (helped by `GOGC=50`) recycles it
  fast enough that it never shows up as a working-set or RSS problem.
- `heap_alloc_kb` is no longer monotonic (e.g. `round_1_linlayer` 11,028,250
  → `round_1_cube` 10,962,320, a genuine decrease) — behaves like a real
  live-heap counter under GC pressure, unlike the crashed trace's
  monotonic climb to 14.4M KB while RSS was only 8.97M KB.

**Conclusion on the original "why did 9 GB kill a 15 GB host" question:**
the most likely explanation is external memory pressure (VS Code + 3
concurrent Claude Code processes + Chrome, all confirmed running at crash
time) consuming the ~6 GB headroom that would otherwise have absorbed the
run, combined with default `GOGC=100` (no ceiling) letting the heap grow
unchecked. It was **not** a runaway cost inherent to `hera.Crypt`'s round
loop — that loop is well-behaved (+278 MB across 5 rounds) once given a
GC ceiling and a host not fighting it for RAM. Not re-tested unconstrained
+ VS Code closed (to isolate which of the two fixes mattered most) — noted
as a possible follow-up, not pursued.

## Deprioritised (2e) — LogN ladder

Not started. 2a already confirms direct full-scale measurement exists, so
extrapolation from toy scale is redundant unless 2d fails again and
sub-scale checkpoints become the only way to localize the fault further.
