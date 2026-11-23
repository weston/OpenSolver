# Flop subsets

`wsolver_<N>.txt`: N flops with weights that stand in for all 22,100 flops, for preflop trees
(`add_to_subset`) and for solving one postflop tree over a spread of boards (`tools/jobs.py
--flops-file`, the viewer's Jobs tab). One `flop:weight` per line, weights in percent, the format of the
subset files other solvers use. Each flop stands for its suit class (`Ks8s7h` for the 12 flops with those
ranks and that suit pattern), which is how the solver deals a subset.

These files are our own: chosen by `tools/flop_subsets.cpp` from the rules of the game, with no other
solver's subsets as input or as a target. `make flop-subsets` writes them again (`FLOP_SIZES="10 25"` for
other sizes); on the machine they were made on (an M4 Mac) that gives the same files.

## What is chosen

A preflop solve sees a flop only through what the hands are worth on it. So for every flop class and
every pair of preflop hand classes (169 x 169) the generator counts who is ahead: on the flop itself, on
the turn (over the 45 turn cards) and at the river (over the 990 runouts, which is all-in equity). All
1,755 flop classes at their natural weights give the true tables; the river one is the exact preflop
equity table, which is checked.

A subset is chosen so that its weighted tables are close to the true ones. The error is the one a solve
would see: the squared error of a hand's value against an opponent range, averaged over the three
streets, the hands, and every range (each hand class in it or not). That has two parts per hand: its
error against all hands at once, and its error matchup by matchup. Nothing in it is a tuned weight.
The search adds the flop that lowers the error most, re-solves all weights exactly, and at the end
swaps flops while a swap helps.

## How close they are

RMS error of a hand's share of the pot against a range (1.0 is the whole pot), and what kind of flops
the weights add up to:

| File | flop | turn | river | rainbow / two-tone / monotone | paired |
|---|---|---|---|---|---|
| `wsolver_10.txt` | 0.0218 | 0.0164 | 0.0144 | 46.9 / 39.5 / 13.6 % | 26.1 % |
| `wsolver_25.txt` | 0.0087 | 0.0067 | 0.0065 | 36.5 / 56.5 / 7.0 % | 22.0 % |
| `wsolver_50.txt` | 0.0033 | 0.0024 | 0.0026 | 39.1 / 54.8 / 6.0 % | 21.0 % |
| `wsolver_100.txt` | 0.0012 | 0.0009 | 0.0009 | 39.1 / 55.6 / 5.3 % | 17.5 % |
| `wsolver_200.txt` | 0.0006 | 0.0004 | 0.0003 | 39.3 / 55.6 / 5.2 % | 17.0 % |
| all flops | 0 | 0 | 0 | 39.8 / 55.1 / 5.2 % | 16.9 % |

Random flops of the same number do several times worse, also with their weights solved the same way
(median of 200 draws, all three streets together): 10 flops 0.067 against 0.031 here, 25 flops 0.030
against 0.013, 50 flops 0.016 against 0.005, 100 flops 0.0077 against 0.0018. With the usual weights (how
often each flop is dealt) random flops are at 0.087, 0.057, 0.040 and 0.027.

`build/flop_subsets eval FILE` prints these numbers for any flop file, and `build/flop_subsets equity
FILE AKs QQ,JTs` what a hand's all-in equity against a range becomes through the file's flops, next to
the exact figure. What they do to a solved preflop strategy is measured in
`bench/where-the-time-goes.md` ("Flop subsets").

## Which size

Memory and time grow in step with the number of flops (each is a copy of every exit's postflop tree), so
take the largest the machine and the patience allow. Ten flops are for trying things out: their equities
are right to a couple of percent and every strategy read off them is coarse.
