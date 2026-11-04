# DGX runbook: slot and CNP-B benchmarks at LogN 16

Four jobs on `lnmdgx1`, under your own account, in two rounds. Expect about 3 hours of machine time, nearly all unattended. Each job uses one socket and up to 65 GB of RAM. You add one Docker image, plus files in your home directory. Paste each block from a shell; jobs survive logout. Raghav scores the results.

If any check fails, stop and tell Raghav. Don't work around it.

## 0. Preflight

Paste this block, then read the output against the table below.

```bash
hostname; id -nG | tr ' ' '\n' | grep -x docker || echo "NOT IN docker GROUP"
mkdir -p ~/slot_cnp_meta
cat > ~/slot_cnp_meta/socket_busy.sh <<'EOF'
# Busy threads per socket over 30 s. Socket 0 = CPUs 0-19,40-59; socket 1 = CPUs 20-39,60-79.
a=$(grep '^cpu[0-9]' /proc/stat); sleep 30; b=$(grep '^cpu[0-9]' /proc/stat)
printf '%s\n%s\n' "$a" "$b" | awk '{ id=substr($1,4)+0; idle=$5+$6; tot=0; for(i=2;i<=9;i++) tot+=$i
  if (id in T) { s=(id<20||(id>=40&&id<60))?0:1; busy[s]+=1-(idle-I[id])/(tot-T[id]) }
  T[id]=tot; I[id]=idle }
  END { printf "socket 0: %.1f of 40 threads busy\nsocket 1: %.1f of 40 threads busy\n", busy[0], busy[1] }'
EOF
{
  date -u; hostname; uptime
  lscpu | grep -E '^NUMA node[01] CPU'
  echo "--- governor"; cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort | uniq -c
  echo "--- disk"; df -h "$(docker info --format '{{.DockerRootDir}}')" "$HOME"
  echo "--- RAM by socket"; grep -hE 'MemFree|FilePages' /sys/devices/system/node/node[01]/meminfo
  echo "--- containers"; docker ps --format '{{.Names}}  {{.Image}}  {{.RunningFor}}'
  docker ps -q | xargs -r docker inspect --format '{{.Name}} cpus=[{{.HostConfig.CpusetCpus}}]'
  echo "--- name clashes"; docker ps -a --format '{{.Names}}' | grep -E '^(slot|cnp)_(hera|rubato)_s[01]$'
  echo "--- top processes"; ps -eo user:12,pcpu,psr,comm --sort=-pcpu | head -12
  echo "--- socket load"; bash ~/slot_cnp_meta/socket_busy.sh
} 2>&1 | tee ~/slot_cnp_meta/preflight.txt
```

| Check | Go if |
|---|---|
| Host and group | Prints `lnmdgx1` and `docker`. If you aren't in the group, ask the admin. Don't use `sudo`. |
| CPU layout | Node 0 is `0-19,40-59`; node 1 is `20-39,60-79`. |
| Disk | At least 5 GB free for Docker and 1 GB in your home. The final image is about 89 MB; the rest is the Go build stage (base image 807 MB). |
| RAM | On each node, MemFree + FilePages is above 70 GB (values are in kB). |
| Name clashes | Nothing listed. |
| Sockets | Each socket under 10 of 40 threads busy, and no other user's process above 400% CPU. The host is rarely idle: load was 12 to 22 at the start of every earlier job, so use the socket figures, not `uptime`. Containers showing `cpus=[]` can run on either socket. |

The governor is only recorded. Earlier runs had `performance`. Don't change it.

## 1. Get the code

```bash
git clone --branch main https://github.com/raghavpathak30/PPFDaaS.git ~/PPFDaaS
cd ~/PPFDaaS && git rev-parse HEAD | tee ~/slot_cnp_meta/git_sha.txt
```

Nothing else to fetch: the image build clones and patches `third_party/RtF-Transciphering` itself, pinned to `105fc73`.

## 2. Inputs

Put `cnpb_data.tar.gz` from Raghav in your home directory, then:

```bash
tar -xzf ~/cnpb_data.tar.gz -C ~
cd ~/cnpb_data && sha256sum -c - <<'EOF'
5e6df6eafaeca093767f82e2f18622e7535c3911924b0955acd5692dbb0c5a4d  expected.i64
98001b75879558ac9ae483144a5087c34b14a3d3c05f78ef29c9edfa531aee46  meta.json
6621b19f7cc6785c330812143302e81b46d1b941b49cf25c374c6f7b602cd64b  plain_logit.f64
0fb6a8b686e7f23b9870c9590240a731da4932d8727148efda518a402f1f4400  strat4096.i32
718327abe85e822ec412c7174e3449048a8a751196ec91490b07c8939cdb733a  wq.i16
a83ad5f54ee185fa1300acc459798e8a3255f7b7b4b1923a0c85e1b05876eef7  xq.i16
c80d24946b6334dfcd600a652351f56ad0ed9dfc69fc952b319c2e708c1207d4  y.u8
EOF
```

All 7 lines must say `OK`.

## 3. Build the image

The build needs access to github.com and the Go module proxy.

```bash
cd ~/PPFDaaS && export IMAGE=ppfdaas-transcipher-bench:$(git rev-parse --short=7 HEAD)
docker build -f tools/transciphering/Dockerfile.bench \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --build-arg PATCH_SHA256="$(sha256sum third_party/patches/0001-rss-checkpoints.patch | cut -d' ' -f1)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t "$IMAGE" .
docker image inspect --format '{{json .Config.Labels}}' "$IMAGE" | tee ~/slot_cnp_meta/image_labels.json
docker image inspect --format '{{index .Config.Labels "ppfdaas.repo_sha"}}' "$IMAGE"; cat ~/slot_cnp_meta/git_sha.txt
```

The last two lines printed must match. The `export IMAGE` line is what selects this build: without it, the run script silently uses `:latest`, which may be an older image. The blocks below set it again, so they are safe in a new shell.

## 4. Run

**Round 1, slot.** ESTIMATE: about 1 h 15 min for HERA and 30 min for Rubato, so **allow 1.5 h**. This path has never run at LogN 16. HERA does 18 keystream runs at about 220 s each and Rubato does 8 at about 150 s, using times from the earlier round 2 runs, plus setup and evaluation.

```bash
cd ~/PPFDaaS && export IMAGE=ppfdaas-transcipher-bench:$(git rev-parse --short=7 HEAD)
CIPHER=hera   NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_hera_s0 slot
CIPHER=rubato NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh slot_rubato_s1 slot
```

Both jobs detach. When both have `exit_code` files and pass section 5, rerun the section 0 socket check (`bash ~/slot_cnp_meta/socket_busy.sh`) and start round 2.

**Round 2, CNP-B.** MEASURED on this host, 28 Oct 2026, same settings: HERA 1 h 26 min, Rubato 47 min, so **allow 2 h**.

```bash
cd ~/PPFDaaS && export IMAGE=ppfdaas-transcipher-bench:$(git rev-parse --short=7 HEAD)
CIPHER=hera   NODE=0 CPUS=0-19  REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_hera_s0 cnp
CIPHER=rubato NODE=1 CPUS=20-39 REPS=5 DATA_DIR=$HOME/cnpb_data scripts/dgx/run_transcipher.sh cnp_rubato_s1 cnp
```

To block until a round ends, paste this and name its jobs. It only waits, so closing the shell doesn't affect the jobs:

```bash
wait_jobs() { for j in "$@"; do until [ -e "$HOME/results/$j/exit_code" ]; do sleep 60; done; done; }
wait_jobs slot_hera_s0 slot_rubato_s1
```

## 5. Monitoring

A running job prints almost nothing until it ends, often for over an hour. That is normal.

```bash
for j in slot_hera_s0 slot_rubato_s1 cnp_hera_s0 cnp_rubato_s1; do d=~/results/$j
  echo "== $j exit_code=$(cat $d/exit_code 2>/dev/null || echo none)"
  [ -e $d/exit_code ] && grep -E '^--- (PASS|FAIL)|^panic|STOPPED|row set:' $d/container.log | cut -c1-150
  [ -e $d/exit_code ] && grep -hE '"headroom_gate"' $d/*_hera.json $d/*_rubato.json 2>/dev/null | cut -c1-70
done; docker ps --format '{{.Names}}  {{.RunningFor}}' | grep -E '^(slot|cnp)_'
```

**Passed** means all of these:
- `exit_code=0`
- `--- PASS: TestSlotRun` or `--- PASS: TestCNPRun`
- `"headroom_gate": "passed: ...`

The row-set line should say `full test set (56962 rows`. If it says `STRATIFIED`, the job has still passed, but tell Raghav.

**Failed** looks like any of these:

| You see | Meaning |
|---|---|
| `exit_code` not 0, or `--- FAIL`, or `panic` | The job failed. |
| `exit_code=137` | Killed, usually out of memory. |
| `STOPPED: headroom ...` or `HERA bit-exactness violated` | A correctness gate stopped the run. |
| `exit_code=none`, and the job isn't in `docker ps` | The wrapper died. See `wrapper.log` in the job's folder. |

Rubato shows `"bit_exact": false` in its JSON. That is expected and not a failure.

If a job fails, don't start the next round and don't rerun it. Send Raghav the folder as it is.

## 6. Hand-back

After both rounds pass. The machine-state files are inside each job's folder, and the SHA and preflight record are in `slot_cnp_meta`.

```bash
tar -C ~ -czf ~/slot_cnp_results_$(date -u +%Y%m%d).tar.gz results/slot_hera_s0 results/slot_rubato_s1 results/cnp_hera_s0 results/cnp_rubato_s1 slot_cnp_meta && sha256sum ~/slot_cnp_results_*.tar.gz
```

Send Raghav the file and the hash it prints. It should be a few MB at most.

## 7. Rules

- Never run `docker system prune`, and never remove an image you didn't build.
- Never run anything as root. No `sudo`.
- Don't use `FORCE=1`, and don't edit the scripts.
- **Stop if another user's heavy job lands on your socket.** During a run, expect about 20 of 40 threads busy on each socket from these jobs. If `bash ~/slot_cnp_meta/socket_busy.sh` shows over 30 on a socket and `ps` or `docker ps` shows the other user's job, run `docker stop <your job on that socket>` and tell Raghav. Leave the other user's job alone.
