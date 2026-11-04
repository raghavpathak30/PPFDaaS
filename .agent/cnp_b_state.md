# CNP-B state (branch cnp-b, worktree /home/raghavp/BTP-cnpb, from cnp-a)
See docs/cnp_b.md. Pitfalls: fork is `go 1.13` language level (no `any`/generics); `third_party/RtF-Transciphering` is
gitignored and re-created by `bash third_party/fetch_rtf.sh` (copies every testdata/ckks_fv_patch/*.go into ckks_fv/);
copy edited harness files there (glob `rtf_cnp*_test.go`, note rtf_cnp_test.go has no underscore suffix) before `go vet`.
Toy run: `E2E_RUN=1 E2E_CIPHER=hera E2E_LOGN=12 CNP_CLIENT_ENCODE=exact CNP_DATA_DIR=<prepare_inputs out> go test -run '^TestCNPRun$' -v`.
Not done: any LogN 16 run (needs the DGX; this host has 15 GB). Memory split fields: setup_retained_heap_kb, memory_checkpoints.
