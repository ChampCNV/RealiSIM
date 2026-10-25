"""Bound the private data memory of one pipeline process before executing it."""
import os, resource, sys
limit = int(os.environ.get("REALISIM_LOCAL_DATA_BYTES", str(4 * 1024**3)))
old_soft, old_hard = resource.getrlimit(resource.RLIMIT_DATA)
hard = limit if old_hard == resource.RLIM_INFINITY else min(limit, old_hard)
resource.setrlimit(resource.RLIMIT_DATA, (hard, hard))
if len(sys.argv) < 2:
    raise SystemExit("Missing pipeline executable")
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
