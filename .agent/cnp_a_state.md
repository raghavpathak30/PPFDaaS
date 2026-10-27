# CNP-A state (branch `cnp-a`)

Coefficient-Native Packing for the SEAL CKKS 160-bit pipeline (N=8192, {60,40,60}, scale 2^40),
behind `PPFD_PACKING=slot|cnp` (server, per process) / `packing='cnp'` (bank client).
Plaintext gate and the chosen precision pair live in `docs/cnp_gate.md` (that gate is the BFV/mod-t
Pipeline-B analysis; CNP-A here is the CKKS arm and does not use the (8,6) pair).

## Decisions (user-approved this session)
- **Bias stays server-side**, folded into the mask: coefficient 256k+255 of the mask = `round(bias * ct.scale())`
  using the ciphertext's ACTUAL post-rescale scale, same parms_id, at ALL 32 outputs regardless of
  `n_transactions`. Every other coefficient is uniform mod q. (`vendor_server/include/cnp_codec.h:build_output_mask`)
- Server mode fixed per process by `PPFD_PACKING` (default `slot`); mismatching `packing_mode` ->
  `FAILED_PRECONDITION`, never a fallback. `n_transactions` outside 1..32 in CNP -> `INVALID_ARGUMENT`.
- CNP server goes READY at startup via the existing `on_structural_result` / `on_canary_result`
  transitions (`provisioning_state.h` is byte-identical). `ProvisionGaloisKeys` and `CanaryCheck` return
  `FAILED_PRECONDITION` and change no state in CNP mode.

## STALE DOC (not edited, per instruction)
`docs/code_and_data_flow.md` still says the client applies the bias:
- line 9   "Bank-side code decrypts and applies sigmoid + bias for probabilities."
- line 63  "Applies sigmoid: `expit(raw + bias)`."
- line 158 step 10: "Client applies sigmoid + bias and returns fraud probabilities."
Both pipelines apply the bias SERVER-SIDE (slot: `add_plain(pt_bias_)`, `inference_service_160.cpp`;
CNP: inside the mask). The client only applies the sigmoid (`bank_client.py:203` comment is correct).

## Pitfalls found
- `tests/test_inference.py::test_runtime_validation` hard-codes port 50052 (inside the kernel's ephemeral range,
  can collide with outbound connections) AND overwrites the tracked `artifacts/errors.json`. After running it,
  `git checkout -- artifacts/errors.json`. New CNP tests/scripts bind free ports and write elsewhere.
- Bare `pytest` from the repo root fails collection: 14 errors, all in `third_party/openfhe-development/**/googletest|googlemock`
  helper scripts (`No module named 'googletest'/'googlemock'`). Pre-existing, unrelated to this branch.
- Two source-grep tests in `tests/test_inference.py` fail on main already (they grep the 200-bit `inference_service.cpp`
  for `invariant_noise_budget` / `ct_out_buf_`).
- `build/` binaries can be stale (the May `test_he_core` failed against current artifacts until rebuilt). Rebuild before trusting.
- `bank_client` wrapper build needs `-DCMAKE_POLICY_DEFAULT_CMP0057=NEW` and system python3 (pybind11 is in ~/.local):
  `cmake -S bank_client -B build/bank_client -DPython3_EXECUTABLE=/usr/bin/python3 -DCMAKE_POLICY_DEFAULT_CMP0057=NEW`.

## Build / test
    cmake -S . -B build -DFETCHCONTENT_FULLY_DISCONNECTED=ON
    cmake --build build -j --target vendor_server_160 vendor_server_160_cnp_test benchmark_160 test_he_core
    cmake --build build/bank_client -j --target seal_wrapper_160
    .venv/bin/python -m pytest tests/test_cnp.py -q          # 24 tests, ~13 s; reports go to $PPFD_CNP_TEST_OUT or a tmp dir
    .venv/bin/python scripts/cnp_parity.py --mode cnp|slot --out <path>

`vendor_server_160_cnp_test` is TEST-ONLY (`-DPPFDAAS_CNP_TEST_NOMASK`): it honours `PPFD_CNP_TEST_MASK_OFF=1`
(all-zero mask: raw product, no bias). The production binary does not contain that string (tests assert it).

## Follow-up (bench scopes, Docker, test hygiene)
- Bench rows carry `scope`: `server_compute_only` (benchmark_160, all four strategies, same stages incl. bias add)
  or `e2e_grpc` (fold, cnp only). Compare an e2e row's `server_stage_us.compute_only_us` with the matching
  server_compute_only row, never its `e2e_us`. 10-request smoke numbers are not results.
- `pytest.ini` sets `testpaths = tests`: bare `pytest` no longer collects third_party/. Still red: the two 200-bit
  source-grep tests in tests/test_inference.py (pre-existing).
- `run_runtime_validation` and tests/test_cnp.py write to $PPFD_ERRORS_JSON / $PPFD_CNP_TEST_OUT or a tmp dir.
  Regenerate tracked artifacts deliberately: `python scripts/cnp_parity.py --mode cnp --out artifacts/errors_cnp.json`.
- Load the 160-bit wrapper ONLY via `cnp_common.get_wrapper()`. Importing the same .so under two module names
  (`bank_client.seal_wrapper_160` + `seal_wrapper_160`) double-initialises it and crashed Python 3.11 at exit.
- Docker: `tools/cnp/Dockerfile.cnpbench` (image `ppfdaas-cnp-bench`); run via `scripts/dgx/run_ckks.sh <job> cnp|smoke`.
  artifacts/ is mounted read-only, never baked in.
