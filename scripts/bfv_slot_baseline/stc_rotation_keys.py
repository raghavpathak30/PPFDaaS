#!/usr/bin/env python3
"""Count the SlotToCoeff^FV rotation keys CNP-B needs at each LogN, by set arithmetic only (no HE, no allocation).

Mirrors the structure of the fork at radix 2, keys of the diagonal maps only, not their values:
  genDcdDiabDecomp   ckks_fv/mfv_encoder.go:708-803
  multDiabMats       ckks_fv/mfv_encoder.go:683-706 (keys add mod N/2)
  genDcdMatsRad2     ckks_fv/mfv_encoder.go:646-665
  EncodeDiagMatrixT  ckks_fv/mfv_encoder.go:517-556 (naive below 3 diagonals, else BSGS split at ratio 16)
  findbestbabygiantstepsplit, bsgsIndex  ckks_fv/ckks_encoder.go:557, :410
  GenRotationIndexesForSlotsToCoeffsMat  ckks_fv/keygen.go:378-416
Plus one conjugation key (cnpSetup passes includeConjugate=true, patch/rtf_cnp_setup_test.go:128).

The count is an arithmetic prediction. Validate it against rotation_key_counts.stc_only from a real run before citing
it at a LogN that was not run. Key bytes use the fork's layout: 6 blocks x 2 polys x 28 limbs x N x 8 = 2688 N.
"""
import argparse
import json


def diag_decomp(log_n):
    n = 1 << log_n
    res = [None] * log_n
    res[0] = {0, 1, 2, 3, n // 2 - 1, n // 2 - 2, n // 2 - 3}
    for ind in range(1, log_n - 2):
        s = 1 << ind
        res[ind] = {0, s, 2 * s, n // 2 - s, n // 2 - 2 * s}
    s = n // 4
    res[log_n - 2] = {0, s}
    res[log_n - 1] = {0, s}
    return res


def mult(a, b, n):
    return {(x + y) % (n // 2) for x in a for y in b}


def rad2(log_n):
    n = 1 << log_n
    d = diag_decomp(log_n)
    depth = len(d) - 1
    out = [None] * ((depth + 1) // 2 + 1)
    if depth % 2 == 0:
        for i in range(0, depth - 2, 2):
            out[i // 2] = mult(d[i + 1], d[i], n)
    else:
        out[0] = d[0]
        for i in range(1, depth - 2, 2):
            out[(i + 1) // 2] = mult(d[i + 1], d[i], n)
    out[(depth - 1) // 2] = mult(d[depth - 1], d[depth - 2], n)
    out[(depth + 1) // 2] = mult(d[depth], d[depth - 2], n)
    return out


def bsgs_index(keys, slots, n1):
    index = {}
    for k in keys:
        k &= slots - 1
        index.setdefault(k // n1, []).append(k & (n1 - 1))
    return index


def best_split(keys, max_n, max_ratio=16.0):
    n1 = 1
    while n1 < max_n:
        index = bsgs_index(keys, max_n, n1)
        if len(index.get(0, [])) > 0:
            hoisted, normal = len(index[0]) - 1, len(index) - 1
            if normal == 0:
                return n1 // 2
            if hoisted > normal:
                while hoisted / normal < max_ratio:
                    if normal // 2 == 0:
                        break
                    n1 *= 2
                    hoisted = hoisted * 2 + 1
                    normal = normal // 2
                return n1
        n1 <<= 1
    return 1


def stc_rotations(log_n):
    slots = 1 << log_n
    rots = set()
    for keys in rad2(log_n):
        if len(keys) > 2:
            n1 = best_split(keys, slots)
            index = bsgs_index(keys, slots, n1)
            vec = {n1 * j + i for j, row in index.items() for i in row}
        else:
            vec = {k + slots if k < 0 else k for k in keys}
            n1 = None
        if len(vec) < 3:
            rots |= vec  # keygen.go:388-394 keeps index 0 here if a matrix has it
        else:
            for j in vec:
                a = ((j // n1) * n1) & (slots - 1)
                b = j & (n1 - 1)
                if a:
                    rots.add(a)
                if b:
                    rots.add(b)
    return rots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logn", type=int, nargs="+", default=[10, 12, 14, 16])
    a = ap.parse_args()
    out = {}
    for log_n in a.logn:
        count = len(stc_rotations(log_n)) + 1  # +1: conjugation
        out[log_n] = {"stc_keys_including_conjugation": count, "bytes_each": 2688 * (1 << log_n), "bytes_total": count * 2688 * (1 << log_n)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
