# Paper fact checks

Checked on `main` at `b48fe24`, 2026-11-07. No code was changed. Paths are repo-relative unless they start with `~`. Commands that needed the RtF reference ran in scratch copies of the pinned checkout (`105fc73`), not in `third_party/`.

## 1. Was the HERA and Rubato known-answer test re-run after the plaintext-modulus fix?

**Yes, for both ciphers, and both pass now.** Both KAT vectors also still match what ckks_fv's own reference produces today.

**The fix:** commit `418be1f` (2026-10-24), "fix(hera): use ckks_fv's plaintext modulus 0x1fc0001, not 2^26", merged in `0319bbb`.

- **The modulus now:** `tools/transciphering/cipher/hera.go:63` sets `HeraModulus = uint64(0x1fc0001)`, which is t = 33,292,289. This matches the pinned reference's `RtFHeraParams[2]` and `[3]` at `ckks_fv/rtf_params.go:414` and `:482`.
- **Rubato was never wrong:** `tools/transciphering/cipher/rubato.go:80` sets `RubatoModulus = uint64(0x1fc0001)`, as does the reference at `ckks_fv/fv_rubato.go:37`. The fix did not touch Rubato. Its KAT vector was last changed in `557de43` (2026-08-18).

**The tests:**

| Test | Location | What it checks |
|---|---|---|
| `TestHERAKnownAnswer` | `tools/transciphering/cipher/hera_test.go:53` | Keystream block 0 against a stored `plainHera` vector (`:63`). Also fails if the old 2^26 vector reappears (`:74`). |
| `TestHeraModulusMatchesCkksFV` | `tools/transciphering/cipher/hera_test.go:23` | `HeraModulus` equals `RtFHeraParams[2,3].PlainModulus`, is prime, and is not a power of two. Added by `418be1f`. |
| `TestRubatoKnownAnswer` | `tools/transciphering/cipher/rubato_test.go:59` | Keystream block 0 at sigma=0 against a stored `plainRubato` vector (`:69`). |

**Re-run after the fix:** `418be1f` regenerated the HERA vector at t = 33,292,289. `docs/MEASUREMENT_PROVENANCE.md:87` records that, after the fix, `go test ./...` passed, and that both HERA tests failed when `HeraModulus` was reverted to `1<<26`. That `go test ./...` run covers `rubato_test.go` too, because it is in the same package.

**Run now** (`tools/transciphering`, go1.25.0):

```
$ go test ./cipher -run '^(TestHERAKnownAnswer|TestHeraModulusMatchesCkksFV|TestRubatoKnownAnswer)$' -count=1 -v
--- PASS: TestHeraModulusMatchesCkksFV (0.00s)
--- PASS: TestHERAKnownAnswer (0.00s)
--- PASS: TestRubatoKnownAnswer (0.00s)
ok  	github.com/raghavpathak30/ppfdaas/transciphering/cipher	0.003s
```

**Limit: the tests compare against stored vectors, not against ckks_fv at run time.** To close that gap, the reference was re-run on the same inputs in a scratch clone of the pinned, unpatched RtF checkout:

- **HERA:** the committed generator `tools/transciphering/cipher/testdata/hera_kat_gen_test.go`, which calls `plainHera(5, nonce, key, RtFHeraParams[3].PlainModulus)`.
- **Rubato:** a scratch test calling `plainRubato(64, 2, nonce, counter0, key, 0x1fc0001, 0)`, with the inputs documented at `rubato_test.go:41-46`. It was not committed anywhere.

Compared by program, all 16 HERA elements and all 60 Rubato elements equal the stored vectors.

**What this does not cover:**
- The Rubato KAT runs at sigma=0, so it says nothing about the noise sampler (`rubato_test.go:30-38`).
- Bare `pytest` doesn't run these Go tests.

## 2. Headroom of the real scores in `~/cnpb_data/expected.i64`

`expected.i64` has sha256 `5e6df6ea...0c5a4d`, 56,962 rows. Each row is `centre((xq·wq + bias_q) mod t)`, written by `scripts/cnp_b/prepare_inputs.py:47` and `:58`. The bias is already included.

| Quantity | Value |
|---|---|
| t, and t/2 | 33,292,289 and 16,646,144.5 |
| **max \|score\|, bias included** | **256,230** (row 41,799, score −256,230, logit −15.64) |
| log2(t/2) | 23.9887 |
| log2(max \|score\|) | 17.9671 |
| **Margin: log2(t/2) − log2(max \|score\|)** | **6.02 bits** |
| max \|xq·wq\|, without bias | 338,999, which gives 5.62 bits |
| Worst-case bound, `meta.json` `worst_case_bound` (Σ\|wq\|·2^8 + \|bias_q\|) | 889,976, which gives 4.23 bits |

All rows satisfy \|xq·wq + bias_q\| < t/2 with no wraparound, and they equal `expected.i64` exactly.

**The bias, as the server adds it:**
- **Value:** `bias_q = round_half_even(bias · 2^14) = −137,080` (`prepare_inputs.py:45`, `meta.json`). The float bias is −8.366707513150757.
- **How it is added:** the server places `(bias_q mod t) = 33,155,209 = t − 137,080` into the mask plaintext. For CNP-B that is the output coefficient of each lane (`rtf_cnp_test.go:241`), scaled up at `:250`. For slot it is each output slot (`rtf_slot_test.go:232`). The mask is then added homomorphically to the result (`rtf_slot_test.go:902`). The plaintext check uses the same rule: `cnpSim` returns `centre(Σ xq·wq + biasQ, t)` (`rtf_cnp_test.go:73-78`).

The harness files are under `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/`.

## 3. "Lifting a negative weight to t − |w| makes the noise about 2^23 times larger"

**It is a measurement, at toy scale, taken from the console output of `TestCNPSignedLift`.** No run of it is saved in the repo. It is not measured at production settings.

**Where the claim comes from:**
- **Claim in the repo:** `docs/cnp_b.md:22`: "~2^23 more noise at level 3 and ~19 bits less headroom at level 0 (`TestCNPSignedLift`)". It was added in `0fbfc76` (2026-10-28).
- **Test:** `tools/transciphering/toy_correctness/testdata/ckks_fv_patch/rtf_cnp_lift_test.go:13`. At `:46-69` it encrypts a fixed plaintext (`:50`) at level 3, multiplies by the weights with the signed lift and with the [0, t) lift, and logs the noise difference (`:69`). It only asserts that the difference is at least 8 bits (`:73`). The number itself is never stored.
- **Weights:** the test uses synthetic seeded weights in [−64, 64], not the model's. It calls `cnpLoadData(t, "", ...)` at `rtf_cnp_lift_test.go:19`, which takes the branch at `rtf_cnp_test.go:95-101`.

**Re-run now:** LogN 10, `E2E_RUN=1 E2E_LOGN=10 go test -run '^TestCNPSignedLift$' -v`, in a scratch staging of the patched checkout made by `third_party/fetch_rtf.sh`.

```
E2E_CIPHER=hera
level 0: signed lift headroom 21.03 bits (noise 2^13.0); [0,t) lift headroom 2.21 bits; 0 of 1024 coefficients differ mod t
level 3: signed lift   log2 noise 30.37, headroom 138.64 bits
level 3: [0,t) lift    log2 noise 53.60, headroom 115.41 bits
level 3: coefficients differing mod t: 0 of 1024; extra noise from the [0,t) lift: 2^23.2
--- PASS: TestCNPSignedLift (1.48s)
E2E_CIPHER=rubato
level 0: signed lift headroom 20.67 bits (noise 2^13.3); [0,t) lift headroom 1.11 bits; 0 of 1024 coefficients differ mod t
level 3: (same three lines as HERA, extra noise 2^23.2)
--- PASS: TestCNPSignedLift (1.40s)
```

**What this means for the sentence:**

| Setting | Extra noise from the [0, t) lift | Source |
|---|---|---|
| Fresh encryption at level 3, LogN 10, synthetic weights | **2^23.2**. This is the 2^23. | Measured (above) |
| Production level 0, the CNP-B ciphertext after Sub, LogN 10 | **2^18.8 (HERA), 2^19.6 (Rubato)**, read from the headroom loss. With the [0, t) lift, only 2.21 and 1.11 bits of headroom remain, below the 5-bit gate. | Measured (above) |
| Real model weights | **About 2^20.3** (ratio of the L2 norms of lifted and signed real `wq`; 110 of 256 weights are negative). Pmul noise only. | ESTIMATE, not a run |
| LogN 16 | Not measured | — |

**Recommended wording:** the paper should either state the setting, for example "about 2^23 more noise at level 3 (measured, LogN 10)", or use the production figure: "about 19 bits less headroom at level 0, which leaves under 5 bits". Without a setting, 2^23 overstates the effect at the level CNP-B actually uses.
