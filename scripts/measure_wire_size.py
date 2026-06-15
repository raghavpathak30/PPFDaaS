#!/usr/bin/env python3
"""§5.7 Part A: ciphertext wire-size measurement.

Runs vendor_server/build/wire_size_probe (a standalone, OUT-OF-TCB binary --
see vendor_server/src/wire_size_probe.cpp) which, for both the 160-bit
({60,40,60}) and 200-bit ({60,40,40,60}) modulus chains, encrypts the same
plaintext batch and measures:

  - "standard": seal::Ciphertext::save_size() for a public-key encryption --
    the wire format actually produced by bank_client/he_wrapper/seal_wrapper*.
  - "seeded": seal::Serializable<Ciphertext>::save_size() for a symmetric-key
    (encrypt_symmetric) encryption of the same plaintext -- a "what if"
    comparison point (requires the secret key; not what the bank's
    public-key path produces today).
  - zlib/zstd compressed sizes of the standard ciphertext and the resulting
    compression ratios.

Writes artifacts/wire_sizes.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
PROBE_BIN = REPO_ROOT / "vendor_server" / "build" / "wire_size_probe"


def main() -> int:
    if not PROBE_BIN.exists():
        print(f"Missing required binary: {PROBE_BIN}")
        print("Build it with: cmake --build vendor_server/build --target wire_size_probe -j4")
        return 1

    proc = subprocess.run([str(PROBE_BIN)], capture_output=True, text=True, check=True)
    chains = json.loads(proc.stdout)

    out = {
        "framing": {
            "description": (
                "§5.7 Part A: ciphertext wire sizes for a single (4096-slot) "
                "CKKS ciphertext, measured by vendor_server/build/wire_size_probe "
                "for both modulus chains. 'standard' is the public-key encryption "
                "wire format produced by bank_client/he_wrapper/seal_wrapper*. "
                "'seeded' is a symmetric-key (encrypt_symmetric) 'what if' "
                "comparison point -- not what the bank's public-key path "
                "produces today."
            ),
            "methodology": "measured",
            "source": "vendor_server/build/wire_size_probe",
        },
        "chains": chains,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_file = ARTIFACTS / "wire_sizes.json"
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nWrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
