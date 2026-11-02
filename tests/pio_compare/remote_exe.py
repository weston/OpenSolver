#!/usr/bin/env python3
"""Run a UPI solver on a remote (Windows) machine as if it were a local executable.

Lets local tools that drive a solver over stdin/stdout (bench/run.py, test clients) target a remote
binary, e.g. PioSOLVER on the maintainer's PC or our own solver.exe there, for same-machine
comparisons. Black-box: only UPI text is sent.

    PIO_SSH=user@host PIO_EXE='C:\\path\\to\\solver.exe' tests/pio_compare/remote_exe.py

  PIO_SSH  ssh destination (required)
  PIO_KEY  ssh private key (default: ~/.ssh/pio_pc_ed25519)
  PIO_EXE  executable on the remote machine (default: PioSOLVER 3 edge znver5)

`set_range` lines with range text are converted to Pio's 1326 numbers with our local ./solver,
because PioSOLVER only accepts the numeric form. Output is streamed back line by line.
"""
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from compare import range_to_floats  # noqa: E402

LOCAL = os.path.join(HERE, "..", "..", "solver")


def convert(line: str) -> str:
    parts = line.split(None, 2)
    if len(parts) == 3 and parts[0] == "set_range":
        body = parts[2].strip()
        if any(c.isalpha() for c in body):
            return f"set_range {parts[1]} {range_to_floats(LOCAL, body)}"
    return line


def main() -> int:
    dest = os.environ["PIO_SSH"]
    key = os.path.expanduser(os.environ.get("PIO_KEY", "~/.ssh/pio_pc_ed25519"))
    exe = os.environ.get("PIO_EXE", r"C:\PioSOLVER\PioSOLVER3-edge-znver5.exe")
    folder = exe.rsplit("\\", 1)[0] if "\\" in exe else "."
    remote = f'cd /d "{folder}" && "{exe}"'  # PioSOLVER must start in its own folder (licence files)
    p = subprocess.Popen(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", dest, remote],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def pump_out():
        for line in p.stdout:
            sys.stdout.write(line.replace("\r", ""))
            sys.stdout.flush()

    t = threading.Thread(target=pump_out, daemon=True)
    t.start()
    try:
        for line in sys.stdin:
            p.stdin.write(convert(line.rstrip("\r\n")) + "\n")
            p.stdin.flush()
    except BrokenPipeError:
        pass
    try:
        p.stdin.close()
    except BrokenPipeError:
        pass
    p.wait()
    t.join(timeout=5)
    return p.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
