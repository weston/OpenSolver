#!/usr/bin/env python3
"""Interactive PioSOLVER session over SSH: send one command, wait for its END-terminated response."""
import os, queue, subprocess, threading, time

class PioSession:
    def __init__(self, dest=None, key=None, exe=None):
        dest = dest or os.environ["PIO_SSH"]  # e.g. user@windows-host (required)
        key = os.path.expanduser(key or os.environ.get("PIO_KEY", "~/.ssh/pio_pc_ed25519"))
        exe = exe or os.environ.get("PIO_EXE", r"C:\PioSOLVER\PioSOLVER3-edge-znver5.exe")
        folder, name = exe.rsplit("\\", 1)
        self.p = subprocess.Popen(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", dest,
                                   f"cd /d {folder} && {name}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.q = queue.Queue()
        self.solver = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.cmd("set_end_string END")

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\r\n"))
        self.q.put(None)

    def cmd(self, line, timeout=600):
        self.p.stdin.write(line + "\r\n"); self.p.stdin.flush()
        out, t0 = [], time.time()
        while True:
            try:
                l = self.q.get(timeout=max(1, timeout - (time.time() - t0)))
            except queue.Empty:
                return "<TIMEOUT>\n" + "\n".join(out)
            if l is None:
                return "<SOLVER EXITED>\n" + "\n".join(out)
            if l.startswith("[LOG]"):  # progress lines Pio prints while solving preflop trees
                self.solver.append([l])
                continue
            if l.startswith("SOLVER:") and not out:
                block = [l]
                if l == "SOLVER:":
                    while (l2 := self.q.get()) not in ("END", None):
                        block.append(l2)
                self.solver.append(block)
                continue
            if l == "END":
                return "\n".join(out)
            out.append(l)

    def close(self):
        try:
            self.p.stdin.write("exit\r\n"); self.p.stdin.flush(); self.p.wait(timeout=20)
        except Exception:
            self.p.kill()
