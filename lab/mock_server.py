#!/usr/bin/env python3
"""
PPFDaaS Module-2 lab stub. NOT the real gRPC server — just a faithful
long-running process that binds :50052 and prints heartbeats, so you can
practice run/exec/logs/inspect/stats/stop and SEE the PID 1 signal problem.

Behaviour is controlled entirely by environment variables (Module 2: -e):
  PORT=50052           TCP port to bind
  HANDLE_SIGTERM=1     1 = install a SIGTERM/SIGINT handler (clean shutdown)
                       0 = no handler (the PID 1 trap: docker stop hangs 10s)
  EXIT_AFTER=0         0 = run forever; N = exit after N heartbeats
  EXIT_CODE=1          exit code used when EXIT_AFTER fires (for --restart demos)
"""
import os, signal, socket, sys, time

HANDLE     = os.environ.get("HANDLE_SIGTERM", "1") == "1"
PORT       = int(os.environ.get("PORT", "50052"))
EXIT_AFTER = int(os.environ.get("EXIT_AFTER", "0"))
EXIT_CODE  = int(os.environ.get("EXIT_CODE", "1"))

def shutdown(signum, frame):
    print(f"[server] caught signal {signum} -> clean shutdown", flush=True)
    sys.exit(0)

if HANDLE:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", PORT))
sock.listen()
# pid is printed on purpose: inside the container you'll see pid=1.
print(f"[server] listening on :{PORT}  HANDLE_SIGTERM={HANDLE}  pid={os.getpid()}", flush=True)

beat = 0
while True:
    time.sleep(2)
    beat += 1
    print(f"[server] heartbeat {beat}", flush=True)
    if EXIT_AFTER and beat >= EXIT_AFTER:
        print(f"[server] EXIT_AFTER={EXIT_AFTER} reached -> exiting {EXIT_CODE}", flush=True)
        sys.exit(EXIT_CODE)
