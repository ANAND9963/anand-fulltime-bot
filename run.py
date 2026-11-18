"""Start the Job Bot and open it in your browser. Uses the first free port from 8770 upward."""
import os
import socket
import threading
import time
import webbrowser

import uvicorn


def free_port(start=int(os.environ.get("JOBBOT_PORT", 8770)), tries=40):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:      # nothing listening there
                return port
    raise SystemExit(f"No free port between {start} and {start + tries}")


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Job Bot running at {url}  (close this window to stop it)\n", flush=True)

    def open_when_ready():
        for _ in range(60):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    webbrowser.open(url)
                    return
            time.sleep(0.5)

    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
