"""Call the current Muse run's existing tools from an Agent-written script."""
import json
import os
import socket


def call(name, arguments=None, *, timeout=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(os.environ["MUSE_TOOL_SOCKET"])
        payload = {"token": os.environ["MUSE_TOOL_TOKEN"], "name": name, "arguments": arguments or {}}
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        with conn.makefile("rb") as stream:
            result = json.loads(stream.readline())
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["result"]
