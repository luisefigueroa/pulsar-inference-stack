#!/usr/bin/env python3
"""Read host runtime versions for private measurement provenance only."""
import json
import platform
import subprocess


def scalar(command):
    try:
        result=subprocess.run(command,text=True,capture_output=True,timeout=5)
        return result.stdout.strip() or None if result.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):
        return None


def main():
    print(json.dumps({'architecture':platform.machine(),'kernel_release':platform.release(),
        'gpu_driver':scalar(['nvidia-smi','--query-gpu=driver_version','--format=csv,noheader']),
        'container_runtime_version':scalar(['docker','version','--format','{{.Server.Version}}'])},sort_keys=True))


if __name__=='__main__': main()
