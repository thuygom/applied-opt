"""Exercise the real Gunicorn process or Docker image through HTTP.

Run after installing ``.[hosting]``; optionally pass ``--docker-image NAME``.
The synthetic public origin checks HTTPS proxy handling over a local socket.
Only temporary test credentials and synthetic Python source are used.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOST = "review.example.test"
PUBLIC_ORIGIN = "https://" + PUBLIC_HOST
SOURCE = (
    "raise RuntimeError('submitted source must never execute')\n"
    "async def pipeline(text, subject):\n"
    "    context = await fetch_context(subject)\n"
    "    typos = await detect_typos(text)\n"
    "    note = await generate_note(text, context)\n"
    "    return note, typos\n"
)


def smoke(port: int, token: str) -> None:
    def request(method, path, data=None, *, authenticated=True, host=PUBLIC_HOST, origin=None):
        headers = {"Host": host}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    deadline = time.monotonic() + 20
    while True:
        try:
            status, _, raw = request("GET", "/healthz", authenticated=False)
            if status == 200:
                break
        except (OSError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError("Hosted server did not become healthy within 20 seconds")
        time.sleep(0.1)
    health = json.loads(raw)
    assert health["mode"] == "hosted" and health["auth_required"] is True, health
    status, headers, raw = request("GET", "/", authenticated=False)
    assert status == 200 and b"Applied Opt" in raw, "Dashboard shell not served"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request("HEAD", "/", authenticated=False)[2] == b""
    assert request("GET", "/api/report", authenticated=False)[0] == 401
    status, _, raw = request("GET", "/api/report")
    assert status == 200 and json.loads(raw).get("benchmark"), "Bundled report unavailable"
    payload = {"source": SOURCE, "filename": "hosting-smoke.py"}
    status, _, raw = request("POST", "/api/review", payload, origin=PUBLIC_ORIGIN)
    assert status == 200 and json.loads(raw)["findings"], "Real review API failed"
    status, _, raw = request("POST", "/api/graph", payload, origin=PUBLIC_ORIGIN)
    assert status == 200, "Real source-to-flow API failed"
    graph = json.loads(raw)
    assert graph["graph"]["kind"] == "inferred" and len(graph["graph"]["nodes"]) == 3
    assert "flowchart" in graph["mermaid"] and "@startuml" in graph["plantuml"]
    assert "<svg" in graph["svg"] and len(graph["revision"]) == 12
    assert all(node["duration_ms"] is None for node in graph["graph"]["nodes"])
    assert request("POST", "/api/review", payload, origin="https://untrusted.example")[0] == 403
    assert request("GET", "/api/report", host="untrusted.example")[0] == 403
    assert request("POST", "/api/review", payload, authenticated=False, origin=PUBLIC_ORIGIN)[0] == 401
    print("PASS: hosted health, dashboard, HEAD, report auth, source review, Mermaid/PlantUML/SVG, origin and Host checks")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-image", help="Run this prebuilt Docker image instead of the installed package")
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "APPLIED_OPT_TOKEN": token, "APPLIED_OPT_PUBLIC_URL": PUBLIC_ORIGIN,
           "WEB_CONCURRENCY": "1", "APPLIED_OPT_THREADS": "2"}
    container = None
    process = None
    with tempfile.TemporaryFile(mode="w+") as logs:
        try:
            if args.docker_image:
                container = "applied-opt-smoke-" + secrets.token_hex(6)
                subprocess.run(
                    ["docker", "run", "--detach", "--rm", "--name", container,
                     "--publish", f"127.0.0.1:{port}:8787",
                     "--read-only", "--tmpfs", "/tmp:size=16m", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges:true",
                     "--env", "APPLIED_OPT_TOKEN", "--env", "APPLIED_OPT_PUBLIC_URL",
                     "--env", "WEB_CONCURRENCY", "--env", "APPLIED_OPT_THREADS", args.docker_image],
                    env=env, stdout=subprocess.DEVNULL, check=True, timeout=30,
                )
            else:
                env.update({"HOST": "127.0.0.1", "PORT": str(port),
                            "APPLIED_OPT_REPORT": str(ROOT / "examples/demo-report.json")})
                process = subprocess.Popen(
                    [sys.executable, "-m", "applied_opt", "host"], env=env,
                    stdout=logs, stderr=logs, start_new_session=True,
                )
            smoke(port, token)
        except Exception:
            if container:
                subprocess.run(["docker", "logs", container], stdout=logs, stderr=logs, timeout=10, check=False)
            logs.seek(0)
            print(logs.read(12000).replace(token, "[redacted]"), file=sys.stderr)
            raise
        finally:
            if container:
                subprocess.run(["docker", "rm", "--force", container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=False)
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
