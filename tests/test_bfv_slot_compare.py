"""Pure-logic tests for scripts/bfv_slot_baseline/run_laptop_compare.py: no Go, no HE, no timing."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bfv_slot_baseline/run_laptop_compare.py"
spec = importlib.util.spec_from_file_location("run_laptop_compare", SCRIPT)
rlc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rlc)


def _supply(root, name, **files):
    d = root / name
    d.mkdir()
    for k, v in files.items():
        (d / k).write_text(v + "\n")


def test_power_ac_when_any_supply_online(tmp_path):
    _supply(tmp_path, "ADP0", online="1")
    _supply(tmp_path, "BAT0", status="Charging")
    assert rlc.read_power(tmp_path)["power_source"] == "AC"


def test_power_battery_when_nothing_online_and_discharging(tmp_path):
    _supply(tmp_path, "ADP0", online="0")
    _supply(tmp_path, "USBC", online="0")
    _supply(tmp_path, "BAT0", status="Discharging")
    assert rlc.read_power(tmp_path)["power_source"] == "battery"


def test_power_unknown_is_not_treated_as_ac(tmp_path):
    _supply(tmp_path, "ADP0", online="0")
    assert rlc.read_power(tmp_path)["power_source"] == "unknown"
    assert rlc.read_power(tmp_path / "missing")["power_source"] == "unknown"


def test_require_ac_stops_on_battery(monkeypatch):
    monkeypatch.setattr(rlc, "read_power", lambda: {"power_source": "battery"})
    with pytest.raises(rlc.StopRun):
        rlc.require_ac("test")


@pytest.mark.parametrize("logn", [13, 14, 16])
def test_refuses_logn_above_12(logn):
    with pytest.raises(SystemExit):
        rlc.check_logn([10, logn])


def test_allows_toy_logn():
    rlc.check_logn([10, 12])


def test_battery_at_start_writes_stop_record_and_measures_nothing(tmp_path, monkeypatch):
    (tmp_path / "meta.json").write_text("{}")
    monkeypatch.setattr(rlc, "OUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(rlc, "read_power", lambda: {"power_source": "battery"})
    monkeypatch.setattr(rlc, "machine_state", lambda: {"power_source": "battery"})
    assert rlc.main(["--data", str(tmp_path), "--logn", "10"]) == 3
    produced = list((tmp_path / "out").iterdir())
    assert len(produced) == 1 and produced[0].name.startswith("STOPPED_")
    assert "battery" in json.loads(produced[0].read_text())["stopped"]


def test_gate_and_mode_env_cannot_leak_from_the_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("CNP_HEADROOM_GATE_BITS", "0")
    monkeypatch.setenv("SLOT_EVAL_LEVEL", "0")
    monkeypatch.setenv("CNP_SCALEUP", "fvscaleup")
    monkeypatch.setattr(rlc, "git", lambda *a: "sha")
    env = rlc.arm_env("slot", "hera", 10, 5, tmp_path, "strat", tmp_path)
    for k in ("CNP_HEADROOM_GATE_BITS", "SLOT_EVAL_LEVEL", "CNP_SCALEUP"):
        assert k not in env
    assert env["GOMEMLIMIT"] == "11GiB"


def test_arm_env_selects_the_existing_cnp_arms(monkeypatch, tmp_path):
    monkeypatch.setattr(rlc, "git", lambda *a: "sha")
    assert rlc.arm_env("cnp", "hera", 10, 5, tmp_path, "strat", tmp_path)["CNP_ARMS"] == "cnp"
    assert rlc.arm_env("halfboot", "hera", 10, 5, tmp_path, "strat", tmp_path)["CNP_ARMS"] == "cnp baseline"
    assert "CNP_ARMS" not in rlc.arm_env("slot", "hera", 10, 5, tmp_path, "strat", tmp_path)


def _stage(ms):
    return {"median_ms": ms}


def test_cnp_row_amortises_crypt_over_outputs_and_lanes():
    d = {"cnp_stages": {"keystream_crypt (offline)": _stage(160.0), "stc_modswitch (offline)": _stage(5.0)},
         "outputs_per_crypt": 16, "lanes_per_output_ct": 4, "per_output_ct_ms": {"total": 2.0},
         "correctness": {"max_abs_integer_diff": 0, "bit_exact": True}, "headroom_gate": "passed", "vm_hwm_kb_end": 100}
    r = rlc.arm_row("cnp", d, None, 10, 3)
    assert r["offline_per_ct_ms"] == 15.0  # 160/16 + 5
    assert r["online_per_ct_ms"] == 2.0
    assert r["offline_per_txn_ms"] == 15.0 / 4
    assert r["total_per_txn_ms"] == 17.0 / 4
    assert r["lanes_per_crypt"] == 64
    assert r["rotation_key_bytes"] == 30


def test_halfboot_row_uses_the_lower_bound_online():
    d = {"cnp_stages": {"keystream_crypt (offline)": _stage(160.0), "stc_modswitch (offline)": _stage(5.0)},
         "outputs_per_crypt": 16, "lanes_per_output_ct": 4, "per_output_ct_ms": {"total": 2.0},
         "baseline_lower_bound_per_output_ct_ms": {"total": 40.0},
         "baseline_halfboot_stages": {"_max_abs_error_check": _stage(1e-5)}, "vm_hwm_kb_end": 100}
    r = rlc.arm_row("halfboot", d, None, 10, 3)
    assert r["online_per_ct_ms"] == 40.0
    assert r["total_per_txn_ms"] == 55.0 / 4
    assert r["max_abs_error_ckks_2x_plus_1"] == 1e-5
