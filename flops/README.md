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
streets, the hands, and the ranges. A range is a run of hands in order of strength: one hand class, the
best x %, the worst x %, a band in between, or all hands, every run as likely as any other. Errors of one
sign add up along a run, so a subset cannot be a little kind to weak hands in every matchup; that is
what went wrong first, and it moved whole preflop ranges (`bench/where-the-time-goes.md`, "Flop
subsets"). Nothing in the error is a tuned weight. The search adds the flop that lowers the error most,
re-solves all weights exactly, and at the end swaps flops while a swap helps.

## How close they are

RMS error of a hand's share of the pot against a range (1.0 is the whole pot; a range counts for the
share of the opponent's hands it holds), and what kind of flops the weights add up to:

| File | flop | turn | river | rainbow / two-tone / monotone | paired | trips |
|---|---|---|---|---|---|---|
| `wsolver_10.txt` | 0.0104 | 0.0075 | 0.0061 | 36.4 / 44.9 / 18.7 % | 9.2 % | 0 |
| `wsolver_25.txt` | 0.0040 | 0.0029 | 0.0026 | 39.5 / 52.0 / 8.5 % | 16.0 % | 1.3 % |
| `wsolver_50.txt` | 0.0016 | 0.0012 | 0.0013 | 40.3 / 54.0 / 5.8 % | 17.9 % | 0.9 % |
| `wsolver_100.txt` | 0.0005 | 0.0004 | 0.0004 | 39.1 / 55.5 / 5.4 % | 16.6 % | 0.7 % |
| `wsolver_200.txt` | 0.0002 | 0.0001 | 0.0001 | 40.1 / 54.7 / 5.2 % | 16.1 % | 0.6 % |
| all flops | 0 | 0 | 0 | 39.8 / 55.1 / 5.2 % | 16.9 % | 0.2 % |

Ten flops cannot look like the deck (one monotone flop is already too many, and none is too few for the
suited hands); from 50 on the textures are the deck's.

Random flops of the same number are 2 to 4 times further off, also with their weights solved the same
way (median of 200 draws, all three streets together): 10 flops 0.030 against 0.014 here, 25 flops 0.013
against 0.0055, 50 flops 0.0071 against 0.0024, 100 flops 0.0034 against 0.0008. With the usual weights
(how often each flop is dealt) random flops are at 0.040, 0.026, 0.018 and 0.013.

`build/flop_subsets eval FILE` prints these numbers for any flop file, and `build/flop_subsets equity
FILE AKs QQ,JTs` what a hand's all-in equity against a range becomes through the file's flops, next to
the exact figure. What they do to a solved preflop strategy is measured in
`bench/where-the-time-goes.md` ("Flop subsets").

## Which size

Memory and time grow in step with the number of flops (each is a copy of every exit's postflop tree), so
take the largest the machine and the patience allow. Measured on one heads-up tree (100 big blinds, flop
betting): 100 flops gave the preflop strategy of 200 flops to within what two solves of the same tree
differ by, and 50 flops came close (every action's frequency within 1 point). 25 and 10 flops kept the
opening ranges but had the big blind fold 4 points too often to a raise. Ten flops are for trying things
out.
