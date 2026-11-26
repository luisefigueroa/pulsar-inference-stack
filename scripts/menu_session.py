"""Run the interactive home menu and its on-demand storage collector."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def session():
    def interrupt(*_):
        raise KeyboardInterrupt

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, interrupt)
    with tempfile.TemporaryDirectory(prefix="pulsar-menu-") as tmp:
        request = Path(tmp) / "storage-request"
        home = subprocess.Popen([str(ROOT / "scripts/home.sh")],
                                env={**os.environ, "PULSAR_MENU_SESSION": tmp})
        collector = None
        retry_at = 0.
        try:
            while home.poll() is None:
                if collector is not None and collector.poll() is not None:
                    collector = None
                    retry_at = time.monotonic() + 5
                if collector is None and request.exists() and time.monotonic() >= retry_at:
                    collector = subprocess.Popen(
                        ["bash", "-c", 'REPO_DIR=$1; . "$1/scripts/menu-settings.sh"; '
                         'exec python3 "$1/scripts/storage_overview.py" collect', "_", str(ROOT)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                time.sleep(.1)
            return home.returncode
        finally:
            if collector is not None and collector.poll() is None:
                try:
                    os.killpg(collector.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                collector.wait()
            if home.poll() is None:
                home.terminate()
                home.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(session())
    except KeyboardInterrupt:
        raise SystemExit(130)
