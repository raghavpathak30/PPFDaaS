# third_party/RtF-Transciphering — build notes

`third_party/` is gitignored (except this file and `fetch_rtf.sh`, which are
the reproducibility mechanism — see `.gitignore`). This file records the
toolchain and build verification for the vendored checkout, re-verified
2026-08-19 rather than copied from an earlier session's claim.

## Checkout

- Source: `github.com/KAIST-CryptLab/RtF-Transciphering` (a Lattigo-v2-era
  fork, module path `github.com/ldsec/lattigo/v2`, package `ckks_fv`).
- Pinned commit: `105fc73115b56f1d6ff357029c7682b19a6d8510` (branch
  `master`), fetched via `third_party/fetch_rtf.sh`.
- Fork's own `go.mod`: `go 1.13` directive, no `replace` lines.

## Toolchain

- Go **1.25.0** (`go version go1.25.0 linux/amd64`), installed on the dev
  host. No toolchain pin or downgrade was needed to build the fork.

## Build verification (re-run 2026-08-19, not assumed from a prior session)

Run from `third_party/RtF-Transciphering/ckks_fv/`:

```
go vet ./...                    # exit 0
go test -c -run '^$' .          # exit 0, produces a runnable test binary
```

Both exit 0 with **zero source changes** to the vendored checkout. The
"dependency/API breakage" framing that appears in older docs in this repo
does not apply to this checkout as of this pinned SHA and this Go version.

## What is and isn't blocked by this

- Build/link: confirmed clean (above).
- Toy-scale (`LogN=10`) correctness harnesses: confirmed PASS this session
  for all three staged configs — see `tools/transciphering/toy_correctness/
  README.md`.
- Full-scale (`LogN=16`, real secure params) HERA evaluation: RAM, not
  build, is the constraint — see `PROJECT_STATE.md` and
  `docs/MEASUREMENT_PROVENANCE.md` row 17 for the one existing full-scale
  measurement (HERA only, 9.54 GB peak VmHWM).
- Full-scale Rubato-128L evaluation: not attempted; no full-scale measurement
  exists for this cipher at all, toy-scale only.
