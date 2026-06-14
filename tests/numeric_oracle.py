"""Phase 0.4 numeric oracle — standing regression guard for the slot-reduction fold.

This is a pure-NumPy model of the two candidate "rotate-and-add" folds used by
``hoisted_tree_sum`` (vendor_server/src/rotation_hoisting.cpp):

  * BROKEN  (pre-Phase-0.1): rotations of the ORIGINAL ciphertext ``ct`` are
    accumulated -- ``acc = acc + rot(ct, s)``. This sums only the 9-element
    subset of slots {0,1,2,4,8,16,32,64,128} of each window, not all 256.

  * CORRECT (Phase 0.1 fix): rotations of the ACCUMULATOR are taken --
    ``acc2 = acc2 + rot(acc2, s)``. By induction this is a doubling fold that,
    after steps {1,2,4,8,16,32,64,128}, sums all 256 consecutive slots of each
    window: ``acc2[i] == sum(ct[i:i+256])`` (cyclically wrapped).

Run directly:  python3 tests/numeric_oracle.py
Run under pytest: pytest tests/numeric_oracle.py
"""

import numpy as np


def rot(v: np.ndarray, s: int) -> np.ndarray:
    """SEAL rotate_vector(v, s): cyclic left-rotation by s slots."""
    return np.roll(v, -s)


def broken_fold(ct: np.ndarray, steps=(1, 2, 4, 8, 16, 32, 64, 128)) -> np.ndarray:
    """Pre-Phase-0.1 fold: accumulate rotations of the ORIGINAL ciphertext."""
    acc = ct.copy()
    for s in steps:
        acc = acc + rot(ct, s)
    return acc


def correct_fold(ct: np.ndarray, steps=(1, 2, 4, 8, 16, 32, 64, 128)) -> np.ndarray:
    """Phase 0.1 fold: accumulate rotations of the ACCUMULATOR (sequential)."""
    acc = ct.copy()
    for s in steps:
        acc = acc + rot(acc, s)
    return acc


def test_numeric_oracle():
    rng = np.random.default_rng(0)
    ct = rng.normal(size=4096)

    acc = broken_fold(ct)
    acc2 = correct_fold(ct)

    true_sum = ct[:256].sum()

    # The corrected fold's slot 0 must equal the true 256-element window sum.
    assert np.isclose(acc2[0], true_sum)

    # The broken fold's slot 0 must NOT equal the true 256-element window sum
    # -- it only ever sums the 9-element subset {0,1,2,4,8,16,32,64,128}.
    assert not np.isclose(acc[0], true_sum)


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    ct = rng.normal(size=4096)

    acc = broken_fold(ct)
    acc2 = correct_fold(ct)
    true_sum = ct[:256].sum()

    print("broken  fold acc[0]  =", acc[0])
    print("correct fold acc2[0] =", acc2[0])
    print("true 256-element sum =", true_sum)

    assert np.isclose(acc2[0], true_sum), "correct fold must match true 256-sum"
    assert not np.isclose(acc[0], true_sum), "broken fold must NOT match true 256-sum"

    print("PASS: correct fold matches the 256-element oracle; broken fold does not.")
