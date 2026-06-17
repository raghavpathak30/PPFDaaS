import sys, json

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / 'artifacts'

def run_dispatch():
    COMPILER_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(COMPILER_DIR))

    print('=== AUC DISPATCH: Running Depth-1 LR surrogate ===')
    from train_logistic_regression import validate_and_gate
    import numpy as np
    X_test = np.load(ARTIFACTS_DIR / 'X_test.npy')
    y_test = np.load(ARTIFACTS_DIR / 'y_test.npy')
    weights = np.load(ARTIFACTS_DIR / 'weights.npy')
    import struct
    with open(ARTIFACTS_DIR / 'model_weights.bin', 'rb') as f:
        f.read(4)
        bias, = struct.unpack('<d', f.read(8))
    _, auc = validate_and_gate(weights, bias, X_test, y_test)

    result = {'depth1_auc': auc, 'active_path': None, 'degree2_auc': None}

    if auc >= 0.94:
        print(f'[DISPATCH] Depth-1 AUC={auc:.4f} >= 0.94 → PRIMARY PATH ACTIVE')
        result['active_path'] = 'depth1'

    elif auc >= 0.92:
        print(f'[DISPATCH] Depth-1 AUC={auc:.4f} in [0.92,0.94) → BORDERLINE')
        print('[DISPATCH] Recommendation: increase SMOTE sampling_strategy to 0.3')
        print('[DISPATCH] Re-run train_xgboost.py with adjusted SMOTE, then retry.')
        print('[DISPATCH] Proceeding with Depth-1 (borderline acceptable).')
        result['active_path'] = 'depth1_borderline'

    else:
        print(f'[DISPATCH] CRITICAL: Depth-1 AUC={auc:.4f} < 0.92')
        print('[DISPATCH] Activating Degree-2 fallback path...')
        from degree2_linearizer import linearize_degree2
        d2_auc = linearize_degree2(str(ARTIFACTS_DIR))
        result['degree2_auc']  = d2_auc
        result['active_path']  = 'degree2'
        print(f'[DISPATCH] Degree-2 AUC={d2_auc:.4f}')
        if d2_auc < 0.96:
            raise AssertionError(f'Degree-2 AUC {d2_auc:.4f} < 0.96 threshold')
        print()
        print('=== ACTION REQUIRED (Degree-2 Fallback Activated) ===')
        print('  1. Rebuild vendor_server with ckks_context_depth2.cpp')
        print('  2. Regenerate SEAL keys for n=16384')
        print('  3. Update gRPC max_message_size to 3 MB (both ends)')
        print('  4. Use FeaturePipelineDegree2 in FastAPI main.py')
        print('  5. Re-run all Phase 1.0 smoke tests with new parameters')
        print('=======================================================')

    with open(ARTIFACTS_DIR / 'dispatch_result.json', 'w') as f:
        json.dump(result, f, indent=2)
    print(f"[DISPATCH] Result saved → {ARTIFACTS_DIR / 'dispatch_result.json'}")
    print(f"[DISPATCH] Active path: {result['active_path']}")
    return result

if __name__ == '__main__':
    r = run_dispatch()
    sys.exit(0 if r['active_path'] in ('depth1','depth1_borderline','degree2') else 1)
 