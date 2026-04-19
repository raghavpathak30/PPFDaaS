#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import importlib
import sys


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    artifacts = repo_root / "artifacts"
    bank_client_dir = repo_root / "bank_client"

    if str(bank_client_dir) not in sys.path:
        sys.path.insert(0, str(bank_client_dir))

    artifacts.mkdir(parents=True, exist_ok=True)

    try:
        wrapper = importlib.import_module("seal_wrapper_160")
    except Exception as exc:
        print("[gen_keys_160] failed to import seal_wrapper_160")
        print("[gen_keys_160] build bank_client targets first so the module exists")
        print(f"[gen_keys_160] import error: {exc}")
        return 1

    keys = wrapper.generate_keys_160()

    public_path = artifacts / "public_key_160.bin"
    secret_path = artifacts / "secret_key_160.bin"
    galois_path = artifacts / "galois_keys_160.bin"

    public_path.write_bytes(bytes(keys["public_key"]))
    secret_path.write_bytes(bytes(keys["secret_key"]))
    galois_path.write_bytes(bytes(keys["galois_keys"]))

    print(f"[gen_keys_160] wrote {public_path}")
    print(f"[gen_keys_160] wrote {secret_path}")
    print(f"[gen_keys_160] wrote {galois_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
