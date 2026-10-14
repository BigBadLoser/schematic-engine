# CP-SAT placement prototype vs greedy (run auto-st3, recorded refine calls)

Scorer: `_Model(start symbols, Weights())`, same for all variants (turn/flip/move relative to the recorded start). Variants: a start, b greedy (`replay(rounds=2, orient=0)`), c CP-SAT (num_workers=1, max_deterministic_time=20, seed 0, hint=start), d CP-SAT + `refine_placement` polish.
Group = index in `load_circuits`; group 4 = buck (8/8 movable); groups 10 and 20 = the two 14/14 netlist trials (LM317 is one of them; not labelled in the dump). Wall s includes two processes of this script running concurrently by mistake (inflates times); CP-SAT hit its 20 deterministic-s limit (about 90-120 wall s) on all but group 110.
Simplifications: only wire pairs, power facing, orientation, extent, no-overlap (label nets, crossings, clearance, through-body, move cost not modeled); positions rounded to 1.27 mm cells (unmoved symbols keep exact mm); chain order for >3 pins by recorded tip x then y; wire weight = net.weight; 100x integer costs; overlap uses padded `box`.
Images: `out/g<N>_b.png` (greedy) and `out/g<N>_d.png` (CP-SAT+polish), SVGs for all four variants in `out/`.


### group 4: 8 symbols, 8 movable, 6 nets, 9 wire pairs; CP-SAT FEASIBLE obj=9259 bound=-1306

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 232.1 | 124.5 | 10 | 0 | 0 | 1 | 0 | 0.0 |
| b greedy | 110.9 | 53.3 | 4 | 0 | 0 | 0 | 0 | 51.2 |
| c cpsat | 655.5 | 54.6 | 4 | 0 | 0 | 0 | 0 | 106.9 |
| d cpsat+polish | 162.0 | 77.5 | 8 | 0 | 0 | 0 | 0 | 119.1 |

### group 10: 14 symbols, 14 movable, 9 nets, 19 wire pairs; CP-SAT FEASIBLE obj=30799 bound=-734

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 891.0 | 232.4 | 22 | 1 | 4 | 5 | 1 | 0.0 |
| b greedy | 350.7 | 203.2 | 16 | 0 | 1 | 0 | 0 | 126.6 |
| c cpsat | 1778.5 | 232.4 | 20 | 0 | 1 | 13 | 0 | 120.0 |
| d cpsat+polish | 387.2 | 199.4 | 16 | 0 | 1 | 0 | 0 | 170.4 |

### group 20: 14 symbols, 14 movable, 10 nets, 15 wire pairs; CP-SAT FEASIBLE obj=23431 bound=3916

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 363.4 | 137.2 | 16 | 0 | 0 | 2 | 0 | 0.0 |
| b greedy | 304.2 | 133.4 | 12 | 0 | 0 | 2 | 0 | 21.5 |
| c cpsat | 1427.7 | 113.0 | 8 | 1 | 0 | 6 | 0 | 120.0 |
| d cpsat+polish | 323.3 | 144.8 | 12 | 0 | 0 | 0 | 0 | 138.3 |

### group 42: 5 symbols, 5 movable, 4 nets, 6 wire pairs; CP-SAT FEASIBLE obj=11928 bound=6004

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 269.7 | 81.3 | 4 | 0 | 2 | 1 | 0 | 0.0 |
| b greedy | 138.5 | 48.3 | 1 | 0 | 1 | 0 | 0 | 62.5 |
| c cpsat | 923.9 | 31.7 | 4 | 0 | 1 | 1 | 0 | 90.7 |
| d cpsat+polish | 154.4 | 50.8 | 2 | 0 | 1 | 0 | 0 | 127.8 |

### group 58: 23 symbols, 23 movable, 13 nets, 29 wire pairs; CP-SAT FEASIBLE obj=47168 bound=-1012

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 532.5 | 260.4 | 26 | 2 | 3 | 2 | 0 | 0.0 |
| b greedy | 386.3 | 219.7 | 16 | 0 | 2 | 0 | 0 | 146.3 |
| c cpsat | 2447.1 | 274.3 | 23 | 0 | 2 | 10 | 0 | 120.0 |
| d cpsat+polish | 599.5 | 290.8 | 22 | 0 | 1 | 2 | 0 | 162.9 |

### group 76: 5 symbols, 5 movable, 6 nets, 7 wire pairs; CP-SAT FEASIBLE obj=12729 bound=3616

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 241.9 | 119.4 | 4 | 0 | 1 | 1 | 0 | 0.0 |
| b greedy | 123.4 | 63.5 | 4 | 0 | 0 | 0 | 0 | 74.2 |
| c cpsat | 322.0 | 45.7 | 4 | 1 | 1 | 1 | 0 | 87.9 |
| d cpsat+polish | 140.7 | 48.3 | 2 | 0 | 0 | 1 | 0 | 107.3 |

### group 84: 6 symbols, 6 movable, 6 nets, 6 wire pairs; CP-SAT FEASIBLE obj=6720 bound=-754

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 82.0 | 24.1 | 6 | 0 | 0 | 0 | 0 | 0.0 |
| b greedy | 82.0 | 24.1 | 6 | 0 | 0 | 0 | 0 | 26.9 |
| c cpsat | 110.5 | 16.5 | 2 | 0 | 0 | 1 | 0 | 101.3 |
| d cpsat+polish | 83.3 | 19.0 | 2 | 0 | 0 | 0 | 0 | 118.9 |

### group 110: 5 symbols, 5 movable, 3 nets, 0 wire pairs; CP-SAT OPTIMAL obj=5892 bound=5892

| variant | total | wire mm | bends | jogs | cross | through | overlaps | wall s |
|---|---|---|---|---|---|---|---|---|
| a start | 66.6 | 0.0 | 0 | 0 | 0 | 0 | 0 | 0.0 |
| b greedy | 61.7 | 0.0 | 0 | 0 | 0 | 0 | 0 | 37.6 |
| c cpsat | 686.8 | 0.0 | 0 | 0 | 0 | 0 | 0 | 36.2 |
| d cpsat+polish | 84.7 | 0.0 | 0 | 0 | 0 | 0 | 0 | 55.2 |