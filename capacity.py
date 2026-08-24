"""Capacity planning and lead time estimation for high-mix, low-volume shops.

Loads a product mix against routed workcenters, finds the constraint, and
estimates queueing lead time with Kingman's equation -- so the model shows not
just whether capacity is sufficient, but what utilization is doing to lead time
before capacity runs out.

Standard library only -- no install step.

Usage:
    python capacity.py --workcenters data/workcenters.csv \
        --routings data/routings.csv --demand data/demand.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass


# Above this, lead time starts climbing steeply; past ~0.95 it is a cliff.
UTILIZATION_WARN = 0.85
UTILIZATION_CRITICAL = 0.95


@dataclass
class WorkCenter:
    """A resource group: machines, staffing pattern, and losses."""

    name: str
    machines: int
    hours_per_shift: float
    shifts_per_day: float
    days: float
    availability: float   # uptime fraction
    efficiency: float     # performance against standard
    cv_arrival: float     # CV of time between job arrivals
    cv_service: float     # CV of processing time

    @property
    def gross_hours(self) -> float:
        return self.machines * self.hours_per_shift * self.shifts_per_day * self.days

    @property
    def available_hours(self) -> float:
        """Hours actually usable after uptime and performance losses."""
        return self.gross_hours * self.availability * self.efficiency

    def hours_per_machine_added(self) -> float:
        return self.available_hours / self.machines if self.machines else 0.0


@dataclass
class RoutingStep:
    """One operation: a product at a workcenter."""

    product: str
    workcenter: str
    run_hours_per_unit: float
    setup_hours_per_batch: float


@dataclass
class DemandLine:
    product: str
    units: float
    batch_size: float

    @property
    def batches(self) -> float:
        return math.ceil(self.units / self.batch_size) if self.batch_size > 0 else 0


@dataclass
class Load:
    """Computed load at one workcenter."""

    workcenter: WorkCenter
    run_hours: float
    setup_hours: float
    batches: float

    @property
    def required_hours(self) -> float:
        return self.run_hours + self.setup_hours

    @property
    def utilization(self) -> float:
        available = self.workcenter.available_hours
        return self.required_hours / available if available else math.inf

    @property
    def slack_hours(self) -> float:
        return self.workcenter.available_hours - self.required_hours

    @property
    def is_overloaded(self) -> bool:
        return self.utilization > 1.0

    @property
    def setup_share(self) -> float:
        return self.setup_hours / self.required_hours if self.required_hours else 0.0

    @property
    def hours_per_batch(self) -> float:
        """Effective process time for one batch at this workcenter."""
        return self.required_hours / self.batches if self.batches else 0.0

    def machines_needed(self) -> int:
        """Machines required to bring utilization to 1.0 or below."""
        per_machine = self.workcenter.hours_per_machine_added()
        if per_machine <= 0:
            return self.workcenter.machines
        return math.ceil(self.required_hours / per_machine - 1e-9)

    def queue_hours(self) -> float:
        """Expected wait before processing, via Kingman's VUT equation.

            Wq = ( (ca^2 + cs^2) / 2 ) * ( u / (1 - u) ) * te

        Variability x Utilization x Time. The middle term is why a line at 95%
        utilization has roughly four times the queue of one at 80%, with no
        change in variability at all.

        Returns infinity at or above 100% utilization -- the queue does not
        reach steady state, it grows without bound.
        """
        u = self.utilization
        if u >= 1.0:
            return math.inf

        wc = self.workcenter
        variability = (wc.cv_arrival**2 + wc.cv_service**2) / 2

        # Multi-machine correction (Sakasegawa): the queue shrinks roughly as
        # u^(sqrt(2(m+1)) - 1) / (m(1-u)) for m parallel machines.
        m = max(wc.machines, 1)
        utilization_term = u ** (math.sqrt(2 * (m + 1)) - 1) / (m * (1 - u))

        return variability * utilization_term * self.hours_per_batch

    def cycle_hours(self) -> float:
        """Queue time plus processing time for one batch."""
        return self.queue_hours() + self.hours_per_batch


def load_workcenters(path: str) -> dict[str, WorkCenter]:
    centers: dict[str, WorkCenter] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            def num(key: str, default: float | None = None) -> float:
                raw = (row.get(key) or "").strip()
                if not raw:
                    if default is None:
                        raise ValueError(f"{path} line {line_no}: missing {key}")
                    return default
                return float(raw)

            name = row["workcenter"].strip()
            if name in centers:
                raise ValueError(f"{path} line {line_no}: duplicate workcenter {name}")

            availability = num("availability", 1.0)
            efficiency = num("efficiency", 1.0)
            for label, value in (("availability", availability), ("efficiency", efficiency)):
                if not 0 < value <= 1:
                    raise ValueError(
                        f"{path} line {line_no}: {label} must be in (0, 1]"
                    )

            centers[name] = WorkCenter(
                name=name,
                machines=int(num("machines", 1)),
                hours_per_shift=num("hours_per_shift"),
                shifts_per_day=num("shifts_per_day", 1.0),
                days=num("days"),
                availability=availability,
                efficiency=efficiency,
                cv_arrival=num("cv_arrival", 1.0),
                cv_service=num("cv_service", 1.0),
            )

    if not centers:
        raise ValueError(f"No workcenters found in {path}")
    return centers


def load_routings(path: str) -> list[RoutingStep]:
    steps: list[RoutingStep] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            steps.append(
                RoutingStep(
                    product=row["product"].strip(),
                    workcenter=row["workcenter"].strip(),
                    run_hours_per_unit=float(row["run_hours_per_unit"]),
                    setup_hours_per_batch=float(row.get("setup_hours_per_batch") or 0),
                )
            )
    if not steps:
        raise ValueError(f"No routing steps found in {path}")
    return steps


def load_demand(path: str) -> list[DemandLine]:
    lines: list[DemandLine] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            units = float(row["units"])
            batch = float(row.get("batch_size") or 1)
            if units <= 0 or batch <= 0:
                raise ValueError(
                    f"{path} line {line_no}: units and batch_size must be positive"
                )
            lines.append(
                DemandLine(product=row["product"].strip(), units=units, batch_size=batch)
            )
    if not lines:
        raise ValueError(f"No demand lines found in {path}")
    return lines


def compute_loads(
    centers: dict[str, WorkCenter],
    routings: list[RoutingStep],
    demand: list[DemandLine],
) -> dict[str, Load]:
    """Roll the product mix through the routings onto the workcenters."""
    known_products = {d.product for d in demand}
    routed_products = {r.product for r in routings}

    unrouted = known_products - routed_products
    if unrouted:
        raise ValueError(
            f"Demand for product(s) with no routing: {', '.join(sorted(unrouted))}"
        )
    unknown_centers = {r.workcenter for r in routings} - set(centers)
    if unknown_centers:
        raise ValueError(
            f"Routing references unknown workcenter(s): "
            f"{', '.join(sorted(unknown_centers))}"
        )

    by_product = {d.product: d for d in demand}
    run: dict[str, float] = defaultdict(float)
    setup: dict[str, float] = defaultdict(float)
    batches: dict[str, float] = defaultdict(float)

    for step in routings:
        line = by_product.get(step.product)
        if line is None:
            continue  # Routed but not demanded this period.
        run[step.workcenter] += line.units * step.run_hours_per_unit
        setup[step.workcenter] += line.batches * step.setup_hours_per_batch
        batches[step.workcenter] += line.batches

    return {
        name: Load(
            workcenter=centers[name],
            run_hours=run[name],
            setup_hours=setup[name],
            batches=batches[name],
        )
        for name in centers
        if batches[name] > 0
    }


def product_lead_time(
    product: str, routings: list[RoutingStep], loads: dict[str, Load]
) -> float:
    """Sum queue + process time along one product's routing."""
    total = 0.0
    for step in routings:
        if step.product != product:
            continue
        load = loads.get(step.workcenter)
        if load is None:
            continue
        total += load.cycle_hours()
    return total


def _bar(value: float, peak: float, width: int = 24) -> str:
    if math.isinf(value):
        return "!" * width
    filled = min(width, int(round(width * value / peak))) if peak else 0
    return "#" * filled + "." * (width - filled)


def _hours(value: float) -> str:
    return "unbounded" if math.isinf(value) else f"{value:,.1f} h"


def format_report(
    loads: dict[str, Load],
    routings: list[RoutingStep],
    demand: list[DemandLine],
) -> str:
    lines: list[str] = []
    add = lines.append

    ordered = sorted(loads.values(), key=lambda load: -load.utilization)
    constraint = ordered[0]

    add("=" * 76)
    add("CAPACITY PLAN".center(76))
    add("=" * 76)
    add("")
    add(f"  Products        {len(demand)}")
    add(f"  Workcenters     {len(loads)}")
    add(f"  Total units     {sum(d.units for d in demand):,.0f}")
    add("")
    add("-" * 76)
    add("LOAD VS CAPACITY")
    add("-" * 76)
    add("")
    add(
        f"  {'Workcenter':<14} {'Req h':>8} {'Avail h':>8} {'Util':>7} "
        f"{'Slack h':>9}  Loading"
    )
    add("  " + "-" * 72)
    for load in ordered:
        util = load.utilization
        flag = ""
        if load.is_overloaded:
            flag = "  OVER"
        elif util >= UTILIZATION_CRITICAL:
            flag = "  CRIT"
        elif util >= UTILIZATION_WARN:
            flag = "  WARN"
        add(
            f"  {load.workcenter.name:<14} {load.required_hours:>8,.0f} "
            f"{load.workcenter.available_hours:>8,.0f} {util * 100:>6.1f}% "
            f"{load.slack_hours:>9,.0f}  [{_bar(util, 1.0)}]{flag}"
        )

    add("")
    add("-" * 76)
    add("CONSTRAINT")
    add("-" * 76)
    add("")
    add(f"  {constraint.workcenter.name} at {constraint.utilization * 100:.1f}% utilization")
    add("")
    add(f"    Required           {constraint.required_hours:,.0f} h")
    add(f"    Available          {constraint.workcenter.available_hours:,.0f} h")
    add(f"    Run                {constraint.run_hours:,.0f} h")
    add(
        f"    Setup              {constraint.setup_hours:,.0f} h "
        f"({constraint.setup_share * 100:.1f}% of required)"
    )
    add(f"    Batches            {constraint.batches:,.0f}")
    add("")
    if constraint.is_overloaded:
        short = -constraint.slack_hours
        need = constraint.machines_needed()
        add(f"    SHORT BY {short:,.0f} h. Options to close the gap:")
        add(
            f"      - Add machines: {constraint.workcenter.machines} -> {need} "
            f"(+{need - constraint.workcenter.machines})"
        )
        extra_shifts = (
            constraint.required_hours / constraint.workcenter.available_hours
            * constraint.workcenter.shifts_per_day
        )
        add(
            f"      - Add shifts:   {constraint.workcenter.shifts_per_day:.0f} -> "
            f"{math.ceil(extra_shifts):.0f}"
        )
        add(f"      - Overtime:     {short:,.0f} h across the period")
        if constraint.setup_share > 0.15:
            saved = constraint.setup_hours * 0.5
            add(
                f"      - SMED:         setup is {constraint.setup_share * 100:.0f}% of load; "
                f"halving it frees {saved:,.0f} h"
            )
        if constraint.batches > 0:
            add("      - Larger batches: fewer setups, at the cost of WIP and flexibility")
    else:
        add(f"    Fits, with {constraint.slack_hours:,.0f} h of slack.")
        if constraint.utilization >= UTILIZATION_WARN:
            add("    But see the lead time section -- capacity is not the binding issue.")

    add("")
    add("-" * 76)
    add("LEAD TIME (Kingman VUT: queue + process, per batch)")
    add("-" * 76)
    add("")
    add(
        f"  {'Workcenter':<14} {'Util':>7} {'Process':>10} {'Queue':>12} "
        f"{'Cycle':>12} {'Q/P':>7}"
    )
    add("  " + "-" * 72)
    for load in ordered:
        queue = load.queue_hours()
        process = load.hours_per_batch
        ratio = "inf" if math.isinf(queue) else f"{queue / process:,.1f}x" if process else "-"
        add(
            f"  {load.workcenter.name:<14} {load.utilization * 100:>6.1f}% "
            f"{process:>9,.1f}h {_hours(queue):>12} {_hours(load.cycle_hours()):>12} "
            f"{ratio:>7}"
        )

    add("")
    add("  Estimated door-to-door lead time by product:")
    add("")
    for line in sorted(demand, key=lambda d: d.product):
        lead = product_lead_time(line.product, routings, loads)
        add(f"    {line.product:<12} {_hours(lead)}")

    add("")
    hot = [load for load in ordered if load.utilization >= UTILIZATION_WARN]
    if hot:
        add(f"  {len(hot)} workcenter(s) at or above {UTILIZATION_WARN * 100:.0f}% utilization.")
        add("  Queue time grows non-linearly here: the u/(1-u) term roughly doubles")
        add("  between 80% and 90%, and doubles again between 90% and 95%. Buying")
        add("  back a few points of utilization buys back disproportionate lead time.")
    else:
        add("  All workcenters below the utilization warning threshold.")

    add("=" * 76)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Model workcenter load, find the constraint, estimate lead time.",
    )
    parser.add_argument("--workcenters", required=True, help="Workcenter CSV")
    parser.add_argument("--routings", required=True, help="Routing CSV")
    parser.add_argument("--demand", required=True, help="Demand CSV")
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Multiply all demand by this factor, e.g. 1.25 for a 25%% increase",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.scale <= 0:
        print("error: --scale must be positive", file=sys.stderr)
        return 2

    try:
        centers = load_workcenters(args.workcenters)
        routings = load_routings(args.routings)
        demand = load_demand(args.demand)
        if args.scale != 1.0:
            demand = [
                DemandLine(d.product, d.units * args.scale, d.batch_size)
                for d in demand
            ]
        loads = compute_loads(centers, routings, demand)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not loads:
        print("error: demand does not load any workcenter", file=sys.stderr)
        return 1

    if args.scale != 1.0:
        print(f"\n[ demand scaled x{args.scale:g} ]")

    print(format_report(loads, routings, demand))

    return 3 if any(load.is_overloaded for load in loads.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
