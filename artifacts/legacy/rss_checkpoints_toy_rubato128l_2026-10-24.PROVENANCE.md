# Provenance: rss_checkpoints_toy_rubato128l_2026-10-24.jsonl

Copied unmodified from `third_party/RtF-Transciphering/ckks_fv/rss_checkpoints.jsonl`
(a dirty, gitignored working copy) on 2026-10-24 before that checkout was reset.

**The file itself records no cipher, param set, or LogN** (old-format records:
label, timestamp, vm_hwm_kb, heap_alloc_kb, total_alloc_kb, GOGC/GOMEMLIMIT).
What follows is inferred, not recorded:

- All 12 timestamps are 2026-10-24T16:46:56-57+05:30, within one second.
- The labels are exactly the 12 `checkpointRSS` calls in `mfvRubato.Crypt` for
  numRound=2 (crypt_entry, init, round_0, round_1 x4, round_2 x5, incl. the
  Rubato-only `_feistel` / `_finlinlayer` steps); `fv_hera.go` has no such calls.
- So it is a by-product of the 2026-10-24 toy-scale (LogN=10)
  `TestRtFRubato128LToyCorrectness` re-run (the instrumentation was
  unconditional and wrote to the cwd). VmHWM peak 570,248 kB.

It is NOT a full-scale (LogN=16) trace and is not comparable to
`artifacts/rubato_crypt_rss_full_run.jsonl` or `hera_crypt_rss_full_run.jsonl`.
Kept only because it was asked for; do not cite it as a measurement.
