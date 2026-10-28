"""Shared helpers for the CNP-A tests, parity runner and bench harness.

Everything that DECRYPTS lives here / in tests/ / scripts/ (bank side) -- never
under vendor_server/, which stays out of the secret-key TCB.
"""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
ART = REPO_ROOT / "artifacts"
BUILD_DIR = Path(os.environ.get("PPFD_BUILD_DIR", REPO_ROOT / "build"))

for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SERVER_BINARIES = {
    "prod": BUILD_DIR / "vendor_server" / "vendor_server_160",
    # TEST-ONLY: compiled with -DPPFDAAS_CNP_TEST_NOMASK (honours PPFD_CNP_TEST_MASK_OFF=1).
    "nomask_test": BUILD_DIR / "vendor_server" / "vendor_server_160_cnp_test",
}
GRPC_MAX = 8 * 1024 * 1024

SLOT_BATCH = 16
CNP_BATCH = 32


def get_wrapper():
    """The 160-bit pybind module, loaded exactly once under its top-level name.

    BankClient resolves it as top-level `seal_wrapper_160`. Importing the same .so under a second
    name (e.g. `bank_client.seal_wrapper_160`) initialises the extension twice, which crashed the
    interpreter at exit ("free(): invalid pointer") under Python 3.11 in the Docker image.
    """
    import importlib
    try:
        return importlib.import_module("seal_wrapper_160")
    except ModuleNotFoundError:
        importlib.import_module("bank_client")  # cache the (namespace) package FIRST: once bank_client/ is on
        d = str(REPO_ROOT / "bank_client")      # sys.path, bank_client.py there would otherwise shadow it
        if d not in sys.path:
            sys.path.append(d)  # append, never insert(0): must not shadow the `bank_client` package
        return importlib.import_module("seal_wrapper_160")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


DEFAULT_PORT = 50052


class PortInUseError(RuntimeError):
    pass


def ensure_port_free(port: int) -> None:
    """Raise PortInUseError (with the fix) unless `port` is free for a gRPC server on this host.

    Two probes, because gRPC sets SO_REUSEPORT: (1) a connect probe -- if anything accepts on
    127.0.0.1:port, a second server could silently share the port instead of failing to bind;
    (2) a bind probe mirroring the server (also catches a local port held by an outbound
    connection, which happens for ports inside the kernel's ephemeral range).
    """
    try:
        socket.create_connection(("127.0.0.1", port), 0.3).close()
    except OSError:
        pass
    else:
        raise PortInUseError(f"port {port} is already in use: something is listening on 127.0.0.1:{port}. "
                             f"Stop it or choose another port with --port.")
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as e:
        raise PortInUseError(f"port {port} is already in use ({e.strerror}). The kernel's ephemeral range is "
                             f"{open('/proc/sys/net/ipv4/ip_local_port_range').read().split()}; pick a port "
                             f"outside it, or choose another with --port.") from e
    finally:
        probe.close()


def launch_server(mode: str, binary: str = "prod", extra_env: dict | None = None,
                  port: int | None = None, omp_threads: int | None = None,
                  pin_cores: set | None = None):
    """Start a vendor_server_160 process in `mode` ('slot'|'cnp'); returns (proc, address).

    One process per mode: the mode is fixed for the lifetime of the process.
    """
    if port is None:
        port = free_port()  # tests: any free port
    else:
        ensure_port_free(port)  # explicit port: fail clearly, never reuse silently
    env = dict(os.environ)
    env["PPFD_PACKING"] = mode
    if omp_threads is not None:
        env["OMP_NUM_THREADS"] = str(omp_threads)
    env.update(extra_env or {})
    exe = SERVER_BINARIES[binary]
    if not exe.exists():
        raise FileNotFoundError(f"server binary not built: {exe}")
    proc = subprocess.Popen([str(exe), str(ART / "model_weights.bin"), str(port)], cwd=str(REPO_ROOT),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=(lambda: os.sched_setaffinity(0, pin_cores)) if pin_cores else None)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return proc, f"127.0.0.1:{port}"
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early (code={proc.returncode}) mode={mode} binary={exe}")
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("server did not open its port in time")


def stop_server(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def load_model():
    raw = (ART / "model_weights.bin").read_bytes()
    bias = struct.unpack("<d", raw[4:12])[0]
    w = np.load(ART / "weights.npy").astype(np.float64)
    assert w.shape == (256,)
    return w, float(bias)


def load_test_set():
    return np.load(ART / "X_test.npy").astype(np.float64), np.load(ART / "y_test.npy")


def make_client(address: str, packing: str):
    from bank_client.bank_client import BankClient

    kw = dict(public_key_path=str(ART / "public_key_160.bin"), secret_key_path=str(ART / "secret_key_160.bin"),
              wrapper_module="seal_wrapper_160", grpc_max_message_length=GRPC_MAX, packing=packing)
    if packing == "slot":
        kw["galois_keys_path"] = str(ART / "galois_keys_160.bin")  # provisioning + canary handshake
    return BankClient(address, **kw)


def request_batch(client, X_chunk: np.ndarray, packing: str, institution_id: str = "CNP_HARNESS",
                  timeout: float = 10.0):
    """One RunInference on `X_chunk`; returns (decrypted raw logits incl. server-side bias, response)."""
    import uuid

    from generated import inference_pb2

    n = X_chunk.shape[0]
    if packing == "cnp":
        ct = client._wrapper.encrypt_cnp_batch(np.ascontiguousarray(X_chunk, dtype=np.float64))
        req = inference_pb2.InferenceRequest(ciphertext=ct, request_id=str(uuid.uuid4()),
                                             institution_id=institution_id, n_transactions=n,
                                             packing_mode=inference_pb2.CNP)
    else:
        buf = np.zeros((SLOT_BATCH, 256))
        buf[:n] = X_chunk
        ct = client._wrapper.encrypt_batch(np.ascontiguousarray(buf.ravel(), dtype=np.float64))
        req = inference_pb2.InferenceRequest(ciphertext=ct, request_id=str(uuid.uuid4()),
                                             institution_id=institution_id, n_transactions=SLOT_BATCH)
    resp = client._stub.RunInference(req, timeout=timeout)
    if resp.status != inference_pb2.InferenceStatus.OK:
        raise RuntimeError(f"Vendor error {resp.status}: {resp.error_message}")
    if packing == "cnp":
        raw = client._wrapper.decrypt_cnp_batch(resp.result_ciphertext, n)
    else:
        raw = client._wrapper.decrypt_batch(resp.result_ciphertext, SLOT_BATCH)[:n]
    return np.asarray(raw), resp


def encrypted_logits(client, X: np.ndarray, packing: str) -> np.ndarray:
    batch = CNP_BATCH if packing == "cnp" else SLOT_BATCH
    out = np.empty(X.shape[0])
    for lo in range(0, X.shape[0], batch):
        hi = min(lo + batch, X.shape[0])
        out[lo:hi], _ = request_batch(client, X[lo:hi], packing)
    return out


def error_report(plain_logits, enc_logits, y) -> dict:
    """Same keys as artifacts/errors.json (tests/test_inference.py run_runtime_validation)."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    e = np.abs(plain_logits - enc_logits)
    return {
        "n_samples": int(len(e)),
        "max_abs_error": float(e.max()),
        "mean_abs_error": float(e.mean()),
        "median_abs_error": float(np.median(e)),
        "abs_error_distribution": {
            "p50": float(np.percentile(e, 50)), "p90": float(np.percentile(e, 90)),
            "p99": float(np.percentile(e, 99)), "p99.9": float(np.percentile(e, 99.9)),
            "min": float(e.min()), "max": float(e.max()),
        },
        "roc_auc_encrypted": float(roc_auc_score(y, enc_logits)),
        "pr_auc_encrypted": float(average_precision_score(y, enc_logits)),
    }
