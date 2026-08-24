# Capacity Planning Model

Roll a product mix through routed workcenters, find the constraint, and estimate lead time with **Kingman's equation** — so the model answers not just *"do we have enough capacity?"* but *"what is our utilization doing to lead time before capacity runs out?"*

Those are different questions, and the second one is usually the one that hurts.

Pure Python standard library. No `pip install`. Clone and run.

---

## Why this exists

Every capacity spreadsheet compares required hours to available hours and colours the cell red when required wins. That model says a workcenter at 90% utilization is fine — it has slack.

It is not fine. At 90% utilization with ordinary variability, a job waits far longer in queue than it spends being worked on. Capacity planning that stops at load-versus-capacity hands you a plan that is technically feasible and operationally miserable, and nobody understands why the shop is late when the numbers said it fit.

This model reports both, side by side.

## What it computes

**Load vs. capacity**

Available hours are derated honestly: `machines × hours/shift × shifts × days × availability × efficiency`. Required hours include **setup**, rolled up from batch counts — because in high-mix, low-volume work setup is not a rounding error, it is often a third of the load.

**The constraint**, with sized options to close a gap: machines to add, shifts to add, overtime hours, SMED savings when setup is a large share of load, and the batch-size tradeoff.

**Lead time**, via Kingman's VUT approximation:

```
Wq  =  ( (ca² + cs²) / 2 )  ×  ( u / (1 − u) )  ×  te
       └── Variability ──┘     └ Utilization ┘     Time
```

with the Sakasegawa correction for `m` parallel machines. Queue time returns **infinity at or above 100% utilization** — that is not a bug, it is the honest answer. A queue at full utilization does not reach steady state; it grows without bound.

The `u/(1−u)` term is the whole lesson. It roughly doubles between 80% and 90%, and doubles again between 90% and 95%. Utilization is not free, and the price is paid in lead time long before it is paid in missed capacity.

## Usage

```bash
python capacity.py --workcenters data/workcenters.csv --routings data/routings.csv --demand data/demand.csv
```

| Flag | Meaning |
|---|---|
| `--workcenters` | Workcenter definitions CSV |
| `--routings` | Product routing CSV |
| `--demand` | Demand CSV |
| `--scale` | Multiply all demand, e.g. `1.25` to test a 25% increase |

Exit code is `0` when everything fits and `3` when any workcenter is overloaded. `--scale` makes it a one-line what-if: *at what demand does this shop break?*

### Input format

`workcenters.csv` — only `workcenter`, `hours_per_shift`, and `days` are required; the rest default sensibly.

```csv
workcenter,machines,hours_per_shift,shifts_per_day,days,availability,efficiency,cv_arrival,cv_service
PAINT,1,8,1,20,0.80,0.85,1.3,0.9
ASSEMBLY,6,8,1,20,0.92,0.88,1.0,1.2
```

`routings.csv`

```csv
product,workcenter,run_hours_per_unit,setup_hours_per_batch
TAMPER-A,PAINT,1.2,2.0
TAMPER-A,ASSEMBLY,6.5,0.5
```

`demand.csv`

```csv
product,units,batch_size
TAMPER-A,24,4
```

Demand for an unrouted product, routings to an unknown workcenter, duplicate workcenters, and availability outside `(0, 1]` are all rejected at load rather than silently producing a plan.

## Example output

Six workcenters, four products, a 20-day period:

```
============================================================================
                               CAPACITY PLAN
============================================================================

  Products        4
  Workcenters     6
  Total units     82

----------------------------------------------------------------------------
LOAD VS CAPACITY
----------------------------------------------------------------------------

  Workcenter        Req h  Avail h    Util   Slack h  Loading
  ------------------------------------------------------------------------
  PAINT                98      109   90.1%        11  [######################..]  WARN
  TEST                148      259   57.1%       111  [##############..........]
  CUT                 145      259   55.8%       115  [#############...........]
  ASSEMBLY            432      777   55.6%       345  [#############...........]
  MACHINE             349      694   50.3%       345  [############............]
  WELD                200      507   39.5%       306  [#########...............]

----------------------------------------------------------------------------
CONSTRAINT
----------------------------------------------------------------------------

  PAINT at 90.1% utilization

    Required           98 h
    Available          109 h
    Run                62 h
    Setup              36 h (36.7% of required)
    Batches            18

    Fits, with 11 h of slack.
    But see the lead time section -- capacity is not the binding issue.

----------------------------------------------------------------------------
LEAD TIME (Kingman VUT: queue + process, per batch)
----------------------------------------------------------------------------

  Workcenter        Util    Process        Queue        Cycle     Q/P
  ------------------------------------------------------------------------
  PAINT            90.1%       5.4h       61.8 h       67.2 h   11.3x
  TEST             57.1%       6.2h        5.4 h       11.6 h    0.9x
  CUT              55.8%       6.0h        2.4 h        8.4 h    0.4x
  ASSEMBLY         55.6%      18.0h        1.7 h       19.7 h    0.1x
  MACHINE          50.3%      14.5h        3.7 h       18.2 h    0.3x
  WELD             39.5%      11.1h        0.9 h       12.0 h    0.1x

  Estimated door-to-door lead time by product:

    GRINDER-C    137.1 h
    REGULATOR-D  57.9 h
    TAMPER-A     137.1 h
    TAMPER-B     137.1 h

  1 workcenter(s) at or above 85% utilization.
  Queue time grows non-linearly here: the u/(1-u) term roughly doubles
  between 80% and 90%, and doubles again between 90% and 95%. Buying
  back a few points of utilization buys back disproportionate lead time.
============================================================================
```

### Reading this result

**PAINT passes the capacity test and fails the business.** It has 11 hours of slack — a traditional capacity plan would colour it green and move on. But at 90.1% utilization a batch waits **61.8 hours in queue to receive 5.4 hours of work.** That single workcenter contributes about **half of the 137-hour door-to-door lead time** on three of the four products.

Every other workcenter is under 60% and effectively invisible in the lead time. ASSEMBLY has the longest processing time in the shop at 18 hours per batch and contributes almost no queue at all. **Where the work is is not where the time is.**

And the fix is not "buy a paint booth." Look at the constraint breakdown: **36.7% of PAINT's load is setup**, 36 hours of it across 18 batches. Halving setup through SMED takes utilization from 90% to roughly 74% — which, through the `u/(1−u)` term, cuts queue time by roughly two thirds. Same booth, same shifts, most of the lead time gone.

Run `--scale 1.25` and PAINT goes to 113.5% — overloaded, short by 15 hours, and the tool sizes the options. The interesting part is that the lead time problem showed up **a full 25% of demand before** the capacity problem did. That gap is the warning the standard spreadsheet never gives you.

## Tests

```bash
python -m unittest discover -s tests -v
```

25 tests covering derated capacity, setup rollup and batch rounding, machines-needed sizing, and the queueing model's required behaviours: queue grows super-linearly with utilization, goes unbounded at 100%, vanishes at zero variability, rises with variability, and falls with parallel machines.

## Author

**Varun Mysore Vinay** — Manufacturing Engineer
[LinkedIn](https://www.linkedin.com/in/varunmysorevinay) · varunmysorevinay@gmail.com

## License

MIT — see [LICENSE](LICENSE).
