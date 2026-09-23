"""GPU allocator cap and payload invocation, inside the guarded container."""

import json
import os
from pathlib import Path
import runpy
import sys


def main():
    context = json.loads(Path(sys.argv[1]).read_text())
    # CUDA is only touched by this child after the CPU guard releases it.
    import torch

    if torch.cuda.device_count() != 1 or torch.cuda.get_device_capability(0) != (12, 1):
        raise RuntimeError("diagnostic requires exactly one SM121 GPU")
    limit = context["request"]["limits"]["memory_bytes"]
    total = torch.cuda.get_device_properties(0).total_memory
    if limit >= total:
        raise RuntimeError("allocator cap exceeds device memory")
    torch.cuda.set_per_process_memory_fraction(limit / total, 0)
    os.environ["PULSAR_DIAGNOSTIC_CONTEXT"] = sys.argv[1]
    os.environ["PULSAR_DIAGNOSTIC_RESULT"] = "/tmp/pulsar-diagnostic-result.json"
    sys.path.insert(0, "/diagnostic/payload")
    sys.argv = ["/diagnostic/payload/" + context["request"]["entrypoint"]]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
