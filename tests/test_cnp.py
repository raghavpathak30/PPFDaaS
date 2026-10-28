"""CNP-A (Coefficient-Native Packing) tests.

Every test that decrypts lives here, outside vendor_server/ (out of the TCB).
Servers are real vendor_server_160 processes on free ports, one process per
packing mode (the mode is fixed per process).

Measured numbers (max errors, p-values) are recorded by test_zz_record_numbers, and the full
parity report by test_4, into $PPFD_CNP_TEST_OUT (default: a tmp dir) -- NEVER into the tracked
artifacts/ unless that is passed explicitly. The committed artifacts/errors_cnp.json comes from
`python scripts/cnp_parity.py --mode cnp --out artifacts/errors_cnp.json`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import grpc
import numpy as np
import pytest
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
import cnp_common as C  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (C.SERVER_BINARIES["prod"].exists() and C.SERVER_BINARIES["nomask_test"].exists()
         and (C.ART / "secret_key_160.bin").exists() and (C.ART / "X_test.npy").exists()),
    reason="needs built vendor_server_160 / vendor_server_160_cnp_test and artifacts/*_160.bin + X_test.npy",
)

PARITY_TOL = 1e-3  # the repo's logit parity tolerance
OUT_DIR = Path(os.environ.get("PPFD_CNP_TEST_OUT") or Path(tempfile.gettempdir()) / "ppfdaas")
OUT_DIR.mkdir(parents=True, exist_ok=True)
NUMBERS: dict = {}
W, BIAS = C.load_model() if (C.ART / "weights.npy").exists() else (None, None)


# ───────────────────────────── fixtures ─────────────────────────────
@pytest.fixture(scope="module")
def wrapper():
    w = C.get_wrapper()  # one load, same object BankClient uses (a 2nd import name double-initialises the .so)
    w.init_seal((C.ART / "public_key_160.bin").read_bytes(), (C.ART / "secret_key_160.bin").read_bytes())
    return w


def _server(mode, binary="prod", env=None):
    proc, addr = C.launch_server(mode, binary, env)
    return proc, addr


@pytest.fixture(scope="module")
def cnp_server():
    proc, addr = _server("cnp")
    yield addr
    C.stop_server(proc)


@pytest.fixture(scope="module")
def slot_server():
    proc, addr = _server("slot")
    yield addr
    C.stop_server(proc)


@pytest.fixture(scope="module")
def nomask_server():
    proc, addr = _server("cnp", "nomask_test", {"PPFD_CNP_TEST_MASK_OFF": "1"})
    yield addr
    C.stop_server(proc)


@pytest.fixture(scope="module")
def prod_with_off_env_server():
    # PRODUCTION binary, but with the test-only env var set: it must be ignored.
    proc, addr = _server("cnp", "prod", {"PPFD_CNP_TEST_MASK_OFF": "1"})
    yield addr
    C.stop_server(proc)


@pytest.fixture(scope="module")
def cnp_client(cnp_server, wrapper):
    return C.make_client(cnp_server, "cnp")


@pytest.fixture(scope="module")
def slot_client(slot_server, wrapper):
    return C.make_client(slot_server, "slot")


@pytest.fixture(scope="module")
def Xtest():
    return C.load_test_set()[0]


def _stub_call(addr_client, ct_bytes, n, mode):
    from generated import inference_pb2
    req = inference_pb2.InferenceRequest(ciphertext=ct_bytes, request_id=str(uuid.uuid4()),
                                         institution_id="TEST", n_transactions=n, packing_mode=mode)
    return addr_client._stub.RunInference(req, timeout=10.0)


def _rpc_code(fn):
    with pytest.raises(grpc.RpcError) as ei:
        fn()
    return ei.value.code()


# ─────────────────────── 1. encode -> decode (no encryption) ───────────────────────
def test_1_encode_decode_roundtrip_no_encryption(wrapper):
    rng = np.random.default_rng(1)
    worst = 0.0
    for scale_amp in (1.0, 40.0):
        v = rng.uniform(-scale_amp, scale_amp, 8192)
        back = wrapper.cnp_encode_decode(v)
        worst = max(worst, float(np.abs(back - v).max()))
    NUMBERS["t1_max_roundtrip_err"] = worst
    assert worst <= 0.5 / 2.0 ** 40 + 1e-15, worst  # exact up to rounding to the 2^-40 grid


# ─────────────────────── 2. encrypt -> decrypt (no evaluation) ───────────────────────
def test_2_encrypt_decrypt_roundtrip(wrapper):
    rng = np.random.default_rng(2)
    worst = 0.0
    for _ in range(5):
        v = rng.uniform(-1, 1, 8192)
        worst = max(worst, float(np.abs(wrapper.cnp_encrypt_decrypt(v) - v).max()))
    NUMBERS["t2_max_enc_dec_err"] = worst
    assert worst < 1e-9, worst


def test_decoder_refuses_multi_limb_ciphertext(wrapper):
    ct = wrapper.encrypt_cnp_batch(np.zeros((1, 256)))  # fresh: 2 RNS limbs
    assert wrapper.cnp_ct_limbs(ct) == 2
    with pytest.raises(RuntimeError, match="exactly 1 RNS limb"):
        wrapper.cnp_decrypt_all(ct)


# ─────────────────────── 3. layout under encryption ───────────────────────
@pytest.mark.parametrize("lanes", [1, 2, 16, 31, 32])
def test_3_layout_under_encryption(cnp_client, lanes):
    rng = np.random.default_rng(100 + lanes)
    worst = 0.0
    for _ in range(3):
        X = rng.uniform(-1, 1, (lanes, 256))  # dense, every coefficient of every lane filled
        raw, _ = C.request_batch(cnp_client, X, "cnp")
        worst = max(worst, float(np.abs(raw - (X @ W + BIAS)).max()))
    NUMBERS[f"t3_max_err_lanes_{lanes}"] = worst
    assert worst < PARITY_TOL, worst


# ─────────────────────── 4. full parity (56,962 test transactions) ───────────────────────
def test_4_full_parity_cnp(cnp_client):
    X, y = C.load_test_set()
    enc = C.encrypted_logits(cnp_client, X, "cnp")
    rep = C.error_report(X @ W + BIAS, enc, y)
    rep["packing"] = "cnp"
    rep["batch_size"] = C.CNP_BATCH
    (OUT_DIR / "errors_cnp.json").write_text(json.dumps(rep, indent=2))
    NUMBERS["t4"] = {k: rep[k] for k in ("n_samples", "max_abs_error", "mean_abs_error", "median_abs_error",
                                         "roc_auc_encrypted", "pr_auc_encrypted")}
    assert rep["n_samples"] == 56962
    assert rep["max_abs_error"] < PARITY_TOL, rep["max_abs_error"]


# ─────────────────────── 5. mask ───────────────────────
def _chi2(raw_u64, q0, bins=64):
    u = raw_u64.astype(np.float64) / float(q0)
    h = np.bincount(np.minimum((u * bins).astype(np.int64), bins - 1), minlength=bins)
    return stats.chisquare(h)


NON_OUT = np.setdiff1d(np.arange(8192), np.arange(32) * 256 + 255)
OUT = np.arange(32) * 256 + 255


def test_5a_masked_equals_unmasked_plus_bias(cnp_client, nomask_server, wrapper):
    nomask_client = C.make_client(nomask_server, "cnp")
    rng = np.random.default_rng(5)
    X = rng.uniform(-1, 1, (5, 256))  # only 5 lanes filled: the bias must still land on all 32 outputs
    ct = wrapper.encrypt_cnp_batch(X)
    masked = wrapper.cnp_decrypt_all(_stub_call(cnp_client, ct, 5, 1).result_ciphertext)
    unmasked = wrapper.cnp_decrypt_all(_stub_call(nomask_client, ct, 5, 1).result_ciphertext)
    diff = (masked - unmasked)[OUT]
    NUMBERS["t5a_max_dev_from_bias"] = float(np.abs(diff - BIAS).max())
    assert np.abs(diff - BIAS).max() < 1e-6
    # unmasked outputs are the plain dot products (no bias) on the 5 real lanes
    assert np.abs(unmasked[OUT[:5]] - X @ W).max() < PARITY_TOL
    # and the real lanes decode to x.w + b on the masked path
    assert np.abs(masked[OUT[:5]] - (X @ W + BIAS)).max() < PARITY_TOL


def test_5b_non_outputs_uniform_mod_q0(cnp_client, nomask_server, wrapper):
    nomask_client = C.make_client(nomask_server, "cnp")
    X = np.load(C.ART / "X_test.npy")[:32].astype(np.float64)
    ct = wrapper.encrypt_cnp_batch(X)
    pooled, ps, first = [], [], None
    for i in range(8):
        arr, q0, scale = wrapper.cnp_decrypt_raw(_stub_call(cnp_client, ct, 32, 1).result_ciphertext)
        pooled.append(arr[NON_OUT])
        ps.append(_chi2(arr[NON_OUT], q0).pvalue)
        if first is None:
            first = arr[NON_OUT]
        else:
            assert (arr[NON_OUT] != first).mean() > 0.999, "mask is not fresh per request"
    pooled = np.concatenate(pooled)
    res = _chi2(pooled, q0)
    un_arr, _, _ = wrapper.cnp_decrypt_raw(_stub_call(nomask_client, ct, 32, 1).result_ciphertext)
    un = _chi2(un_arr[NON_OUT], q0)
    NUMBERS["t5b"] = {"masked_single_request_p": ps, "masked_pooled_p": float(res.pvalue),
                      "pooled_n": int(len(pooled)), "unmasked_p": float(un.pvalue)}
    assert res.pvalue > 1e-4, f"masked non-output coefficients not uniform (p={res.pvalue})"
    assert un.pvalue < 1e-6, "control: unmasked coefficients should NOT look uniform"


def _attack_query(seed=7):
    """Attacker-chosen single-lane query: x0 = 1 dominates, so the triangular solve is well conditioned."""
    rng = np.random.default_rng(seed)
    x = np.concatenate([[1.0], rng.uniform(-0.1, 0.1, 255)])
    return x


def _triangular_recover(c, x):
    """c[m] = sum_{i=0..m} x[i] * w_{255-m+i} for m=0..255 (lane 0, other lanes zero)."""
    w = np.zeros(256)
    for m in range(256):
        acc = c[m]
        for i in range(1, m + 1):
            acc -= x[i] * w[255 - m + i]
        w[255 - m] = acc / x[0]
    return w


def test_5c_unmasked_leaks_weights_masked_does_not(cnp_client, nomask_server, wrapper):
    nomask_client = C.make_client(nomask_server, "cnp")
    x = _attack_query()
    ct = wrapper.encrypt_cnp_batch(x.reshape(1, 256))  # lane 0 only; every other lane is zero
    un = wrapper.cnp_decrypt_all(_stub_call(nomask_client, ct, 1, 1).result_ciphertext)
    rec_un = _triangular_recover(un[:256], x)
    err_un = float(np.abs(rec_un - W).max())
    mk = wrapper.cnp_decrypt_all(_stub_call(cnp_client, ct, 1, 1).result_ciphertext)
    rec_mk = _triangular_recover(mk[:256], x)
    err_mk = float(np.abs(rec_mk - W).max())
    corr = float(abs(np.corrcoef(rec_mk, W)[0, 1]))
    NUMBERS["t5c"] = {"unmasked_max_weight_err": err_un, "masked_max_weight_err": err_mk, "masked_abs_corr": corr}
    assert err_un < 1e-6, f"mask OFF: weights should be recovered, max err {err_un}"
    assert err_mk > 1.0 and corr < 0.5, f"mask ON: recovery must fail (err {err_mk}, corr {corr})"


def test_5d_production_cannot_disable_mask(prod_with_off_env_server, wrapper):
    # (i) the switch is not even compiled into the production binary
    prod = C.SERVER_BINARIES["prod"].read_bytes()
    test = C.SERVER_BINARIES["nomask_test"].read_bytes()
    assert b"PPFD_CNP_TEST_MASK_OFF" not in prod
    assert b"PPFD_CNP_TEST_MASK_OFF" in test  # control: the test build does contain it
    # (ii) behaviourally: set the env var anyway -> still masked, attack still fails
    client = C.make_client(prod_with_off_env_server, "cnp")
    x = _attack_query()
    ct = wrapper.encrypt_cnp_batch(x.reshape(1, 256))
    mk = wrapper.cnp_decrypt_all(_stub_call(client, ct, 1, 1).result_ciphertext)
    assert float(np.abs(_triangular_recover(mk[:256], x) - W).max()) > 1.0


# ─────────────────────── 6. no Galois keys, no rotations ───────────────────────
def _status(client):
    from generated import inference_pb2
    return client._stub.GetProvisioningStatus(inference_pb2.ProvisioningStatusRequest(), timeout=5.0)


def test_6_cnp_has_zero_galois_bytes_and_zero_rotations(cnp_client, Xtest):
    from generated import inference_pb2
    before = _status(cnp_client)
    for lo in range(0, 160, 32):
        C.request_batch(cnp_client, Xtest[lo:lo + 32], "cnp")
    st = _status(cnp_client)
    NUMBERS["t6_cnp"] = {"galois_key_bytes": st.galois_key_bytes, "rotation_calls": st.rotation_calls}
    assert st.packing_mode == inference_pb2.CNP and st.state == inference_pb2.PROV_READY
    assert st.galois_key_bytes == 0
    assert st.rotation_calls == before.rotation_calls == 0
    # Provisioning and the rotation canary are refused and change no state.
    code = _rpc_code(lambda: cnp_client._stub.ProvisionGaloisKeys(
        inference_pb2.ProvisionGaloisKeysRequest(galois_keys=(C.ART / "galois_keys_160.bin").read_bytes()), timeout=10))
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    code = _rpc_code(lambda: cnp_client._stub.CanaryCheck(inference_pb2.CanaryRequest(ciphertext=b"x"), timeout=5))
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    after = _status(cnp_client)
    assert after.state == inference_pb2.PROV_READY and after.galois_key_bytes == 0 and after.rotation_calls == 0


def test_6_positive_control_slot_counts_rotations(slot_client, Xtest):
    # The counter must actually move in SLOT mode, else "0" above proves nothing.
    from generated import inference_pb2
    st0 = _status(slot_client)
    assert st0.packing_mode == inference_pb2.SLOT and st0.galois_key_bytes > 1_000_000
    C.request_batch(slot_client, Xtest[:16], "slot")
    st1 = _status(slot_client)
    NUMBERS["t6_slot_rotations_per_request"] = int(st1.rotation_calls - st0.rotation_calls)
    assert st1.rotation_calls - st0.rotation_calls == 8  # hoisted_tree_sum: 8 rotations


# ─────────────────────── mode / lane-count rejection ───────────────────────
def test_cnp_server_rejects_slot_request(cnp_client, wrapper):
    ct = wrapper.encrypt_batch(np.zeros(4096))
    assert _rpc_code(lambda: _stub_call(cnp_client, ct, 16, 0)) == grpc.StatusCode.FAILED_PRECONDITION


def test_slot_server_rejects_cnp_request(slot_client, wrapper):
    ct = wrapper.encrypt_cnp_batch(np.zeros((1, 256)))
    assert _rpc_code(lambda: _stub_call(slot_client, ct, 1, 1)) == grpc.StatusCode.FAILED_PRECONDITION


@pytest.mark.parametrize("n", [0, 33, 64])
def test_cnp_rejects_bad_lane_counts(cnp_client, wrapper, n):
    ct = wrapper.encrypt_cnp_batch(np.zeros((1, 256)))
    assert _rpc_code(lambda: _stub_call(cnp_client, ct, n, 1)) == grpc.StatusCode.INVALID_ARGUMENT


def test_cnp_accepts_32_lanes(cnp_client, Xtest):
    raw, _ = C.request_batch(cnp_client, Xtest[:32], "cnp")
    assert raw.shape == (32,)


def test_invalid_packing_env_fails_closed():
    import subprocess
    env = dict(__import__("os").environ, PPFD_PACKING="bogus")
    r = subprocess.run([str(C.SERVER_BINARIES["prod"]), str(C.ART / "model_weights.bin"), str(C.free_port())],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "PPFD_PACKING" in r.stderr


# ─────────────────────── timing invariant ───────────────────────
def test_timing_invariant_includes_mask_us(cnp_client, slot_client, Xtest):
    from bank_client.bank_client import TIMING_RESIDUAL_BUDGET_US, timing_residual_us
    res = {}
    for name, client, mode, n in (("cnp", cnp_client, "cnp", 32), ("slot", slot_client, "slot", 16)):
        worst = 0
        for lo in range(0, n * 20, n):
            _, resp = C.request_batch(client, Xtest[lo:lo + n], mode)
            t = {f: getattr(resp.timing, f) for f in ("deserialization_us", "multiply_plain_us",
                 "rotation_hoisting_us", "mask_us", "serialization_us", "total_inference_us")}
            worst = max(worst, abs(timing_residual_us(t)))
            if mode == "cnp":
                assert t["rotation_hoisting_us"] == 0 and t["mask_us"] > 0
            else:
                assert t["mask_us"] == 0 and t["rotation_hoisting_us"] > 0
        res[name] = worst
        assert worst <= TIMING_RESIDUAL_BUDGET_US, (name, worst)
    NUMBERS["t_timing_max_residual_us"] = res


# ─────────────────────── record measured numbers ───────────────────────
def test_zz_record_numbers():
    (OUT_DIR / "cnp_a_test_numbers.json").write_text(json.dumps(NUMBERS, indent=2, default=float))
    assert NUMBERS
