"""Phase 2, §2.1 — concurrency correctness stress test.

Sends many concurrent RunInference requests at vendor_server_160 and checks:

  1. Every response decodes to a valid probability in (0, 1) -- a torn
     seal::Ciphertext (the Phase 1 acc_buf_/ct_out_buf_ data race) typically
     deserializes into garbage that either fails to decrypt cleanly or decodes
     to wildly out-of-range values.
  2. The SAME input, run many times concurrently, yields the SAME result
     (within floating-point tolerance) as a sequential reference -- shared
     mutable per-request buffers would make concurrent runs of an identical
     input interfere with each other.
"""

from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_inference import (  # noqa: E402
    _VENDOR_160,
    _is_port_open,
    _launch_server,
    _load_bank_client_class,
    _stop_server,
    _RUNTIME_READY,
)

N_REQUESTS = 32
N_WORKERS = 8
N_FEATURES = 256


def _random_input(rng: np.random.Generator) -> np.ndarray:
    return rng.normal(size=(1, N_FEATURES))


@pytest.mark.skipif(
    not _RUNTIME_READY,
    reason="Runtime deps absent: BankClient not yet implemented / artifacts not generated",
)
def test_concurrent_inference_correctness_and_determinism():
    BankClient = _load_bank_client_class()
    # Distinct port from _VENDOR_160's 50052 to avoid colliding with an
    # unrelated process that may already hold that port on the test host.
    cfg = {**_VENDOR_160, "address": "127.0.0.1:50053"}

    host, port_s = cfg["address"].split(":")
    port = int(port_s)

    own_server = None
    if not _is_port_open(host, port):
        own_server = _launch_server(cfg)

    try:
        # Provision the server once (ProvisionGaloisKeys + canary handshake ->
        # PROV_READY). RunInference is refused until this completes.
        provisioning_client = BankClient(
            cfg["address"],
            public_key_path=str(cfg["public_key_path"]),
            secret_key_path=str(cfg["secret_key_path"]),
            wrapper_module=cfg["wrapper_module"],
            grpc_max_message_length=cfg["grpc_max_message_length"],
            galois_keys_path=str(cfg["galois_keys_path"]),
        )

        # BankClient is documented as not thread-safe, so each worker thread
        # gets its own instance (own channel, own seal_wrapper-backed
        # encryptor/decryptor handles). Galois keys are already provisioned at
        # this point, so these instances pass galois_keys_path=None.
        thread_local = threading.local()

        def _client() -> "BankClient":
            client = getattr(thread_local, "client", None)
            if client is None:
                client = BankClient(
                    cfg["address"],
                    public_key_path=str(cfg["public_key_path"]),
                    secret_key_path=str(cfg["secret_key_path"]),
                    wrapper_module=cfg["wrapper_module"],
                    grpc_max_message_length=cfg["grpc_max_message_length"],
                )
                thread_local.client = client
            return client

        def _run(x: np.ndarray) -> float:
            result = _client().run_inference(x, institution_id="CONCURRENT_TEST")
            return float(result["fraud_probabilities"][0])

        # --- Part 1: 32 concurrent requests, 32 distinct random inputs -----
        rng = np.random.default_rng(42)
        inputs = [_random_input(rng) for _ in range(N_REQUESTS)]

        with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            probs = list(pool.map(_run, inputs))

        assert len(probs) == N_REQUESTS
        for p in probs:
            assert np.isfinite(p) and 0.0 < p < 1.0, f"invalid probability: {p}"

        # --- Part 2: determinism under concurrency --------------------------
        # The same ciphertext run 8 times concurrently must agree with a
        # sequential reference run -- per-request mutable state must not leak
        # across concurrent calls.
        fixed_input = _random_input(rng)
        reference = _run(fixed_input)

        with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            repeated = list(pool.map(_run, [fixed_input] * N_WORKERS))

        for p in repeated:
            assert abs(p - reference) < 1e-5, (
                f"non-deterministic result under concurrency: got {p}, "
                f"expected {reference} (sequential reference)"
            )

        print(
            f"PASS: {N_REQUESTS} concurrent requests / {N_WORKERS} threads, "
            f"all valid probabilities. sample={probs[:5]}"
        )
        print(
            f"PASS: determinism under concurrency -- reference={reference:.10f} "
            f"repeated={[f'{p:.10f}' for p in repeated]}"
        )
    finally:
        if own_server is not None:
            _stop_server(own_server)


if __name__ == "__main__":
    test_concurrent_inference_correctness_and_determinism()
