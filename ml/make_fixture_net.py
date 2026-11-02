#!/usr/bin/env python3
"""Regenerate ml/tests/fixture.wsnb: a tiny deterministic river net used by
tests/test_upi.py's warm_start check. NOT the trained model — just a seeded
MLP with the real feature dims (93/36/4) but hidden width 8, exported through
the same WSNW0001 path as real checkpoints so the test pins the C++ reader.

Requires torch (like all training-side tooling). Run from the repo root:
    python3 ml/make_fixture_net.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features
import export

HIDDEN = 8
SEED = 5908
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixture.wsnb")


def main():
    import torch

    torch.manual_seed(SEED)
    dims = {"G": features.G_DIM, "H": features.H_DIM, "A": features.A_DIM}
    nets = {}
    for name, fan_in in (("value", dims["G"] + dims["H"]),
                         ("strat", dims["G"] + dims["H"] + dims["A"])):
        layers = torch.nn.Sequential(
            torch.nn.Linear(fan_in, HIDDEN), torch.nn.ReLU(),
            torch.nn.Linear(HIDDEN, HIDDEN), torch.nn.ReLU(),
            torch.nn.Linear(HIDDEN, 1))
        with torch.no_grad():  # keep logits small so softmaxes stay well-mixed
            for lin in (layers[0], layers[2], layers[4]):
                lin.weight.mul_(0.1)
                lin.bias.uniform_(-0.3, 0.3)
            layers[4].bias.zero_()
        nets[name] = {k: v.clone() for k, v in layers.state_dict().items()}
    ck = {"version": 1, "dims": dims, "pot_scale": features.POT_SCALE,
          "hidden": HIDDEN, "value": nets["value"], "strat": nets["strat"]}
    tmp = OUT + ".pt.tmp"
    torch.save(ck, tmp)
    export.export(tmp, OUT)
    os.unlink(tmp)
    export.selftest(OUT)


if __name__ == "__main__":
    main()
