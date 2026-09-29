# Rubato-128L Full-Scale Plan — post-2026-08-19 non-completion

Written after Rubato-128L's first full-scale (LogN=16) attempt was
SIGKILLed by the OOM killer on this ~16 GB host, and after tracing that
failure to a LogSlots parameter-set mismatch rather than a cipher
difference (`docs/MEASUREMENT_PROVENANCE.md`'s "PRIMARY FINDING" section,
`PROJECT_STATE.md`'s 2026-08-19 dated block). Read those first — this
document assumes their conclusions.

**Do not run anything in this document at LogN=16/LogSlots=15 on this
host.** Every number below that involves LogSlots=15 is an estimate
projected from partial data, not a measurement, and is flagged as such.

## 1. Headroom needed — derived from the 13.28 GB floor, not the toy 2.08x guess

The 2026-08-19 Rubato attempt reached **13,279,632 KB VmHWM (13.28 GB)** at
the `slot_to_coeff_mat` checkpoint and was killed before the next stage
(rotation-key generation) even started. That number is a **floor on setup
memory**, not a peak — real full-scale memory (LogSlots=15, either cipher)
is unmeasured and higher.

**Projection (ESTIMATE, not measurement):** HERA's own completed full-scale
trace (`artifacts/hera_crypt_rss_full_run.jsonl`, LogSlots=4) gives a real
growth shape from its StC-precompute checkpoint to its final peak:

| HERA checkpoint | vm_hwm_kb |
|---|---|
| `after_stc_precompute` | 3,974,228 |
| `after_key_material` (rotation + relin keys) | 7,628,232 |
| `after_evaluator` | 8,006,600 |
| `after_enckey` | 9,265,680 |
| peak (`round_5_cube`) | 9,543,896 |

Growth factor, StC-checkpoint to peak: `9,543,896 / 3,974,228 ≈ 2.40x`.
Applying that factor to Rubato's StC-checkpoint floor: `13,279,632 × 2.40 ≈
31.9 GB`. **Treat this as a rough planning floor, likely an
underestimate**, not a prediction: rotation-key generation
(`GenRotationKeysForRotations`) generates a key per distinct rotation
index, and the rotation-index set size itself grows with LogSlots (more
slots → more distinct halfboot/StC rotation amounts) — so the "key
material" jump HERA saw at LogSlots=4 (+3.65 GB) is expected to be larger,
not equal, at Rubato's LogSlots=15. No confirmed multiplier exists for that
effect; it is a directional caveat, not a number.

**Recommendation:** plan for **≥40 GB**, and treat any number below that as
insufficient margin. This is still a projection — the actual number is
whatever the intermediate rungs below measure, extrapolated properly once
there's more than one data point.

## 2. Highest-value next experiment: HERA at `RtFHeraParams[2]` ("128af", LogSlots=15) — not a bigger box for Rubato

The LogSlots-matched comparison already exists in this checkout and costs
nothing to add: `RtFHeraParams[2]` is HERA's own full-coefficient variant
(LogN=16, LogSlots=15 — same slot occupancy as Rubato's only param set),
distinct from the sparse `RtFHeraParams[3]` ("128as") that produced the
9.54 GB figure. Running `TestRSSCheckpointHeraCrypt` with
`RSS_HERA_PARAM_INDEX=2` gives HERA's own memory cost at the *same* slot
occupancy Rubato was forced to use — the actual apples-to-apples point,
in place of inventing a new Rubato variant (e.g. a hypothetical
"Rubato-128S" at a smaller round count) that would still leave HERA's own
LogSlots=15 cost unmeasured.

**Expected outcome, stated up front: this will probably also not fit on
this ~16 GB host.** `GenSlotToCoeffMatFV`'s cost is driven by LogSlots and
`modCount`, not by which cipher calls it — HERA's own StC step will pay
close to the same LogSlots=15 tax Rubato's did. **That is itself the
finding worth stating, not a failed experiment**: the full-scale memory
wall is a HalfBoot/StC-at-full-slot-occupancy property, largely
independent of cipher, not a Rubato-specific cost. Confirming that (HERA
also fails to fit at LogSlots=15) would settle the question this whole
recovery pass opened; it should be attempted on a host with the headroom
from §1 before Rubato is retried at all.

## 3. Intermediate rung — runnable on this host, for a real scaling curve

`tools/transciphering/cipher/rss_checkpoint_test.go` supports this via
`RSS_LOGN_OVERRIDE`. **Constraint discovered while writing that harness**:
Rubato's "128af" param set uses full-coefficient packing, `LogSlots =
LogN-1` by construction (`Params()` panics otherwise — see
`rtf_toy_correctness_rubato128l_test.go`'s own comment on this). **LogSlots
cannot be held at 15 while LogN shrinks** — the earlier framing of this rung
("LogN 12 or 14, LogSlots held at 15") is not achievable for Rubato as
implemented, and by the same full-coefficient-packing logic is very likely
not achievable for HERA's "128af" either (untested this pass — HERA's "as"
variant does hold LogSlots fixed at 4 independent of LogN, but "as" is the
sparse variant, not the one being tested here). So the intermediate rung
necessarily scales LogSlots down with LogN, which will itself reduce the
measured memory relative to a hypothetical LogSlots=15-held-fixed run —
**an interpretation caveat for whatever curve gets fit, not a reason to
skip the rung.**

Recommended rungs, both cheap on this host, run one cipher at a time:

| LogN | LogSlots (`LogN-1`) | Slots | vs. full-scale slot count |
|---|---|---|---|
| 10 (existing toy point) | 9 | 512 | 1/64x |
| 12 | 11 | 2,048 | 1/16x |
| 14 | 13 | 8,192 | 1/4x |
| 16 (failed) | 15 | 32,768 | 1x |

Run both HERA-128af (`RSS_HERA_PARAM_INDEX=2`) and Rubato-128af at each of
12 and 14, alongside the existing LogN=10 toy point, to fit a curve in
LogSlots with three real data points per cipher instead of extrapolating
from one. Commands:

```
RSS_CHECKPOINT_RUN=1 RSS_LOGN_OVERRIDE=12 RSS_HERA_PARAM_INDEX=2 \
  RSS_CHECKPOINT_PATH=artifacts/hera_128af_logn12_rss.jsonl \
  GOMEMLIMIT=11GiB GOGC=50 \
  go test ./cipher/ -run TestRSSCheckpointHeraCrypt -v -timeout 30m

RSS_CHECKPOINT_RUN=1 RSS_LOGN_OVERRIDE=12 \
  RSS_CHECKPOINT_PATH=artifacts/rubato_128af_logn12_rss.jsonl \
  GOMEMLIMIT=11GiB GOGC=50 \
  go test ./cipher/ -run TestRSSCheckpointRubatoCrypt -v -timeout 30m
```

(repeat with `RSS_LOGN_OVERRIDE=14`). Each writes the new `ParamState`
block into every checkpoint line, so the resulting artifacts are
self-describing — no separate provenance note will be needed to know what
config produced them.

## 4. The 90 GB preflight gate — replaced with an explicit estimate, not a measurement

`scripts/cloud_transcipher_bench/run_benchmark.sh`'s `MIN_FREE_RAM_KB` was
`~90 GB`, derived from a `~60 GB` literature anchor for HERA at 80-bit
security (arXiv:2409.06422v1 §II) that was never itself measured
end-to-end in this repo, and predates both the 9.54 GB HERA-128as
measurement and this document's LogSlots finding. It is being replaced
with the §1 estimate (~40 GB floor, itself a projection) plus margin,
**explicitly labelled as an estimate, not a measurement**, in the script's
comments and preflight failure message. It should be replaced again, with
a real number, once §3's intermediate rungs (or a real cloud run at full
scale) produce one.

## 5. What NOT to do

- Do not run `TestRSSCheckpointRubatoCrypt` or `TestRSSCheckpointHeraCrypt`
  with `RSS_HERA_PARAM_INDEX=2` at `RSS_LOGN_OVERRIDE` unset (i.e. LogN=16,
  full LogSlots=15) on this host. Both are expected, per §1-2, to need
  ~40 GB+.
- Do not compare HERA-128as (LogSlots=4) numbers against Rubato-128af
  (LogSlots=15) numbers for any memory or runtime claim — settled in
  `docs/MEASUREMENT_PROVENANCE.md`'s "PRIMARY FINDING" section.
- Do not treat the §1 projection (~32-40 GB) as a measured number in any
  paper draft. It is derived by applying HERA's own StC-to-peak growth
  ratio to Rubato's partial trace — an engineering estimate for capacity
  planning, explicitly flagged as likely an underestimate, not a citable
  figure.
