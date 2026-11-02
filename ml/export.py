#!/usr/bin/env python3
"""Export a river_v1 .pt checkpoint to WSNW0001, the dependency-free weight
blob the C++ warm-start side (stage 3, #59) reads.

Layout (little-endian, f32 row-major):

    char[8]  magic = "WSNW0001"
    u32      G, H, A            (feature dims; must match ml/features.py)
    f32      pot_scale
    u32      hidden
    value_net: 3 x { u32 out, u32 in, f32 W[out][in], f32 b[out] }
    strat_net: 3 x { u32 out, u32 in, f32 W[out][in], f32 b[out] }

Layer order and count mirror train.build_model exactly (Linear/ReLU pairs,
then the output Linear). The C++ reader must apply ReLU after layers 1-2 and
none after layer 3.

Usage: python3 ml/export.py checkpoint.pt out.wsnb
The round-trip check (`--selftest` on an .wsnb) uses only the stdlib.
"""
from __future__ import annotations

import argparse
import struct
import sys

MAGIC = b"WSNW0001"
LAYERS = ("0", "2", "4")  # Linear module indices inside each nn.Sequential


def export(ckpt_path: str, out_path: str) -> None:
    import torch  # lazy: --selftest works without torch

    ck = torch.load(ckpt_path, map_location="cpu")
    dims, hidden = ck["dims"], ck["hidden"]
    buf = [MAGIC, struct.pack("<III", dims["G"], dims["H"], dims["A"]),
           struct.pack("<f", ck["pot_scale"]), struct.pack("<I", hidden)]
    for net in ("value", "strat"):
        sd = ck[net]
        for li in LAYERS:
            W = sd[f"{li}.weight"].detach().to(torch.float32).contiguous()
            b = sd[f"{li}.bias"].detach().to(torch.float32).contiguous()
            out_f, in_f = W.shape
            wf = W.reshape(-1).tolist()  # C (row-major) order
            bf = b.reshape(-1).tolist()
            buf.append(struct.pack("<II", out_f, in_f))
            buf.append(struct.pack(f"<{len(wf)}f", *wf))
            buf.append(struct.pack(f"<{len(bf)}f", *bf))
    with open(out_path, "wb") as f:
        f.write(b"".join(buf))
    print(f"exported {ckpt_path} -> {out_path} "
          f"(G={dims['G']} H={dims['H']} A={dims['A']} hidden={hidden})")


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.o = data, 0

    def take(self, n: int) -> bytes:
        b = self.d[self.o : self.o + n]
        assert len(b) == n, "unexpected EOF"
        self.o += n
        return b

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def f32s(self, n: int) -> list:
        return list(struct.unpack(f"<{n}f", self.take(4 * n)))


def selftest(path: str) -> None:
    """Parse only; verifies the file is well-formed and self-consistent."""
    r = _Reader(open(path, "rb").read())
    assert r.take(8) == MAGIC, "bad magic"
    G, H, A = r.u32(), r.u32(), r.u32()
    pot_scale = r.f32s(1)[0]
    hidden = r.u32()
    assert (G, H, A) == (93, 36, 4), (G, H, A)
    prev = {"value": G + H, "strat": G + H + A}
    sizes = {}
    for i, net in enumerate(("value", "strat")):
        fan_in = prev[net]
        for li in LAYERS:
            out_f, in_f = r.u32(), r.u32()
            assert in_f == fan_in, (net, li, in_f, fan_in)
            expect = hidden if li != "4" else 1
            assert out_f == expect, (net, li, out_f, expect)
            r.f32s(out_f * in_f)
            r.f32s(out_f)
            fan_in = out_f
        sizes[net] = fan_in
    assert r.o == len(r.d), f"{len(r.d) - r.o} trailing bytes"
    print(f"selftest ok: {path} G={G} H={H} A={A} hidden={hidden} "
          f"pot_scale={pot_scale}")


def load_net(path: str):
    """stdlib parse -> nested weight lists; reference forward for the C++ port."""
    r = _Reader(open(path, "rb").read())
    if r.take(8) != MAGIC:
        raise ValueError("bad magic")
    G_, H_, A_ = r.u32(), r.u32(), r.u32()
    pot_scale = r.f32s(1)[0]
    hidden = r.u32()
    nets = {}
    for net in ("value", "strat"):
        layers = []
        for _li in LAYERS:
            out_f, in_f = r.u32(), r.u32()
            wf = r.f32s(out_f * in_f)
            bf = r.f32s(out_f)
            layers.append(([wf[i * in_f:(i + 1) * in_f] for i in range(out_f)], bf))
        nets[net] = layers
    if r.o != len(r.d):
        raise ValueError("trailing bytes")
    nets["meta"] = dict(G=G_, H=H_, A=A_, pot_scale=pot_scale, hidden=hidden)
    return nets


def _linear(W, b, x, relu: bool):
    out = [sum(wij * xj for wij, xj in zip(wr, x)) + bi for wr, bi in zip(W, b)]
    if relu:
        out = [v if v > 0.0 else 0.0 for v in out]
    return out


def forward(net_layers, x):
    """Last layer without ReLU. Reference for C++, not for speed."""
    for i, (W, b) in enumerate(net_layers):
        x = _linear(W, b, x, relu=i < len(net_layers) - 1)
    return x[0]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ckpt", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--selftest", metavar="WSNB")
    args = ap.parse_args()
    if args.selftest:
        selftest(args.selftest)
    elif args.ckpt and args.out:
        export(args.ckpt, args.out)
    else:
        ap.error("need ckpt+out, or --selftest WSNB")
        sys.exit(2)
