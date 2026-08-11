import subprocess
from pathlib import Path


PID_PATH = Path(__file__).resolve().parent / "data" / "api_server.pid"


def main():
    if not PID_PATH.exists():
        return 0
    try:
        pid = PID_PATH.read_text(encoding="ascii").strip()
        if pid.isdigit():
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True,
                check=False,
            )
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
