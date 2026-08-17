# server_stub.py
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 50052))   # NOT 127.0.0.1 — see the gotcha below
s.listen()
print("stub server listening on 0.0.0.0:50052", flush=True)
while True:
    conn, addr = s.accept()
    print(f"connection from {addr}", flush=True)
    conn.sendall(b"PPFDaaS-stub: HE inference would happen here\n")
    conn.close()
