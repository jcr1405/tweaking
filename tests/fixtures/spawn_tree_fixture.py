"""Fixture for testing multi-process tree containment within Win32 Job Objects.
Spawns a child/worker process, which in turn spawns a grandchild process,
and prints their PIDs to stdout so the parent can monitor their lifecycle.
"""
import sys
import os
import subprocess
import time

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--leaf":
        # Grandchild leaf process: sleeps until terminated by Job Object
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        # Child process: spawn grandchild leaf
        grandchild = subprocess.Popen([sys.executable, __file__, "--leaf"])
        # Output PIDs for parent verification
        print(f"CHILD_PID={os.getpid()}", flush=True)
        print(f"GRANDCHILD_PID={grandchild.pid}", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            grandchild.terminate()
            sys.exit(0)
