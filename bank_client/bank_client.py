import numpy as np, grpc, uuid, time

from scipy.special import expit

from pathlib import Path
import importlib
import sys

from generated import inference_pb2, inference_pb2_grpc

try:
    import seal_wrapper
except ModuleNotFoundError:
    bank_client_dir = Path(__file__).resolve().parent
    if str(bank_client_dir) not in sys.path:
        sys.path.insert(0, str(bank_client_dir))
    import seal_wrapper


def _load_wrapper_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        return importlib.import_module(f"bank_client.{module_name}")

def _grpc_options(max_message_bytes: int):
    return [
        ('grpc.max_send_message_length', max_message_bytes),
        ('grpc.max_receive_message_length', max_message_bytes),
        ('grpc.keepalive_time_ms',               30_000),
        ('grpc.keepalive_timeout_ms',             5_000),
        ('grpc.keepalive_permit_without_calls',       1),
    ]

class BankClient:
    """WARNING: BankClient is not thread-safe. Use one instance per thread or protect with a lock."""

    def __init__(self, vendor_address,
                 public_key_path='artifacts/public_key.bin',
                 secret_key_path='artifacts/secret_key.bin', use_tls=False,
                 wrapper_module='seal_wrapper', grpc_max_message_length=512 * 1024,
                 galois_keys_path=None,
                 tls_ca_path=None, tls_cert_path=None, tls_key_path=None):
        wrapper = seal_wrapper if wrapper_module == 'seal_wrapper' else _load_wrapper_module(wrapper_module)
        self._wrapper = wrapper
        wrapper.init_seal(
            Path(public_key_path).read_bytes(),
            Path(secret_key_path).read_bytes())
        self._pad_buffer = np.zeros((16, 256), dtype=np.float64)
        options = _grpc_options(grpc_max_message_length)
        if use_tls:
            # §1.7: mutually authenticated TLS. If tls_ca_path/tls_cert_path/
            # tls_key_path are given (see scripts/generate_dev_certs.sh), the
            # client presents its own certificate (mTLS) and verifies the
            # server against tls_ca_path instead of the system trust store --
            # required for the dev CA used in local/compose deployments. If
            # none are given, falls back to grpc.ssl_channel_credentials()
            # (system roots, server-auth only).
            if tls_ca_path or tls_cert_path or tls_key_path:
                if not (tls_ca_path and tls_cert_path and tls_key_path):
                    raise ValueError(
                        "tls_ca_path, tls_cert_path, and tls_key_path must all be "
                        "given together for mTLS (or all omitted for system-root TLS)")
                creds = grpc.ssl_channel_credentials(
                    root_certificates=Path(tls_ca_path).read_bytes(),
                    private_key=Path(tls_key_path).read_bytes(),
                    certificate_chain=Path(tls_cert_path).read_bytes())
            else:
                creds = grpc.ssl_channel_credentials()
            ch = grpc.secure_channel(vendor_address, creds, options)
        else:
            ch = grpc.insecure_channel(vendor_address, options)
        self._stub = inference_pb2_grpc.FraudInferenceServiceStub(ch)

        # §1.4/§1.5: if the vendor's Galois keys are not yet provisioned, push
        # them and run the canary handshake (§1.2b) now. The 200-bit baseline
        # server (vendor_server_main) does not implement this protocol and is
        # always called with galois_keys_path=None.
        if galois_keys_path is not None:
            result = self.provision_and_validate(galois_keys_path)
            state_name = inference_pb2.ProvisioningState.Name(result['state'])
            print(f"[BankClient] provisioning: state={state_name} detail={result['detail']}")
            if result['state'] != inference_pb2.PROV_READY:
                raise RuntimeError(f"vendor did not reach PROV_READY: {result}")

        self._warmup()

    def _warmup(self) -> None:
        x = np.zeros((1, 256), dtype=np.float64)
        try:
            resp = self.run_inference(x, institution_id='WARMUP', timeout_seconds=0.25)
            print(f"[BankClient] warmup complete: {resp['timing_breakdown']['total_inference_us']} us")
        except Exception:
            return

    # ─── §1.4 / §1.5 — provisioning protocol ───────────────────────────────
    #
    # Replaces the shared artifacts/ volume: the bank is the only party that
    # holds galois_keys_160.bin (generated together with public_key_160.bin
    # and secret_key_160.bin as one consistent triple by
    # compiler/gen_keys_160.py). It pushes the Galois keys to the vendor over
    # the same channel as inference traffic, then runs the canary handshake
    # (§1.2b) to prove -- without ever sending the secret key -- that the
    # provisioned Galois keys are consistent with this bank's secret key.
    def provision_and_validate(self, galois_keys_path: str, timeout_seconds: float = 5.0) -> dict:
        galois_keys_bytes = Path(galois_keys_path).read_bytes()
        print(f"[BankClient] ProvisionGaloisKeys: pushing {galois_keys_path} "
              f"({len(galois_keys_bytes)} bytes)")
        resp = self._stub.ProvisionGaloisKeys(
            inference_pb2.ProvisionGaloisKeysRequest(galois_keys=galois_keys_bytes),
            timeout=timeout_seconds)
        if resp.state != inference_pb2.PROV_VALIDATING:
            return {'state': resp.state, 'detail': resp.message}
        print(f"[BankClient] ProvisionGaloisKeys: {resp.message}")

        # Canary: a constant vector has a known, analytically-computable
        # rotation-sum result (256 * canary_value on every slot, by the
        # hoisted_tree_sum window-sum invariant -- rotation_hoisting.cpp).
        # If the Galois keys are well-formed but were generated under a
        # DIFFERENT secret key (Bug B), the key-switch error term dominates
        # and the decrypted result is off by many orders of magnitude.
        canary_value = 0.01
        canary_vec = np.full(4096, canary_value, dtype=np.float64)
        ct_bytes = self._wrapper.encrypt_batch(canary_vec)
        print("[BankClient] CanaryCheck: sent encrypted canary, awaiting rotation result")
        canary_resp = self._stub.CanaryCheck(
            inference_pb2.CanaryRequest(ciphertext=ct_bytes), timeout=timeout_seconds)
        if canary_resp.state != inference_pb2.PROV_VALIDATING:
            return {'state': canary_resp.state, 'detail': canary_resp.message}

        decoded = self._wrapper.decrypt_batch(canary_resp.result_ciphertext, 16)
        expected = 256.0 * canary_value
        passed = bool(np.all(np.abs(decoded - expected) < 1e-3))
        message = (
            f"canary matched expected {expected} on all 16 lanes (tol 1e-3)"
            if passed else
            f"canary MISMATCH: decoded={decoded.tolist()}, expected={expected} "
            f"(tol 1e-3) -- Galois keys are not consistent with this secret key (Bug B)"
        )
        confirm = self._stub.CanaryConfirm(
            inference_pb2.CanaryConfirmRequest(passed=passed, message=message),
            timeout=timeout_seconds)
        return {'state': confirm.state, 'detail': confirm.message}

    def get_provisioning_status(self, timeout_seconds: float = 2.0) -> dict:
        resp = self._stub.GetProvisioningStatus(
            inference_pb2.ProvisioningStatusRequest(), timeout=timeout_seconds)
        return {'state': resp.state, 'detail': resp.detail}

    def run_inference(self, X: np.ndarray, institution_id='BANK_001',
                      timeout_seconds=0.5) -> dict:
        n_txns, n_feat = X.shape
        if n_feat != 256: raise ValueError(f'Expected 256 features, got {n_feat}')
        if not (1 <= n_txns <= 16): raise ValueError(f'n_txns must be 1-16')
        if n_txns < 16:
            self._pad_buffer[:n_txns] = X
            self._pad_buffer[n_txns:] = 0.0
            flat = np.ascontiguousarray(self._pad_buffer.ravel(), dtype=np.float64)
        else:
            flat = np.ascontiguousarray(X.ravel(), dtype=np.float64)
        t_start = time.perf_counter()
        ct_bytes   = self._wrapper.encrypt_batch(flat)
        request_id = str(uuid.uuid4())
        req = inference_pb2.InferenceRequest(
            ciphertext=ct_bytes, request_id=request_id,
            institution_id=institution_id, n_transactions=n_txns)
        resp = self._stub.RunInference(req, timeout=timeout_seconds)
        if resp.status != inference_pb2.InferenceStatus.OK:
            raise RuntimeError(f'Vendor error {resp.status}: {resp.error_message}')
        if resp.request_id != request_id:
            raise RuntimeError(f'Request ID mismatch: sent {request_id}')
        raw = self._wrapper.decrypt_batch(resp.result_ciphertext, n_txns)
        # §1.3: bias is applied server-side (inference_service_160.cpp) and is
        # already included in `raw`. The bank never sees model_weights.bin.
        probs = expit(raw)
        latency_ms = (time.perf_counter() - t_start) * 1000.0
        return {
            'fraud_probabilities': probs,
            'latency_ms':          latency_ms,
            'request_id':          resp.request_id,
            'timing_breakdown': {
                'deserialization_us':    resp.timing.deserialization_us,
                'multiply_plain_us':     resp.timing.multiply_plain_us,
                'rotation_hoisting_us':  resp.timing.rotation_hoisting_us,
                'serialization_us':      resp.timing.serialization_us,
                'total_inference_us':    resp.timing.total_inference_us,
            }
        }

    def close(self): ...
