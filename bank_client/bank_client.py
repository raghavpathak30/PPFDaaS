import numpy as np, grpc, uuid, time, struct

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

    def __init__(self, vendor_address, weights_path='artifacts/model_weights.bin',
                 public_key_path='artifacts/public_key.bin',
                 secret_key_path='artifacts/secret_key.bin', use_tls=False,
                 wrapper_module='seal_wrapper', grpc_max_message_length=512 * 1024):
        wrapper = seal_wrapper if wrapper_module == 'seal_wrapper' else _load_wrapper_module(wrapper_module)
        self._wrapper = wrapper
        wrapper.init_seal(
            Path(public_key_path).read_bytes(),
            Path(secret_key_path).read_bytes())
        with open(weights_path, 'rb') as f:
            f.read(4)
            self._bias, = struct.unpack('<d', f.read(8))
        self._pad_buffer = np.zeros((16, 256), dtype=np.float64)
        options = _grpc_options(grpc_max_message_length)
        ch = grpc.secure_channel(vendor_address,
                grpc.ssl_channel_credentials(), options) if use_tls else \
             grpc.insecure_channel(vendor_address, options)
        self._stub = inference_pb2_grpc.FraudInferenceServiceStub(ch)
        self._warmup()

    def _warmup(self) -> None:
        x = np.zeros((1, 256), dtype=np.float64)
        try:
            resp = self.run_inference(x, institution_id='WARMUP', timeout_seconds=0.25)
            print(f"[BankClient] warmup complete: {resp['timing_breakdown']['total_inference_us']} us")
        except Exception:
            return

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
        probs = expit(raw + self._bias)
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
