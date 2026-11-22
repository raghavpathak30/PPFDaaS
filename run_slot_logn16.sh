#!/usr/bin/env bash
# run_slot_logn16.sh: rounds 1 and 2 of docs/DGX_RUNBOOK_slot.md (slot, then CNP-B, LogN 16),
# unattended, on ONE socket (NUMA node 0, CPUs 0-19), on lnmdgx1. Untracked helper; not part of the repo.
#
# Order (sequential, same socket, same image, one session):
#   round 1: slot hera -> slot rubato      (runbook ESTIMATE ~1 h 15 min + ~30 min)
#   round 2: cnp  hera -> cnp  rubato      (runbook MEASURED 1 h 26 min + 47 min on 28 Oct)
#   Expect ~4 h of machine time plus the image build. Differs from the runbook, which runs
#   HERA on socket 0 and Rubato on socket 1 in parallel.
#
# Run from the root of a clean clone of the repo on lnmdgx1 (copy this file in; it stays untracked):
#   EXPECT_SHA=<40-hex commit you cloned> ./run_slot_logn16.sh                 # foreground
#   EXPECT_SHA=<sha> nohup setsid ./run_slot_logn16.sh </dev/null >/dev/null 2>&1 &   # survives logout
# Done when ~/dgx_results/slot-logn16-s0/FINAL_STATUS exists. Full log: .../run.log
#
# Exit codes (also written to $OUT/exit_code):
#   0 every job passed every gate      1 a job or correctness gate failed (later jobs not started)
#   2 preflight / configuration error  3 stopped: another job contended for socket 0
#   4 socket 0 never became idle       130 interrupted (container stopped)
#
# Required:  EXPECT_SHA   full SHA of the checkout; HEAD must equal it and tracked files must be clean.
# Optional (defaults are the runbook's):
#   DATA_DIR=~/cnpb_data            unpacked cnpb_data.tar.gz; all 7 sha256 sums are checked
#   OUT=~/dgx_results/slot-logn16-s0   must not exist or be empty
#   REPS=5  MEM_LIMIT=              (unset = no --memory limit, as in the 28 Oct runs)
#   IDLE_MAX_BUSY=10                socket 0 must have fewer busy threads than this before each job
#   IDLE_WAIT_MIN=120               how long to wait for that before giving up (exit 4)
#   MIN_NODE0_FREE_GB=70            node 0 MemFree+FilePages required before each job
#   CONTENTION_MAX_BUSY=30          during a job (ours uses ~20 of socket 0's 40 threads)
#   CONTENTION_STRIKES=5            consecutive ~60 s samples above that before acting
#   CONTENTION_ACTION=stop          stop (runbook rule 7) | record
#   AUC_TOL=0.001                   |delta ROC-AUC| gate (docs/cnp_gate.md:8)
#   REQUIRE_FULL_ROWS=0             1 = a STRATIFIED row set fails the job (runbook: passes, but report it)
#   JOB_PREFIX=l16                  container/job names: <prefix>_<arm>_<cipher>_s0
#   EXPECT_HOST=lnmdgx1  EXPECT_NODE0_CPUS=0-19,40-59  EXPECT_NODE1_CPUS=20-39,60-79
#
# Every result JSON (slot_<c>.json, cnp_<c>.json) is rewritten with three added keys:
#   machine_state  : the wrapper's machine_state_{start,end}.json, the bench/main.go MachineState
#                    fields read just before and after the job, and the socket-load samples
#   param_state    : the harness's own (slot), or for cnp copied from the same-session slot JSON of the
#                    same cipher (both build params via cnpConfig, rtf_cnp_setup_test.go:72), cross-checked
#   run_slot_logn16: script sha256, repo SHA, image id, job order, gate verdicts
# The harness's original bytes are kept next to it as <name>.harness.json (sha256 in SHA256SUMS).
#
# Written for bash 4.2 and python3 >= 3.6 stdlib only (RHEL 7.9; no numpy on the host).

set -euo pipefail
umask 022

# ---------------------------------------------------------------- configuration
EXPECT_SHA="${EXPECT_SHA:-}"
EXPECT_HOST="${EXPECT_HOST:-lnmdgx1}"
EXPECT_NODE0_CPUS="${EXPECT_NODE0_CPUS:-0-19,40-59}"
EXPECT_NODE1_CPUS="${EXPECT_NODE1_CPUS-20-39,60-79}"   # set empty to skip (tests only)
DATA_DIR="${DATA_DIR:-${HOME}/cnpb_data}"
OUT="${OUT:-${HOME}/dgx_results/slot-logn16-s0}"
REPS="${REPS:-5}"
MEM_LIMIT="${MEM_LIMIT:-}"
IDLE_MAX_BUSY="${IDLE_MAX_BUSY:-10}"
IDLE_WAIT_MIN="${IDLE_WAIT_MIN:-120}"
MIN_NODE0_FREE_GB="${MIN_NODE0_FREE_GB:-70}"
CONTENTION_MAX_BUSY="${CONTENTION_MAX_BUSY:-30}"
CONTENTION_STRIKES="${CONTENTION_STRIKES:-5}"
CONTENTION_ACTION="${CONTENTION_ACTION:-stop}"
AUC_TOL="${AUC_TOL:-0.001}"
REQUIRE_FULL_ROWS="${REQUIRE_FULL_ROWS:-0}"
JOB_PREFIX="${JOB_PREFIX:-l16}"
SAMPLE_S="${SAMPLE_S:-30}"            # socket-load sample window (runbook: 30 s)
PROGRESS_EVERY_S="${PROGRESS_EVERY_S:-900}"

NODE=0
CPUS="0-19"                          # physical cores of socket 0, as in every earlier DGX job
EXPECT_ROWS=56962

# Anything below would silently change the run relative to the runbook / the 28 Oct runs.
unset GOGC GOMEMLIMIT CNP_ARMS CNP_ROWS CNP_BUDGET_S CNP_STC_MODE CNP_SCALEUP CNP_HEADROOM_GATE_BITS \
      SLOT_EVAL_LEVEL LOGN PARAM_INDEX FORCE WAIT RESULTS_ROOT IMAGE CIPHER || true

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${REPO}/$(basename "${BASH_SOURCE[0]}")"
WRAPPER="${REPO}/scripts/dgx/run_transcipher.sh"

# arm cipher, in run order
JOBS=("slot hera" "slot rubato" "cnp hera" "cnp rubato")
job_name() { echo "${JOB_PREFIX}_$1_$2_s0"; }

# Data hashes: docs/DGX_RUNBOOK_slot.md section 2.
DATA_SHA256='5e6df6eafaeca093767f82e2f18622e7535c3911924b0955acd5692dbb0c5a4d  expected.i64
98001b75879558ac9ae483144a5087c34b14a3d3c05f78ef29c9edfa531aee46  meta.json
6621b19f7cc6785c330812143302e81b46d1b941b49cf25c374c6f7b602cd64b  plain_logit.f64
0fb6a8b686e7f23b9870c9590240a731da4932d8727148efda518a402f1f4400  strat4096.i32
718327abe85e822ec412c7174e3449048a8a751196ec91490b07c8939cdb733a  wq.i16
a83ad5f54ee185fa1300acc459798e8a3255f7b7b4b1923a0c85e1b05876eef7  xq.i16
c80d24946b6334dfcd600a652351f56ad0ed9dfc69fc952b319c2e708c1207d4  y.u8'

# ---------------------------------------------------------------- helpers
die_early() { echo "run_slot_logn16: $*" >&2; exit 2; }   # before $OUT/run.log exists
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*"; }

FINAL_WRITTEN=0
finish_run() {  # $1 exit code, $2 status line
  local rc="$1" msg="$2"
  [[ "${FINAL_WRITTEN}" == 1 ]] && return 0
  FINAL_WRITTEN=1
  python3 "${HELPER}" summary "${OUT}" "${rc}" "${msg}" >/dev/null 2>&1 || log "WARN: summary.json not written"
  ( cd "${OUT}" && find . -type f ! -name SHA256SUMS ! -name run.log ! -name .lock -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS ) || true
  echo "${rc}" > "${OUT}/exit_code"
  printf '%s %s\n' "$([[ ${rc} == 0 ]] && echo PASS || echo FAIL)" "${msg}" > "${OUT}/FINAL_STATUS"
  log "FINAL: exit ${rc}: ${msg}"
  if [[ "${rc}" == 0 ]]; then
    local tgz; tgz="$(dirname "${OUT}")/$(basename "${OUT}")_$(date -u +%Y%m%d).tar.gz"
    tar -C "$(dirname "${OUT}")" -czf "${tgz}" "$(basename "${OUT}")" && log "hand-back: ${tgz} sha256 $(sha256sum "${tgz}" | cut -d' ' -f1)"
  fi
}
fail() { local rc="$1"; shift; log "FAIL: $*"; finish_run "${rc}" "$*"; exit "${rc}"; }

# Busy threads per socket over SAMPLE_S seconds (runbook section 0). Prints "<socket0> <socket1>".
socket_busy() {
  local a b
  a="$(grep '^cpu[0-9]' /proc/stat)"; sleep "${SAMPLE_S}"; b="$(grep '^cpu[0-9]' /proc/stat)"
  printf '%s\n%s\n' "${a}" "${b}" | awk '{ id=substr($1,4)+0; idle=$5+$6; tot=0; for(i=2;i<=9;i++) tot+=$i
    if (id in T) { d=tot-T[id]; s=(id<20||(id>=40&&id<60))?0:1; if (d>0) busy[s]+=1-(idle-I[id])/d }
    T[id]=tot; I[id]=idle }
    END { printf "%.1f %.1f\n", busy[0]+0, busy[1]+0 }'
}
node_free_gb() {  # MemFree + FilePages of node $1, in GB (1e6 kB)
  awk '/MemFree|FilePages/ {s+=$(NF-1)} END {printf "%.1f\n", s/1e6}' "/sys/devices/system/node/node$1/meminfo"
}
others_hot_on_socket0() {  # other users' processes >400% CPU whose last CPU is on socket 0
  ps -eo user:32,pcpu,psr,pid,comm --no-headers 2>/dev/null | awk -v me="$(id -un)" \
    '$1!=me && $2+0>400 && ($3<20 || ($3>=40 && $3<60)) {print}'
}

# ---------------------------------------------------------------- static preflight (no side effects)
[[ -n "${EXPECT_SHA}" ]] || die_early "set EXPECT_SHA to the full 40-hex SHA of the commit you cloned"
[[ "${EXPECT_SHA}" =~ ^[0-9a-f]{40}$ ]] || die_early "EXPECT_SHA must be 40 lowercase hex characters"
[[ "$(hostname)" == "${EXPECT_HOST}" ]] || die_early "host is $(hostname), expected ${EXPECT_HOST}"
for c in docker git awk sha256sum python3 flock tar setsid; do
  command -v "${c}" >/dev/null || die_early "missing command: ${c}"
done
python3 -c 'import sys, json, array; assert sys.version_info >= (3, 6); assert sys.byteorder == "little"' \
  || die_early "python3 >= 3.6 (stdlib json, array) on a little-endian host is required"
id -nG | tr ' ' '\n' | grep -qx docker || die_early "$(id -un) is not in the docker group (ask the admin; no sudo)"
docker info >/dev/null 2>&1 || die_early "docker info failed (daemon unreachable or no permission)"
[[ -x "${WRAPPER}" ]] || die_early "${WRAPPER} missing or not executable; run from the repo root"
grep -qE '^[[:space:]]+slot\)' "${WRAPPER}" || die_early "wrapper has no slot target: checkout predates the slot baseline"
HEAD_SHA="$(cd "${REPO}" && git rev-parse HEAD)"   # no git -C: RHEL 7 ships git 1.8.3
[[ "${HEAD_SHA}" == "${EXPECT_SHA}" ]] || die_early "HEAD is ${HEAD_SHA}, EXPECT_SHA is ${EXPECT_SHA}"
[[ -z "$(cd "${REPO}" && git status --porcelain --untracked-files=no)" ]] || die_early "tracked files are modified; the image label would not describe the code"
[[ "$(cat /sys/devices/system/node/node0/cpulist)" == "${EXPECT_NODE0_CPUS}" ]] || die_early "node0 cpulist is not ${EXPECT_NODE0_CPUS}"
if [[ -n "${EXPECT_NODE1_CPUS}" ]]; then
  [[ "$(cat /sys/devices/system/node/node1/cpulist 2>/dev/null)" == "${EXPECT_NODE1_CPUS}" ]] || die_early "node1 cpulist is not ${EXPECT_NODE1_CPUS}"
fi
[[ -f "${DATA_DIR}/meta.json" ]] || die_early "DATA_DIR=${DATA_DIR} has no meta.json (copy and unpack cnpb_data.tar.gz)"
( cd "${DATA_DIR}" && printf '%s\n' "${DATA_SHA256}" | sha256sum -c --quiet - ) || die_early "input sha256 mismatch in ${DATA_DIR}"
EXISTING="$(docker ps -a --format '{{.Names}}')"
for j in "${JOBS[@]}"; do
  n="$(job_name ${j})"
  ! printf '%s\n' "${EXISTING}" | grep -qx "${n}" || die_early "a container named ${n} already exists"
done
case "${CONTENTION_ACTION}" in stop|record) ;; *) die_early "CONTENTION_ACTION must be stop|record" ;; esac
if [[ -e "${OUT}" && -n "$(ls -A "${OUT}" 2>/dev/null)" ]]; then die_early "${OUT} is not empty; never overwrite results"; fi

# ---------------------------------------------------------------- output dir, log, lock
mkdir -p "${OUT}/meta"
exec 9>"${OUT}/.lock"
flock -n 9 || die_early "another run holds ${OUT}/.lock"
exec > >(tee -a "${OUT}/run.log") 2>&1
HELPER="${OUT}/meta/runner_helper.py"
SHA7="${HEAD_SHA:0:7}"
IMAGE="ppfdaas-transcipher-bench:${SHA7}"
CUR_JOB=""
WRAPPER_PID=""

on_signal() {
  log "signal received"
  if [[ -n "${CUR_JOB}" ]]; then docker stop "${CUR_JOB}" >/dev/null 2>&1 || true; fi
  if [[ -n "${WRAPPER_PID}" ]]; then wait "${WRAPPER_PID}" 2>/dev/null || true; fi
  finish_run 130 "interrupted during ${CUR_JOB:-setup}; container stopped"
  exit 130
}
trap on_signal INT TERM HUP

# ---------------------------------------------------------------- python helper (stdlib only)
cat > "${HELPER}" <<'PY'
import array, json, os, sys, time, hashlib

def jload(p):
    with open(p) as f:
        return json.load(f)

def jdump(o, p):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(o, f, indent=1, sort_keys=False)
        f.write("\n")
    os.rename(tmp, p)

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def rd(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except (IOError, OSError):
        return None

def go_machine_state(cpus):
    """Field-for-field the MachineState of tools/transciphering/bench/main.go (readMachineState),
    plus scaling_cur_mhz over the pinned cpuset only (extra, labelled)."""
    ms = {"cpu_governor": rd("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "",
          "power_source": "unknown", "load_avg_1": 0.0, "load_avg_5": 0.0, "load_avg_15": 0.0,
          "cpu_count": 0, "scaling_cur_mhz_min": 0.0, "scaling_cur_mhz_max": 0.0}
    la = (rd("/proc/loadavg") or "").split()
    if len(la) >= 3:
        ms["load_avg_1"], ms["load_avg_5"], ms["load_avg_15"] = float(la[0]), float(la[1]), float(la[2])
    base = "/sys/class/power_supply"
    if os.path.isdir(base):
        ac_found = ac_online = discharging = False
        for e in os.listdir(base):
            on = rd(os.path.join(base, e, "online"))
            if on is not None:
                ac_found = True
                ac_online = ac_online or on == "1"
            if rd(os.path.join(base, e, "status")) == "Discharging":
                discharging = True
        if ac_found and ac_online:
            ms["power_source"] = "AC"
        elif discharging:
            ms["power_source"] = "battery"
        elif ac_found:
            ms["power_source"] = "AC"
    def mhz(i):
        v = rd("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i)
        return None if v is None else float(v) / 1000.0
    vals, i = [], 0
    while True:
        p = "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i
        if not os.path.exists(p):
            break
        ms["cpu_count"] += 1
        v = mhz(i)
        if v is not None:
            vals.append(v)
        i += 1
    if vals:
        ms["scaling_cur_mhz_min"], ms["scaling_cur_mhz_max"] = min(vals), max(vals)
    pinned = []
    for part in cpus.split(","):
        a, _, b = part.partition("-")
        pinned.extend(range(int(a), int(b or a) + 1))
    pv = [v for v in (mhz(c) for c in pinned) if v is not None]
    ms["extra_pinned_cpus"] = cpus
    ms["extra_scaling_cur_mhz_min_pinned"] = min(pv) if pv else None
    ms["extra_scaling_cur_mhz_max_pinned"] = max(pv) if pv else None
    ms["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ms

def read_arr(path, code, size):
    a = array.array(code)
    assert a.itemsize == size, "array %s itemsize %d != %d" % (code, a.itemsize, size)
    with open(path, "rb") as f:
        a.frombytes(f.read())
    return a

def roc_auc(y, s):
    """Mann-Whitney with average ranks for ties; equals sklearn roc_auc_score."""
    n = len(s)
    order = sorted(range(n), key=lambda i: s[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and s[order[j + 1]] == s[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    n1 = sum(1 for v in y if v)
    n0 = n - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (sum(ranks[i] for i in range(n) if y[i]) - n1 * (n1 + 1) / 2.0) / (n1 * n0)

def gate(job_dir, arm, cipher, data_dir, auc_tol, require_full, expect_rows, contention_flag):
    checks, warns = [], []
    def chk(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
    ec = rd(os.path.join(job_dir, "exit_code"))
    chk("exit_code == 0", ec == "0", "exit_code=%s" % ec)
    chk("not stopped for contention", not os.path.exists(contention_flag), "")
    logtxt = rd(os.path.join(job_dir, "container.log")) or ""
    test = "TestSlotRun" if arm == "slot" else "TestCNPRun"
    chk("container.log has --- PASS: " + test, ("--- PASS: " + test) in logtxt)
    bad = [m for m in ("--- FAIL", "panic:", "STOPPED", "bit-exactness violated") if m in logtxt]
    chk("container.log has no FAIL/panic/STOPPED/bit-exactness violation", not bad, ",".join(bad))
    jp = os.path.join(job_dir, "%s_%s.json" % (arm, cipher))
    res = None
    try:
        res = jload(jp)
        chk("result JSON parses", True, os.path.basename(jp))
    except Exception as e:
        chk("result JSON parses", False, repr(e))
    meta = jload(os.path.join(data_dir, "meta.json"))
    host = {}
    if res is not None:
        g = str(res.get("headroom_gate", ""))
        chk("headroom_gate passed", g.startswith("passed"), g[:160])
        ps = res.get("param_state") or {}
        logn = ps.get("log_n") if arm == "slot" else res.get("log_n")
        pset = ps.get("param_set") if arm == "slot" else res.get("param_set")
        chk("LogN == 16", logn == 16, "log_n=%s" % logn)
        chk("param_set == 128af", pset == "128af", "param_set=%s" % pset)
        chk("plain_modulus == meta t", res.get("plain_modulus") == meta["t"], "%s vs %s" % (res.get("plain_modulus"), meta["t"]))
        corr = res.get("correctness") or {}
        if cipher == "hera":
            chk("harness: HERA bit_exact", corr.get("bit_exact") is True, "bit_exact=%s" % corr.get("bit_exact"))
        rs = str(res.get("row_set", ""))
        full = rs.startswith("full test set")
        if not full:
            msg = "row_set is not the full test set: " + rs[:120]
            if require_full:
                chk("full test set", False, msg)
            else:
                warns.append(msg)
    rows_p = os.path.join(job_dir, "%s_%s_rows.i32" % (arm, cipher))
    dec_p = os.path.join(job_dir, "%s_%s_dec.i64" % (arm, cipher))
    if os.path.exists(rows_p) and os.path.exists(dec_p):
        rows = read_arr(rows_p, "i", 4)
        dec = read_arr(dec_p, "q", 8)
        chk("rows/dec same non-zero length", len(rows) == len(dec) and len(rows) > 0, "%d vs %d" % (len(rows), len(dec)))
        if len(rows) == len(dec) and len(rows) > 0:
            y_all = bytearray(open(os.path.join(data_dir, "y.u8"), "rb").read())
            plain_all = read_arr(os.path.join(data_dir, "plain_logit.f64"), "d", 8)
            exp_all = read_arr(os.path.join(data_dir, "expected.i64"), "q", 8)
            y = [y_all[r] for r in rows]
            plain = [plain_all[r] for r in rows]
            exp = [exp_all[r] for r in rows]
            logit = [v / 16384.0 for v in dec]
            diff = [abs(a - b) for a, b in zip(dec, exp)]
            a_enc, a_pl = roc_auc(y, logit), roc_auc(y, plain)
            host = {"rows": len(rows), "frauds": sum(y), "rows_differing_from_integer_simulation": sum(1 for d in diff if d),
                    "max_abs_integer_diff": max(diff), "max_abs_logit_error": max(abs(a - b) for a, b in zip(logit, plain)),
                    "roc_auc_encrypted": a_enc, "roc_auc_plaintext_float": a_pl, "delta_roc_auc": a_enc - a_pl,
                    "note": "stdlib re-implementation of scripts/cnp_b/score.py's checks for the on-host gate; "
                            "the citable score is score.py run on a host with numpy/scikit-learn"}
            if res is not None and str(res.get("row_set", "")).startswith("full test set"):
                chk("full set has %d rows" % expect_rows, len(rows) == expect_rows, "%d" % len(rows))
            if cipher == "hera":
                chk("host: HERA dec == expected.i64 on every row", host["rows_differing_from_integer_simulation"] == 0,
                    "%d rows differ" % host["rows_differing_from_integer_simulation"])
            chk("host: |delta ROC-AUC| <= %g" % auc_tol, abs(host["delta_roc_auc"]) <= auc_tol, "%.3e" % host["delta_roc_auc"])
    else:
        chk("rows/dec files exist", False, "missing %s or %s" % (os.path.basename(rows_p), os.path.basename(dec_p)))
    ok = all(c["passed"] for c in checks)
    rep = {"job_dir": job_dir, "arm": arm, "cipher": cipher, "passed": ok, "checks": checks, "warnings": warns,
           "host_score": host, "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    jdump(rep, os.path.join(job_dir, "gate_check.json"))
    for c in checks:
        print("  [%s] %s %s" % ("ok" if c["passed"] else "FAIL", c["check"], c["detail"]))
    for w in warns:
        print("  [WARN] " + w)
    return 0 if ok else 1

def samples(job_dir):
    p = os.path.join(job_dir, "socket_load.jsonl")
    out = []
    if os.path.exists(p):
        with open(p) as f:
            out = [json.loads(l) for l in f if l.strip()]
    b0 = sorted(s["busy_socket0"] for s in out)
    return {"file": "socket_load.jsonl", "n": len(out),
            "busy_socket0_max": b0[-1] if b0 else None, "busy_socket0_median": b0[len(b0) // 2] if b0 else None,
            "busy_socket1_max": max(s["busy_socket1"] for s in out) if out else None}

def enrich(out_dir, job_dir, arm, cipher, run_ctx_json):
    jp = os.path.join(job_dir, "%s_%s.json" % (arm, cipher))
    if not os.path.exists(jp):
        print("  no result JSON to enrich")
        return 1
    raw = jp[:-5] + ".harness.json"
    if not os.path.exists(raw):
        os.rename(jp, raw)
    res = jload(raw)
    added = {}
    def put(k, v):
        if k in res:
            k = k + "_runner"
        res[k] = v
        added[k] = True
    ms = {"source": "wrapper machine_state_{start,end}.json (scripts/dgx/run_transcipher.sh) + bench/main.go MachineState fields read by this runner",
          "wrapper_start": jload(os.path.join(job_dir, "machine_state_start.json")) if os.path.exists(os.path.join(job_dir, "machine_state_start.json")) else None,
          "wrapper_end": jload(os.path.join(job_dir, "machine_state_end.json")) if os.path.exists(os.path.join(job_dir, "machine_state_end.json")) else None,
          "go_struct_start": jload(os.path.join(job_dir, "machine_state_go_start.json")) if os.path.exists(os.path.join(job_dir, "machine_state_go_start.json")) else None,
          "go_struct_end": jload(os.path.join(job_dir, "machine_state_go_end.json")) if os.path.exists(os.path.join(job_dir, "machine_state_go_end.json")) else None,
          "socket_load": samples(job_dir)}
    put("machine_state", ms)
    rc = 0
    if "param_state" in res:
        res["param_state_source"] = "harness (e2eParamState, rtf_e2e_test.go:44; filled at rtf_slot_test.go:541)"
    else:
        src = None
        for d in sorted(os.listdir(out_dir)):
            cand = os.path.join(out_dir, d, "slot_%s.json" % cipher)
            if d.endswith("_slot_%s_s0" % cipher) and os.path.exists(cand):
                src = cand
        if src is None:
            put("param_state", None)
            res["param_state_source"] = "NOT RECORDED: the CNP harness writes no param_state and no same-session slot JSON for this cipher exists"
            rc = 1
        else:
            s = jload(src)
            ps = s.get("param_state") or {}
            pairs = [("cipher", ps.get("cipher"), res.get("cipher")), ("param_set", ps.get("param_set"), res.get("param_set")),
                     ("log_n", ps.get("log_n"), res.get("log_n")), ("n", s.get("n"), res.get("n")),
                     ("plain_modulus", s.get("plain_modulus"), res.get("plain_modulus")), ("q0_bits", s.get("q0_bits"), res.get("q0_bits"))]
            bad = [p[0] for p in pairs if p[1] != p[2]]
            if bad:
                put("param_state", None)
                res["param_state_source"] = "NOT RECORDED: same-session slot JSON disagrees on " + ",".join(bad)
                rc = 1
            else:
                put("param_state", ps)
                res["param_state_source"] = ("copied from %s: both harnesses build parameters with cnpConfig "
                    "(rtf_cnp_setup_test.go:72; slot via slotSetup, rtf_slot_test.go:111); cross-checked fields: %s"
                    % (os.path.relpath(src, out_dir), ",".join(p[0] for p in pairs)))
    ctx = json.loads(run_ctx_json)
    gp = os.path.join(job_dir, "gate_check.json")
    if os.path.exists(gp):
        g = jload(gp)
        ctx["gate_passed"] = g["passed"]
        ctx["gate_warnings"] = g["warnings"]
        ctx["host_score"] = g["host_score"]
    ctx["harness_json"] = os.path.basename(raw)
    ctx["harness_json_sha256"] = sha256(raw)
    put("run_slot_logn16", ctx)
    jdump(res, jp)
    print("  enriched %s (added: %s)" % (os.path.basename(jp), ", ".join(sorted(added))))
    return rc

def summary(out_dir, rc, msg):
    jobs = []
    for d in sorted(os.listdir(out_dir)):
        gp = os.path.join(out_dir, d, "gate_check.json")
        if os.path.exists(gp):
            g = jload(gp)
            jobs.append({"job": d, "passed": g["passed"], "warnings": g["warnings"],
                         "failed_checks": [c for c in g["checks"] if not c["passed"]],
                         "delta_roc_auc_host": (g.get("host_score") or {}).get("delta_roc_auc"),
                         "wall_s": rd(os.path.join(out_dir, d, "wall_seconds"))})
    jdump({"finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "exit_code": int(rc),
           "status": msg, "jobs": jobs}, os.path.join(out_dir, "summary.json"))
    return 0

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "go_machine_state":
        jdump(go_machine_state(sys.argv[3]), sys.argv[2]); sys.exit(0)
    if cmd == "gate":
        sys.exit(gate(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6]),
                      sys.argv[7] == "1", int(sys.argv[8]), sys.argv[9]))
    if cmd == "enrich":
        sys.exit(enrich(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]))
    if cmd == "summary":
        sys.exit(summary(sys.argv[2], sys.argv[3], sys.argv[4]))
    sys.exit("unknown command " + cmd)
PY

# ---------------------------------------------------------------- recorded preflight
SCRIPT_SHA256="$(sha256sum "${SELF}" | cut -d' ' -f1)"
log "run_slot_logn16 start: repo ${REPO} HEAD ${HEAD_SHA}; script sha256 ${SCRIPT_SHA256}; out ${OUT}"
cp "${SELF}" "${OUT}/meta/run_slot_logn16.sh"
echo "${HEAD_SHA}" > "${OUT}/meta/git_sha.txt"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
{
  date -u; hostname; uptime
  LC_ALL=C lscpu | grep -E '^NUMA node[01] CPU'
  echo "--- governor"; cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort | uniq -c
  echo "--- disk"; df -h "${DOCKER_ROOT}" "${HOME}" "${OUT}"
  echo "--- RAM by node (kB)"; grep -hE 'MemFree|FilePages' /sys/devices/system/node/node[01]/meminfo
  echo "--- containers"; docker ps --format '{{.Names}}  {{.Image}}  {{.RunningFor}}'
  docker ps -q | xargs -r docker inspect --format '{{.Name}} cpus=[{{.HostConfig.CpusetCpus}}]'
  echo "--- top processes"; ps -eo user:12,pcpu,psr,comm --sort=-pcpu | head -12
  echo "--- docker version"; docker version --format '{{.Server.Version}}' 2>/dev/null || true
  echo "--- env"; echo "REPS=${REPS} MEM_LIMIT=${MEM_LIMIT:-unset} DATA_DIR=${DATA_DIR} CPUS=${CPUS} NODE=${NODE}"
} > "${OUT}/meta/preflight.txt" 2>&1
log "preflight recorded in meta/preflight.txt; inputs verified (7 sha256 OK)"

DF_ROOT_KB="$(df -Pk "${DOCKER_ROOT}" | awk 'NR==2{print $4}')"
DF_HOME_KB="$(df -Pk "${HOME}" | awk 'NR==2{print $4}')"
[[ "${DF_HOME_KB}" -ge 1048576 ]] || fail 2 "less than 1 GB free in ${HOME}"

# ---------------------------------------------------------------- image
LABEL_SHA="$(docker image inspect --format '{{index .Config.Labels "ppfdaas.repo_sha"}}' "${IMAGE}" 2>/dev/null || true)"
if [[ "${LABEL_SHA}" == "${HEAD_SHA}" ]]; then
  log "image ${IMAGE} already present with ppfdaas.repo_sha=${HEAD_SHA}; reusing it"
else
  [[ -z "${LABEL_SHA}" ]] || log "image ${IMAGE} exists with repo_sha=${LABEL_SHA}; rebuilding"
  [[ "${DF_ROOT_KB}" -ge 5242880 ]] || fail 2 "less than 5 GB free under Docker root ${DOCKER_ROOT}"
  for base in golang@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437 \
              debian@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8; do
    if docker image inspect "${base}" >/dev/null 2>&1; then log "base image cached: ${base}"; else log "base image NOT cached, build will pull: ${base}"; fi
  done
  log "building ${IMAGE} (needs Docker Hub, github.com and the Go module proxy); log: meta/image_build.log"
  ( cd "${REPO}" && docker build -f tools/transciphering/Dockerfile.bench \
      --build-arg GIT_SHA="${HEAD_SHA}" \
      --build-arg PATCH_SHA256="$(sha256sum third_party/patches/0001-rss-checkpoints.patch | cut -d' ' -f1)" \
      --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      -t "${IMAGE}" . ) > "${OUT}/meta/image_build.log" 2>&1 || fail 2 "docker build failed; see meta/image_build.log"
fi
LABEL_SHA="$(docker image inspect --format '{{index .Config.Labels "ppfdaas.repo_sha"}}' "${IMAGE}")"
[[ "${LABEL_SHA}" == "${HEAD_SHA}" ]] || fail 2 "image label repo_sha=${LABEL_SHA} != HEAD ${HEAD_SHA}"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}")"
docker image inspect --format '{{json .Config.Labels}}' "${IMAGE}" > "${OUT}/meta/image_labels.json"
docker run --rm --network none --user "$(id -u):$(id -g)" --cap-drop ALL --entrypoint sh "${IMAGE}" \
  -c 'grep -qE "^  slot\)" /usr/local/bin/docker-entrypoint.sh' || fail 2 "image entrypoint has no slot mode"
log "image ${IMAGE} id ${IMAGE_ID}"

# ---------------------------------------------------------------- jobs
wait_for_idle() {  # before each job: socket 0 quiet and node 0 has the RAM
  local deadline=$(( $(date +%s) + IDLE_WAIT_MIN * 60 )) busy free hot
  while :; do
    read -r busy _ <<< "$(socket_busy)"
    free="$(node_free_gb 0)"; hot="$(others_hot_on_socket0)"
    if awk -v b="${busy}" -v m="${IDLE_MAX_BUSY}" -v f="${free}" -v g="${MIN_NODE0_FREE_GB}" 'BEGIN{exit !(b<m && f>g)}' && [[ -z "${hot}" ]]; then
      log "socket 0 idle enough: ${busy} busy threads, node0 free ${free} GB"; return 0
    fi
    local hotmsg=""
    [[ -z "${hot}" ]] || hotmsg=", hot processes of other users on socket 0: $(echo "${hot}" | tr '\n' ';')"
    log "waiting: socket 0 ${busy} busy (need < ${IDLE_MAX_BUSY}), node0 free ${free} GB (need > ${MIN_NODE0_FREE_GB})${hotmsg}"
    [[ $(date +%s) -lt ${deadline} ]] || return 1
    sleep 240
  done
}

run_job() {  # $1 arm $2 cipher $3 order index
  local arm="$1" cipher="$2" idx="$3" job jdir stage t0 strikes=0 last_prog busy0 busy1 rc ctx
  job="$(job_name "${arm}" "${cipher}")"; jdir="${OUT}/${job}"
  # The wrapper refuses a non-empty job dir (and FORCE=1 is off-limits), so this runner's own
  # files are staged in meta/<job>/ during the run and moved into the job dir afterwards.
  stage="${OUT}/meta/${job}"; mkdir -p "${stage}"
  wait_for_idle || fail 4 "socket 0 not idle within ${IDLE_WAIT_MIN} min before ${job}"
  python3 "${HELPER}" go_machine_state "${stage}/machine_state_go_start.json" "${CPUS}"
  log "start ${job}: CIPHER=${cipher} target=${arm} NODE=${NODE} CPUS=${CPUS} REPS=${REPS} image=${IMAGE}"
  t0=$(date +%s)
  CUR_JOB="${job}"
  env CIPHER="${cipher}" NODE="${NODE}" CPUS="${CPUS}" REPS="${REPS}" DATA_DIR="${DATA_DIR}" IMAGE="${IMAGE}" \
      RESULTS_ROOT="${OUT}" WAIT=1 ${MEM_LIMIT:+MEM_LIMIT="${MEM_LIMIT}"} \
      "${WRAPPER}" "${job}" "${arm}" > "${stage}/wrapper_fg.log" 2>&1 &
  WRAPPER_PID=$!
  last_prog=${t0}
  while kill -0 "${WRAPPER_PID}" 2>/dev/null; do
    read -r busy0 busy1 <<< "$(socket_busy)"
    printf '{"timestamp_utc":"%s","busy_socket0":%s,"busy_socket1":%s,"loadavg":"%s"}\n' \
      "$(ts)" "${busy0}" "${busy1}" "$(cut -d' ' -f1-3 /proc/loadavg)" >> "${stage}/socket_load.jsonl"
    if awk -v b="${busy0}" -v m="${CONTENTION_MAX_BUSY}" 'BEGIN{exit !(b>m)}'; then
      strikes=$((strikes + 1))
      log "contention sample ${strikes}/${CONTENTION_STRIKES}: socket 0 ${busy0} busy threads"
      { echo "== $(ts)"; ps -eo user:12,pcpu,psr,pid,comm --sort=-pcpu | head -15; docker ps --format '{{.Names}} {{.Image}}'; } >> "${stage}/contention_evidence.txt"
      if [[ ${strikes} -ge ${CONTENTION_STRIKES} && "${CONTENTION_ACTION}" == stop ]]; then
        log "socket 0 above ${CONTENTION_MAX_BUSY} busy threads for ${strikes} samples: stopping ${job} (runbook section 7)"
        echo "${busy0}" > "${stage}/STOPPED_CONTENTION"
        docker stop "${job}" >/dev/null 2>&1 || true
      fi
    else
      strikes=0
    fi
    if [[ $(( $(date +%s) - last_prog )) -ge ${PROGRESS_EVERY_S} ]]; then
      log "${job} running $(( ($(date +%s) - t0) / 60 )) min; socket0 ${busy0} busy; last line: $(docker logs --tail 1 "${job}" 2>&1 | cut -c1-120)"
      last_prog=$(date +%s)
    fi
    sleep $(( 60 - SAMPLE_S > 0 ? 60 - SAMPLE_S : 1 ))
  done
  rc=0; wait "${WRAPPER_PID}" || rc=$?
  WRAPPER_PID=""; CUR_JOB=""
  mkdir -p "${jdir}"
  mv "${stage}"/* "${jdir}"/ && rmdir "${stage}"
  echo $(( $(date +%s) - t0 )) > "${jdir}/wall_seconds"
  python3 "${HELPER}" go_machine_state "${jdir}/machine_state_go_end.json" "${CPUS}"
  log "end ${job}: wrapper exit ${rc}, exit_code file $(cat "${jdir}/exit_code" 2>/dev/null || echo missing), $(cat "${jdir}/wall_seconds") s"
  log "gates for ${job}:"
  local grc=0
  python3 "${HELPER}" gate "${jdir}" "${arm}" "${cipher}" "${DATA_DIR}" "${AUC_TOL}" "${REQUIRE_FULL_ROWS}" "${EXPECT_ROWS}" "${jdir}/STOPPED_CONTENTION" || grc=$?
  ctx="$(printf '{"script_sha256":"%s","repo_sha":"%s","image_ref":"%s","image_id":"%s","job":"%s","order":%s,"round":%s,"node":%s,"cpus":"%s","reps":%s,"mem_limit":"%s","inputs_sha256_verified":true}' \
        "${SCRIPT_SHA256}" "${HEAD_SHA}" "${IMAGE}" "${IMAGE_ID}" "${job}" "${idx}" "$([[ ${arm} == slot ]] && echo 1 || echo 2)" "${NODE}" "${CPUS}" "${REPS}" "${MEM_LIMIT}")"
  local erc=0
  python3 "${HELPER}" enrich "${OUT}" "${jdir}" "${arm}" "${cipher}" "${ctx}" || erc=$?
  if [[ -e "${jdir}/STOPPED_CONTENTION" ]]; then fail 3 "${job} stopped: socket 0 contention (see ${job}/contention_evidence.txt)"; fi
  [[ ${grc} == 0 ]] || fail 1 "${job} failed a gate (see ${job}/gate_check.json); later jobs not started"
  [[ ${erc} == 0 ]] || fail 1 "${job}: MachineState/ParamState could not be recorded in the result JSON"
  log "PASS ${job}"
}

i=0
for j in "${JOBS[@]}"; do
  i=$((i + 1))
  run_job ${j} "${i}"
done
finish_run 0 "all 4 jobs passed: slot and cnp, hera and rubato, LogN 16, socket 0, image ${IMAGE}"
exit 0
