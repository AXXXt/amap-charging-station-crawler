import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PID_PATH = ROOT / "data" / "api_server.pid"
LOG_PATH = ROOT / "logs" / "api_server.log"
ERROR_LOG_PATH = ROOT / "logs" / "api_server_error.log"


def main():
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    stdout_handle = LOG_PATH.open("a", encoding="utf-8")
    stderr_handle = ERROR_LOG_PATH.open("a", encoding="utf-8")
    try:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "api_server.py"],
            cwd=ROOT,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creation_flags,
        )
        PID_PATH.write_text(str(process.pid), encoding="ascii")
        return process.wait()
    finally:
        stdout_handle.close()
        stderr_handle.close()
        if PID_PATH.exists():
            try:
                if PID_PATH.read_text(encoding="ascii").strip() == str(process.pid):
                    PID_PATH.unlink()
            except (OSError, UnboundLocalError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
