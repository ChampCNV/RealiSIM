#!/bin/sh
# Runs the pinned Factory CLI for RealiSIM support jobs with hard process limits. Credentials arrive only through the
# environment the Droid SDK builds for this child; nothing is written here.
ulimit -c 0
ulimit -t 600            # CPU seconds
ulimit -f 1048576        # max file size (KiB) = 1 GiB
exec nice -n 10 /workspace/photo2grasp/factory/bin/droid "$@"
