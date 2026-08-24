"""Unit tests for the capacity planning model. Run: python -m unittest discover tests"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from capacity import (  # noqa: E402
    DemandLine,
    Load,
    RoutingStep,
    WorkCenter,
    compute_loads,
    load_workcenters,
    product_lead_time,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def wc(name="WC", machines=1, hours=8, shifts=1, days=20, avail=1.0, eff=1.0,
       ca=1.0, cs=1.0):
    return WorkCenter(
        name=name,
        machines=machines,
        hours_per_shift=hours,
        shifts_per_day=shifts,
        days=days,
        availability=avail,
        efficiency=eff,
        cv_arrival=ca,
        cv_service=cs,
    )


def load_at(utilization, machines=1, ca=1.0, cs=1.0, batches=10):
    center = wc(machines=machines, ca=ca, cs=cs)
    return Load(
        workcenter=center,
        run_hours=center.available_hours * utilization,
        setup_hours=0.0,
        batches=batches,
    )


class TestCapacityMath(unittest.TestCase):
    def test_gross_hours(self):
        self.assertEqual(wc(machines=2, hours=8, shifts=2, days=20).gross_hours, 640)

    def test_losses_reduce_available_hours(self):
        center = wc(machines=1, hours=10, shifts=1, days=10, avail=0.9, eff=0.8)
        self.assertAlmostEqual(center.available_hours, 100 * 0.9 * 0.8)

    def test_utilization_is_required_over_available(self):
        self.assertAlmostEqual(load_at(0.75).utilization, 0.75)

    def test_overload_detected(self):
        self.assertTrue(load_at(1.2).is_overloaded)
        self.assertFalse(load_at(0.99).is_overloaded)

    def test_slack_goes_negative_when_over(self):
        self.assertLess(load_at(1.1).slack_hours, 0)

    def test_setup_share(self):
        center = wc()
        load = Load(workcenter=center, run_hours=75, setup_hours=25, batches=5)
        self.assertAlmostEqual(load.setup_share, 0.25)
        self.assertAlmostEqual(load.required_hours, 100)

    def test_machines_needed_rounds_up(self):
        # 1 machine at 250% utilization needs 3.
        self.assertEqual(load_at(2.5, machines=1).machines_needed(), 3)

    def test_machines_needed_is_current_count_when_fitting(self):
        self.assertEqual(load_at(0.8, machines=1).machines_needed(), 1)


class TestKingmanQueue(unittest.TestCase):
    def test_queue_grows_with_utilization(self):
        low = load_at(0.50).queue_hours()
        mid = load_at(0.80).queue_hours()
        high = load_at(0.95).queue_hours()
        self.assertLess(low, mid)
        self.assertLess(mid, high)

    def test_queue_is_unbounded_at_full_utilization(self):
        self.assertTrue(math.isinf(load_at(1.0).queue_hours()))
        self.assertTrue(math.isinf(load_at(1.5).queue_hours()))

    def test_queue_grows_faster_than_linearly(self):
        # Doubling 45% -> 90% utilization must more than double the queue.
        low = load_at(0.45).queue_hours()
        high = load_at(0.90).queue_hours()
        self.assertGreater(high, 2 * low)

    def test_zero_variability_means_zero_queue(self):
        self.assertAlmostEqual(load_at(0.9, ca=0.0, cs=0.0).queue_hours(), 0.0)

    def test_more_variability_means_more_queue(self):
        calm = load_at(0.8, ca=0.5, cs=0.5).queue_hours()
        choppy = load_at(0.8, ca=1.5, cs=1.5).queue_hours()
        self.assertGreater(choppy, calm)

    def test_parallel_machines_reduce_queue_at_equal_utilization(self):
        single = load_at(0.85, machines=1).queue_hours()
        multi = load_at(0.85, machines=4).queue_hours()
        self.assertLess(multi, single)

    def test_cycle_time_includes_processing(self):
        load = load_at(0.8)
        self.assertAlmostEqual(
            load.cycle_hours(), load.queue_hours() + load.hours_per_batch
        )


class TestBatching(unittest.TestCase):
    def test_batches_round_up(self):
        self.assertEqual(DemandLine("P", units=10, batch_size=4).batches, 3)

    def test_exact_division_does_not_over_round(self):
        self.assertEqual(DemandLine("P", units=12, batch_size=4).batches, 3)


class TestLoadRollup(unittest.TestCase):
    def setUp(self):
        self.centers = {"A": wc("A"), "B": wc("B")}
        self.routings = [
            RoutingStep("P1", "A", run_hours_per_unit=2.0, setup_hours_per_batch=1.0),
            RoutingStep("P1", "B", run_hours_per_unit=3.0, setup_hours_per_batch=0.5),
        ]
        self.demand = [DemandLine("P1", units=10, batch_size=5)]

    def test_run_and_setup_hours_roll_up(self):
        loads = compute_loads(self.centers, self.routings, self.demand)
        self.assertAlmostEqual(loads["A"].run_hours, 20.0)
        self.assertAlmostEqual(loads["A"].setup_hours, 2.0)   # 2 batches x 1.0
        self.assertAlmostEqual(loads["B"].required_hours, 31.0)

    def test_unrouted_demand_is_rejected(self):
        with self.assertRaises(ValueError):
            compute_loads(
                self.centers, self.routings, [DemandLine("GHOST", 5, 1)]
            )

    def test_unknown_workcenter_in_routing_is_rejected(self):
        bad = self.routings + [RoutingStep("P1", "NOWHERE", 1.0, 0.0)]
        with self.assertRaises(ValueError):
            compute_loads(self.centers, bad, self.demand)

    def test_lead_time_sums_along_the_routing(self):
        loads = compute_loads(self.centers, self.routings, self.demand)
        expected = loads["A"].cycle_hours() + loads["B"].cycle_hours()
        self.assertAlmostEqual(
            product_lead_time("P1", self.routings, loads), expected
        )


class TestFileLoading(unittest.TestCase):
    def test_sample_workcenters_load(self):
        centers = load_workcenters(os.path.join(DATA, "workcenters.csv"))
        self.assertEqual(len(centers), 6)
        self.assertIn("PAINT", centers)

    def _write(self, text):
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_duplicate_workcenter_rejected(self):
        path = self._write(
            "workcenter,machines,hours_per_shift,days\nA,1,8,20\nA,1,8,20\n"
        )
        with self.assertRaises(ValueError):
            load_workcenters(path)

    def test_out_of_range_availability_rejected(self):
        path = self._write(
            "workcenter,machines,hours_per_shift,days,availability\nA,1,8,20,1.5\n"
        )
        with self.assertRaises(ValueError):
            load_workcenters(path)

    def test_defaults_applied_for_optional_columns(self):
        path = self._write("workcenter,machines,hours_per_shift,days\nA,2,8,10\n")
        centers = load_workcenters(path)
        self.assertEqual(centers["A"].availability, 1.0)
        self.assertEqual(centers["A"].efficiency, 1.0)
        self.assertEqual(centers["A"].shifts_per_day, 1.0)
        self.assertAlmostEqual(centers["A"].available_hours, 160.0)


if __name__ == "__main__":
    unittest.main()
