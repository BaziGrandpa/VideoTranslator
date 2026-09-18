from __future__ import annotations

import os
import subprocess
import sys

import uvicorn

from app.config import settings


def main() -> None:
    settings.ensure_directories()
    pid_file = settings.data_dir / "video-translator.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    worker = subprocess.Popen([sys.executable, "-m", "app.worker"])
    try:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=False,
            access_log=False,
        )
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
        try:
            if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
