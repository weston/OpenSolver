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

A subset is chosen so that its weighted tables are close to the true ones. The error that is made small
is the squared error of a hand's value against an opponent range, added over the three streets and
averaged over the hands and the ranges. A range is a run of hands in order of strength: one hand class, the
best x %, the worst x %, a band in between, or all hands, every run as likely as any other. Errors of one
sign add up along a run, so a subset cannot be a little kind to weak hands in every matchup; that is
what went wrong first, and it moved whole preflop ranges (`bench/where-the-time-goes.md`, "Flop
subsets"). Nothing in the error is a tuned weight. The search adds the flop that lowers the error most,
re-solves all weights exactly, and at the end swaps flops while a swap helps.

## How close they are

Two errors, both RMS over the hands with 1.0 the whole pot. *Fitted* is what the search makes small: a
hand against a range that is a run of hands, where a range counts for the share of the opponent's hands
it holds, so most runs are small and so is the number. *All hands* is a hand's equity against every
other hand, which is the larger and plainer figure: at the river it is the error of the hand's all-in
equity.

| File | fitted: flop / turn / river | all hands: flop / turn / river | rainbow / two-tone / monotone | paired | trips |
|---|---|---|---|---|---|
| `wsolver_10.txt` | 0.0104 / 0.0075 / 0.0061 | 0.0250 / 0.0189 / 0.0159 | 36.4 / 44.9 / 18.7 % | 9.2 % | 0 |
| `wsolver_25.txt` | 0.0040 / 0.0029 / 0.0026 | 0.0087 / 0.0070 / 0.0067 | 39.5 / 52.0 / 8.5 % | 16.0 % | 1.3 % |
| `wsolver_50.txt` | 0.0016 / 0.0012 / 0.0013 | 0.0032 / 0.0030 / 0.0033 | 40.3 / 54.0 / 5.8 % | 17.9 % | 0.9 % |
| `wsolver_100.txt` | 0.0005 / 0.0004 / 0.0004 | 0.0012 / 0.0011 / 0.0011 | 39.1 / 55.5 / 5.4 % | 16.6 % | 0.7 % |
| `wsolver_200.txt` | 0.0002 / 0.0001 / 0.0001 | 0.0004 / 0.0004 / 0.0004 | 40.1 / 54.7 / 5.2 % | 16.1 % | 0.6 % |
| all flops | 0 | 0 | 39.8 / 55.1 / 5.2 % | 16.9 % | 0.2 % |

Single hands and single matchups are further off than the averages: through the 10 flops the worst hand
class is 4.3 points of equity from the truth (J7o against all hands) and JTs against 22 is 8.8 points
off; through 25 flops the worst class is 2.4 points off, through 50 flops 1.1.

Ten flops cannot look like the deck (one monotone flop is already too many, and none is too few for the
suited hands). From 50 on the suits and the pairs are close to the deck's; flops of three of a kind
keep 2 to 4 times their share of the weight in every file that has one.

Random flops of the same number are 2 to 4 times further off on the fitted error, also with their
weights solved the same way (median of 200 draws; the three streets' squared errors added, so these are
larger than a column above): 10 flops 0.030 against 0.014 here, 25 flops 0.013 against 0.0055, 50 flops
0.0071 against 0.0024, 100 flops 0.0034 against 0.0008. With the usual weights (how often each flop is
dealt) random flops are at 0.040, 0.026, 0.018 and 0.013. That is the fitted error, not a solve: on the
one solved tree below, 25 of these flops gave hand-by-hand strategies clearly closer to the 200-flop
ones than 25 random flops did, but overall frequencies and EV no closer than the random draws.

`build/flop_subsets eval FILE` prints these numbers for any flop file, and `build/flop_subsets equity
FILE AKs QQ,JTs` what a hand's all-in equity against a range becomes through the file's flops, next to
the exact figure. What they do to a solved preflop strategy is measured in
`bench/where-the-time-goes.md` ("Flop subsets").

## Which size

Memory and time grow in step with the number of flops (each is a copy of every exit's postflop tree), so
take the largest the machine and the patience allow. Measured on one heads-up tree (100 big blinds, flop
betting): with 100 and with 50 flops every action's frequency was within 1 point of the 200-flop solve
and the EV within 0.004 chips of a 10-chip big blind, which is about what two solves of the same tree
differ by; hand by hand, 100 flops were near that too and 50 flops about twice as far. 25 and 10 flops
kept the opening ranges but had the big blind fold 4 points too often to a raise. Ten flops are for
trying things out.
