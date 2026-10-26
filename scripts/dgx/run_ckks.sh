#!/usr/bin/env bash
# Host-side wrapper: run one job of the CKKS benchmark image in a detached,
# CPU/NUMA-pinned, unprivileged container, with before/after machine state
# records. Same shape as run_transcipher.sh. Written for bash 4.2 / RHEL 7.9
# (no jq/python needed).
#
# Usage:
#   scripts/dgx/run_ckks.sh <job> <target>
#
#   <job>     name for the container AND for ~/results/<job>/ (A-Za-z0-9._-)
#   <target>  parity   plaintext parity gate (tol 1e-3), both param sets, fold and bsgs
#             a1       per-request stage breakdown + wire bytes (fold), 160- and 200-bit
#             a2       fold vs BSGS in one invocation
#             a3       thread scaling over THREADS (fold and bsgs)
#             a4       throughput: CONCURRENCY concurrent requests
#             all      parity, a1, a2, a3, a4 in order
#
# Environment (all optional):
#   PARAMS=160|200|both       default both
#   REPS=<n>  WARMUP=<n>      repetitions / warmup per point (harness default 100 / 20)
#   SEED=<n>                  workload seed (default 42)
#   STRATEGIES=fold,bsgs      restrict a1/a3/a4 strategies (defaults: a1 fold, a3 both, a4 fold)
#   OMP_THREADS=<n>           OpenMP threads for a1/a2 (default 1)
#   THREADS="1 2 4 8 16 20"   a3 thread counts (default: powers of two below the pinned CPU
#                             count, then the pinned CPU count itself)
#   CONCURRENCY="1 2 4 ..."   a4 concurrent request counts (same default)
#   DURATION=<s>              a4 seconds per point (default 10)
#   OMP_PROC_BIND, OMP_PLACES OpenMP placement inside the cpuset (default close / cores)
#   OMP_WAIT_POLICY           passed into the container when set
#   CPUS=<list>               --cpuset-cpus, e.g. 0-19 (default: derived from NODE and SMT)
#   SMT=off|on                default off = one logical CPU per physical core of NODE (the
#                             transciphering runs used the 20 physical cores of one socket).
#                             on = every logical CPU of the node. Ignored when CPUS is set.
#   NODE=<n>                  NUMA node for default CPUS and --cpuset-mems (default 0)
#   MEM_LIMIT=<e.g. 16g>      optional --memory (recommended on a shared host)
#   IMAGE=<name[:tag]|id>     default ppfdaas-ckks-bench:latest
#   RESULTS_ROOT=<dir>        default ~/results
#   WAIT=1                    block until the job finishes (default: detach)
#   FORCE=1                   allow reusing a non-empty results dir
set -euo pipefail

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2; exit 2; }
[[ $# -ge 2 ]] || usage
JOB="$1"; TARGET="$2"; shift 2
[[ "${JOB}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "bad job name: ${JOB}" >&2; exit 2; }

IMAGE="${IMAGE:-ppfdaas-ckks-bench:latest}"
NODE="${NODE:-0}"
SMT="${SMT:-off}"
RESULTS_ROOT="${RESULTS_ROOT:-${HOME}/results}"
RDIR="${RESULTS_ROOT}/${JOB}"

CPUS="${CPUS:-}"
if [[ -z "${CPUS}" ]]; then
  CPUS="$(cat "/sys/devices/system/node/node${NODE}/cpulist" 2>/dev/null || true)"
  [[ -n "${CPUS}" ]] || { echo "cannot read cpulist for NUMA node ${NODE}; set CPUS=" >&2; exit 1; }
  if [[ "${SMT}" == off ]]; then   # first logical CPU of each (socket,core) on this node
    CPUS="$(LC_ALL=C lscpu -p=CPU,CORE,SOCKET,NODE | awk -F, -v n="${NODE}" '!/^#/ && $4==n { k=$2":"$3; if(!(k in s)){ s[k]=1; printf "%s%s", (c++?",":""), $1 } }')"
    [[ -n "${CPUS}" ]] || { echo "SMT=off: no CPUs found for NUMA node ${NODE}" >&2; exit 1; }
  elif [[ "${SMT}" != on ]]; then
    echo "SMT must be on|off" >&2; exit 2
  fi
fi
NCPUS="$(echo "${CPUS}" | awk -F, '{n=0; for(i=1;i<=NF;i++){split($i,a,"-"); n+= (a[2]=="")?1:a[2]-a[1]+1}; print n}')"

# ---- build the container command + env --------------------------------------
ENVARGS=(-e "OMP_PROC_BIND=${OMP_PROC_BIND:-close}" -e "OMP_PLACES=${OMP_PLACES:-cores}")
add_env() { ENVARGS+=(-e "$1=$2"); }
[[ -n "${OMP_WAIT_POLICY:-}" ]] && add_env OMP_WAIT_POLICY "${OMP_WAIT_POLICY}"
# Default sweeps top out at the pinned CPU count: 1 2 4 8 16 ... then NCPUS.
default_sweep() { local n=1 out=""; while (( n < NCPUS )); do out+="${n} "; n=$((n*2)); done; echo "${out}${NCPUS}"; }
THREADS="${THREADS:-$(default_sweep)}"
CONCURRENCY="${CONCURRENCY:-$(default_sweep)}"
for t in ${THREADS} ${CONCURRENCY}; do
  (( t <= NCPUS )) || echo "warning: sweep value ${t} exceeds the ${NCPUS} pinned CPUs (oversubscribed)" >&2
done
for v in PARAMS REPS WARMUP SEED STRATEGIES OMP_THREADS THREADS CONCURRENCY DURATION; do
  [[ -n "${!v:-}" ]] && add_env "${v}" "${!v}"
done
CMD=()
case "${TARGET}" in
  parity|a1|a2|a3|a4|all) CMD=("${TARGET}") ;;
  *) usage ;;
esac

# ---- machine-state JSON (hand-built; values are escaped by jstr) ---------------
jstr() { printf '%s' "$1" | awk 'BEGIN{ORS=""; printf "\""} {gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); gsub(/\t/,"\\t"); if(NR>1) printf "\\n"; printf "%s",$0} END{printf "\""}'; }
jraw() { [[ -n "$1" ]] && printf '%s' "$1" || printf 'null'; }
lscpu_field() { LC_ALL=C lscpu | awk -F: -v k="$1" '$1==k{sub(/^[ \t]+/,"",$2); print $2; exit}'; }

cpuset_mhz() {  # per-CPU MHz for the pinned cpuset, as a JSON object {"cpu":mhz,...}
  local list="$1" out="" c
  for part in ${list//,/ }; do
    if [[ "${part}" == *-* ]]; then seq "${part%-*}" "${part#*-}"; else echo "${part}"; fi
  done | while read -r c; do
    awk -v c="${c}" '$1=="processor"{p=$3} $1=="cpu" && $2=="MHz"{ if(p==c) print p, $4 }' /proc/cpuinfo
  done | awk 'BEGIN{printf "{"} {printf "%s\"%s\":%s", (NR>1?",":""), $1, $2} END{printf "}"}'
}

machine_state() {  # $1 = start|end
  local phase="$1" f="${RDIR}/machine_state_$1.json" cname="${JOB}" img_id labels mem_line
  img_id="$(docker image inspect --format '{{.Id}}' "${IMAGE}" 2>/dev/null || echo unknown)"
  labels="$(docker image inspect --format '{{json .Config.Labels}}' "${IMAGE}" 2>/dev/null || true)"
  mem_line="$(LC_ALL=C free -g | awk '/^Mem:/{print}')"
  {
    printf '{\n'
    printf '  "phase": %s,\n' "$(jstr "${phase}")"
    printf '  "job": %s,\n' "$(jstr "${JOB}")"
    printf '  "timestamp_utc": %s,\n' "$(jstr "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    printf '  "hostname": %s,\n' "$(jstr "$(hostname)")"
    printf '  "kernel": %s,\n' "$(jstr "$(uname -r)")"
    printf '  "host_loadavg_1_5_15": %s,\n' "$(awk '{printf "[%s,%s,%s]", $1,$2,$3}' /proc/loadavg)"
    printf '  "lscpu": {\n'
    printf '    "model_name": %s,\n' "$(jstr "$(lscpu_field 'Model name')")"
    printf '    "cpus": %s,\n' "$(jstr "$(lscpu_field 'CPU(s)')")"
    printf '    "sockets": %s,\n' "$(jstr "$(lscpu_field 'Socket(s)')")"
    printf '    "cores_per_socket": %s,\n' "$(jstr "$(lscpu_field 'Core(s) per socket')")"
    printf '    "threads_per_core": %s,\n' "$(jstr "$(lscpu_field 'Thread(s) per core')")"
    printf '    "numa_nodes": %s,\n' "$(jstr "$(lscpu_field 'NUMA node(s)')")"
    printf '    "numa_node0_cpus": %s,\n' "$(jstr "$(lscpu_field 'NUMA node0 CPU(s)')")"
    printf '    "numa_node1_cpus": %s,\n' "$(jstr "$(lscpu_field 'NUMA node1 CPU(s)')")"
    printf '    "cpu_mhz": %s,\n' "$(jstr "$(lscpu_field 'CPU MHz')")"
    printf '    "cpu_max_mhz": %s\n' "$(jstr "$(lscpu_field 'CPU max MHz')")"
    printf '  },\n'
    printf '  "cpu_mhz_pinned_cpus": %s,\n' "$(cpuset_mhz "${CPUS}")"
    printf '  "cpu_governor_cpu0": %s,\n' "$(jstr "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown)")"
    printf '  "free_g": %s,\n' "$(echo "${mem_line}" | awk '{printf "{\"total\":%s,\"used\":%s,\"free\":%s,\"shared\":%s,\"buff_cache\":%s,\"available\":%s}", $2,$3,$4,$5,$6,(NF>=7?$7:"null")}')"
    printf '  "cpuset": {"cpus": %s, "mems": %s, "numa_node": %s, "ncpus": %s, "smt": %s},\n' "$(jstr "${CPUS}")" "$(jstr "${NODE}")" "${NODE}" "${NCPUS}" "$(jstr "${SMT}")"
    printf '  "image": {"ref": %s, "id": %s, "labels": %s},\n' "$(jstr "${IMAGE}")" "$(jstr "${img_id}")" "$(jraw "${labels}")"
    printf '  "command": %s\n' "$(jstr "${CMD[*]}")"
    printf '}\n'
  } > "${f}.tmp" && mv "${f}.tmp" "${f}"
}

# ---- launch -----------------------------------------------------------------
if [[ -d "${RDIR}" && -n "$(ls -A "${RDIR}" 2>/dev/null)" && "${FORCE:-0}" != 1 ]]; then
  echo "${RDIR} is not empty; pick a new job name or FORCE=1" >&2; exit 1
fi
mkdir -p "${RDIR}"
machine_state start

DOCKER_ARGS=(run -d --name "${JOB}" --user "$(id -u):$(id -g)"
  --cpuset-cpus "${CPUS}" --cpuset-mems "${NODE}"
  --cap-drop ALL --security-opt no-new-privileges
  -v "${RDIR}:/results")
[[ -n "${MEM_LIMIT:-}" ]] && DOCKER_ARGS+=(--memory "${MEM_LIMIT}" --memory-swap "${MEM_LIMIT}")
DOCKER_ARGS+=("${ENVARGS[@]}" "${IMAGE}" "${CMD[@]}")

CID="$(docker "${DOCKER_ARGS[@]}")"
echo "started ${JOB} (container ${CID:0:12}) cpus=${CPUS} mems=${NODE} results=${RDIR}"

finish() {
  local rc
  rc="$(docker wait "${JOB}")"
  docker logs "${JOB}" > "${RDIR}/container.log" 2>&1 || true
  echo "${rc}" > "${RDIR}/exit_code"
  machine_state end
  docker rm "${JOB}" >/dev/null 2>&1 || true   # frees the name; logs are saved above
  echo "finished ${JOB}: exit ${rc}" >&2
  return 0
}

if [[ "${WAIT:-0}" == 1 ]]; then
  finish
  exit "$(cat "${RDIR}/exit_code")"
else
  # Survive the SSH session ending; progress: docker logs -f <job>, or ${RDIR}/exit_code appearing.
  export -f finish machine_state jstr jraw lscpu_field cpuset_mhz
  export JOB RDIR IMAGE CPUS NODE NCPUS SMT TARGET
  CMD_STR="${CMD[*]}" ; export CMD_STR
  nohup setsid bash -c 'CMD=(${CMD_STR}); finish' >"${RDIR}/wrapper.log" 2>&1 < /dev/null &
  echo "detached; follow with: docker logs -f ${JOB}   (done when ${RDIR}/exit_code exists)"
fi
