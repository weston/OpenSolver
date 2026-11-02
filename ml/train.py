#!/usr/bin/env python3
"""Training harness for the river strategy model (#59, stage 2).

Trains two small MLPs per decision node of a dumped spot:

- value_net(g, h) -> predicted counterfactual value of hand h (chips / pot)
- strat_net(g, h, a) -> logit for (hand, action); softmax over actions
  (all actions in a record are legal, so no legality mask is needed)

Loss = reach-weighted KL(target avg-strategy || softmax(logits))
     + lambda_v * MSE(value, ev / pot).

Encoding happens on the fly (~2 ms/spot), so nothing is materialised: an
epoch streams spot files from disk. Spots are split train/holdout by key
hash, so the split is stable across corpus growth.

Checkpoints are torch .pt files carrying the feature-layout constants; stage
3 (C++ inference) will need the weights in a portable format — export then,
from the checkpoint.

Usage: python3 ml/train.py --data ~/wsolver-data/river_v1 --spots 200 \
          --epochs 2 --steps-out 10          # quick sanity
       python3 ml/train.py --eval ml/out/river_v1.pt   # holdout metrics only

Requires torch (CPU is fine): not in stdlib, not needed by anything else in
the repo; every other ml/ tool stays stdlib-only.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features
from loader import load_spot

G, H, A = features.G_DIM, features.H_DIM, features.A_DIM
LAMBDA_V = 0.1  # value-loss weight relative to strategy KL


# ---------------------------------------------------------------------------
# Spot -> padded node tensors


def spot_tensors(spot):
    """node dicts with small python lists; converted to tensors at batch time.

    Nodes with fewer than 2 actions carry no strategy signal and are skipped.
    """
    out = []
    for row, rec in zip(features.encode_spot(spot), spot.records):
        na = len(rec.actions)
        if na < 2:
            continue
        # normalise target rows (dump rows are normalised to ~1 already)
        strat = [list(s) for s in rec.strat]  # [action][hand]
        nh = len(row["hs"])
        target = []
        for h in range(nh):
            col = [max(strat[a][h], 0.0) for a in range(na)]
            tot = sum(col)
            target.append([c / tot if tot > 0 else 1.0 / na for c in col])
        # ev_player is the UNnormalised cfv; chip EV = cfv / compat mass
        # (calc_ev semantics). Blocked hands (mass ~ 0) get ~0 reach weight
        # anyway, so a guard is enough.
        pot = max(spot.start_pot, 1e-9)
        masses = features.compat_masses(spot, rec)
        out.append({
            "g": row["g"],
            "hs": row["hs"],
            "as_": row["as_"],
            "target": target,                     # [hand][action]
            "weight": [max(r, 1e-4) for r in rec.reach_player],
            "ev": [e / m / pot if m > 1e-6 else 0.0
                   for e, m in zip(rec.ev_player, masses)],
        })
    return out


def node_batch(torch, nodes, device):
    """Pad a list of node dicts to dense tensors."""
    B = len(nodes)
    Hm = max(len(n["hs"]) for n in nodes)
    Am = max(len(n["as_"]) for n in nodes)
    g = torch.zeros(B, Hm, Am, G)
    hh = torch.zeros(B, Hm, Am, H)
    aa = torch.zeros(B, Hm, Am, A)
    tgt = torch.zeros(B, Hm, Am)
    w = torch.zeros(B, Hm)
    ev = torch.zeros(B, Hm)
    hmask = torch.zeros(B, Hm, dtype=torch.bool)
    amask = torch.zeros(B, Hm, Am, dtype=torch.bool)
    for b, n in enumerate(nodes):
        nh, na = len(n["hs"]), len(n["as_"])
        gt = torch.tensor(n["g"])
        for hi in range(nh):
            ht = torch.tensor(n["hs"][hi])
            for a in range(na):
                g[b, hi, a] = gt
                hh[b, hi, a] = ht
                aa[b, hi, a] = torch.tensor(n["as_"][a])
                tgt[b, hi, a] = n["target"][hi][a]
                amask[b, hi, a] = True
            w[b, hi] = n["weight"][hi]
            ev[b, hi] = n["ev"][hi]
            hmask[b, hi] = True
    return {k: v.to(device) for k, v in
            dict(g=g, hh=hh, aa=aa, tgt=tgt, w=w, ev=ev,
                 hmask=hmask, amask=amask).items()}


# ---------------------------------------------------------------------------
# Model


def build_model(torch, hidden):
    value = torch.nn.Sequential(
        torch.nn.Linear(G + H, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, 1))
    strat = torch.nn.Sequential(
        torch.nn.Linear(G + H + A, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, 1))
    return value, strat


def forward(batch, value_net, strat_net):
    gh = torch_import.cat([batch["g"][:, :, 0], batch["hh"][:, :, 0]], dim=-1)
    v = value_net(gh).squeeze(-1)                        # [B, Hm]
    x = torch_import.cat([batch["g"], batch["hh"], batch["aa"]], dim=-1)
    logits = strat_net(x).squeeze(-1)                    # [B, Hm, Am]
    logits = logits.masked_fill(~batch["amask"], -1e9)
    return v, logits


class _TorchProxy:  # avoid a hard import at module load (tests/CI stay torch-free)
    def __getattr__(self, name):
        return getattr(_TORCH, name)


_TORCH = None
torch_import = _TorchProxy()


def losses(torch, batch, v, logits):
    logp = torch.log_softmax(logits, dim=-1)
    kl_per_hand = -(batch["tgt"] * logp).sum(-1)         # [B, Hm] (target sums to 1)
    w = batch["w"] * batch["hmask"]
    wsum = w.sum().clamp_min(1e-9)
    kl = (kl_per_hand * w).sum() / wsum
    mse = (((v - batch["ev"]) ** 2) * w).sum() / wsum
    return kl, mse


# ---------------------------------------------------------------------------
# Corpus iteration


def list_spots(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, "*.wsdt.gz")))


def is_holdout(path):
    return int(os.path.basename(path)[:8], 16) % 100 == 0  # 1%


def eval_holdout(torch, files, value_net, strat_net, device, cap=50):
    tot_kl = tot_klu = tot_mse = n = 0.0
    for fp in files[:cap]:
        try:
            nodes = spot_tensors(load_spot(fp, strict=False))
        except Exception:
            continue
        if not nodes:
            continue
        b = node_batch(torch, nodes, device)
        with torch.no_grad():
            v, logits = forward(b, value_net, strat_net)
        kl, mse = losses(torch, b, v, logits)
        A_uniform = b["amask"].sum(-1, keepdim=True).clamp_min(1)
        uni = b["amask"].float() / A_uniform
        klu = (-(b["tgt"] * torch.log(uni + 1e-12)).sum(-1) * b["w"] * b["hmask"]).sum()
        klu /= (b["w"] * b["hmask"]).sum().clamp_min(1e-9)
        tot_kl += float(kl); tot_mse += float(mse); tot_klu += float(klu); n += 1
    n = max(n, 1)
    return tot_kl / n, tot_klu / n, tot_mse / n


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/wsolver-data/river_v1"))
    ap.add_argument("--spots", type=int, default=0, help="0 = all")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--batch-spots", type=int, default=16)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=59)
    ap.add_argument("--steps-out", type=int, default=200, help="log every N steps")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--out", default="ml/out/river_v1.pt")
    ap.add_argument("--eval", default=None, help="checkpoint: holdout metrics only")
    args = ap.parse_args()

    import torch  # deferred: module must import cleanly on torch-free machines
    global _TORCH
    _TORCH = torch
    torch.manual_seed(args.seed)
    torch.set_num_threads(args.threads)
    device = "cpu"

    files = list_spots(args.data)
    if args.spots:
        files = files[: args.spots + args.spots // 99 + 2]
    train_files = [f for f in files if not is_holdout(f)]
    hold_files = [f for f in files if is_holdout(f)]
    if args.spots:
        train_files = train_files[: args.spots]
    print(f"{len(train_files)} train + {len(hold_files)} holdout spots", flush=True)

    value_net, strat_net = build_model(torch, args.hidden)

    if args.eval:
        ck = torch.load(args.eval, map_location=device)
        value_net.load_state_dict(ck["value"]); strat_net.load_state_dict(ck["strat"])
        value_net.eval(); strat_net.eval()
        kl, klu, mse = eval_holdout(torch, hold_files, value_net, strat_net, device,
                                    cap=len(hold_files))
        print(f"holdout: KL={kl:.4f} (uniform baseline {klu:.4f}, "
              f"ratio {kl / max(klu, 1e-9):.3f})  value MSE={mse:.5f}")
        return

    value_net.to(device); strat_net.to(device)
    params = list(value_net.parameters()) + list(strat_net.parameters())
    opt = torch.optim.Adam(params, lr=args.lr)
    rng = random.Random(args.seed)
    step = 0
    t0 = time.time()

    for epoch in range(args.epochs):
        rng.shuffle(train_files)
        pending = []
        for fp in train_files:
            try:
                pending += spot_tensors(load_spot(fp, strict=False))
            except Exception as e:
                print(f"skip {fp}: {e}", flush=True)
                continue
            if len(pending) < args.batch_spots * 2:   # ~13 nodes/river spot
                continue
            b = node_batch(torch, pending, device)
            pending = []
            v, logits = forward(b, value_net, strat_net)
            kl, mse = losses(torch, b, v, logits)
            loss = kl + LAMBDA_V * mse
            opt.zero_grad(); loss.backward(); opt.step()
            step += 1
            if step % args.steps_out == 0:
                value_net.eval(); strat_net.eval()
                ekl, eklu, emse = eval_holdout(torch, hold_files, value_net,
                                               strat_net, device)
                value_net.train(); strat_net.train()
                print(f"step {step} epoch {epoch}: KL {float(kl.detach()):.4f}  "
                      f"holdout KL {ekl:.4f} (uniform {eklu:.4f}, "
                      f"ratio {ekl / max(eklu, 1e-9):.3f})  vMSE {emse:.5f}  "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    torch.save({
        "version": 1,
        "dims": {"G": G, "H": H, "A": A},
        "pot_scale": features.POT_SCALE,
        "hidden": args.hidden,
        "value": value_net.state_dict(),
        "strat": strat_net.state_dict(),
    }, args.out)
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    main()
