import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, windows_overlap, window_covered
from src.service import DomainService


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.met_a = Actor("metrologist-a", "metrology")
        self.met_b = Actor("metrologist-b", "metrology")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self, name="Analyzer"):
        return self.service.create(
            self.admin, "instrument", {"name": name, "serial": name + "-1"}
        )["id"]

    def _standard(self, capacity=1, valid_from="2026-01-01", valid_until="2027-01-01"):
        return self.service.create(
            self.admin,
            "standard",
            {
                "name": "Gauge",
                "serial": "G-1",
                "valid_from": valid_from,
                "valid_until": valid_until,
                "capacity": capacity,
            },
        )

    def _order(self, instrument_id, standard_id, start, end):
        return self.service.create(
            self.met_a,
            "work_order",
            {
                "instrument_id": instrument_id,
                "standard_id": standard_id,
                "start_at": start,
                "end_at": end,
            },
        )

    def test_interval_rules(self):
        # 半开区间：相接不重叠
        self.assertFalse(windows_overlap("2026-05-01T09:00:00", "2026-05-01T10:00:00",
                                         "2026-05-01T10:00:00", "2026-05-01T11:00:00"))
        self.assertTrue(windows_overlap("2026-05-01T09:00:00", "2026-05-01T10:30:00",
                                        "2026-05-01T10:00:00", "2026-05-01T11:00:00"))
        self.assertTrue(window_covered("2026-01-02T00:00:00", "2026-12-31T23:59:59",
                                       "2026-01-01T00:00:00", "2026-12-31T23:59:59"))
        self.assertFalse(window_covered("2026-01-02T00:00:00", "2027-01-02T00:00:00",
                                        "2026-01-01T00:00:00", "2027-01-01T00:00:00"))

    def test_first_writer_keeps_slot_second_gets_conflict(self):
        instrument = self._instrument()
        standard = self._standard()
        first = self._order(instrument, standard["id"], "2026-05-01T09:00", "2026-05-01T11:00")
        second = self._order(instrument, standard["id"], "2026-05-01T10:00", "2026-05-01T12:00")

        scheduled = self.service.transition(self.met_a, first["id"], "schedule", {})
        self.assertEqual(scheduled["status"], "scheduled")
        with self.assertRaises(ConflictError):
            self.service.transition(self.met_b, second["id"], "schedule", {})

        # 后到者没有占位：占用账只有一条 held
        held = self.service.occupancy(standard_id=standard["id"], state="held")
        self.assertEqual([item["work_order_id"] for item in held], [first["id"]])
        second = self.service.get(second["id"])
        self.assertEqual(second["status"], "pending")

        # 不重叠的背靠背工单可以排
        third = self._order(instrument, standard["id"], "2026-05-01T11:00", "2026-05-01T12:00")
        self.assertEqual(
            self.service.transition(self.met_b, third["id"], "schedule", {})["status"],
            "scheduled",
        )

    def test_validity_must_cover_whole_window(self):
        instrument = self._instrument()
        standard = self._standard(valid_from="2026-06-01", valid_until="2026-06-30T23:59:59")
        # 创建时有效期盖不住整段即拒
        with self.assertRaises(Exception):
            self._order(instrument, standard["id"], "2026-06-30T20:00", "2026-07-01T02:00")

    def test_capacity_full_queues_and_fifo_promotion_on_release(self):
        instrument = self._instrument()
        standard = self._standard(capacity=1)
        first = self._order(instrument, standard["id"], "2026-05-01T09:00", "2026-05-01T11:00")
        self.service.transition(self.met_a, first["id"], "schedule", {})

        waiting = self._order(instrument, standard["id"], "2026-05-01T10:00", "2026-05-01T10:30")
        # 明确选择排队
        queued = self.service.transition(self.met_b, waiting["id"], "enqueue", {})
        self.assertEqual(queued["status"], "queued")

        # 完成先到工单，排队工单自动顶上空位
        self.service.transition(
            self.met_a, first["id"], "complete",
            {"result": "passed", "completed_at": "2026-05-01T11:00"},
        )
        promoted = self.service.get(waiting["id"])
        self.assertEqual(promoted["status"], "scheduled")
        self.assertEqual(promoted["data"]["scheduled_by"], "scheduler")
        held = self.service.occupancy(standard_id=standard["id"], state="held")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["work_order_id"], waiting["id"])
        completed_rows = self.service.occupancy(state="completed")
        self.assertEqual(len(completed_rows), 1)

    def test_capacity_two_allows_concurrent_orders(self):
        instrument = self._instrument()
        standard = self._standard(capacity=2)
        first = self._order(instrument, standard["id"], "2026-05-01T09:00", "2026-05-01T11:00")
        second = self._order(instrument, standard["id"], "2026-05-01T10:00", "2026-05-01T12:00")
        third = self._order(instrument, standard["id"], "2026-05-01T09:30", "2026-05-01T10:30")
        self.service.transition(self.met_a, first["id"], "schedule", {})
        self.service.transition(self.met_b, second["id"], "schedule", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.met_a, third["id"], "schedule", {})

    def test_standard_change_voids_open_orders_keeps_completed(self):
        instrument = self._instrument()
        standard = self._standard()
        done = self._order(instrument, standard["id"], "2026-05-01T08:00", "2026-05-01T09:00")
        self.service.transition(self.met_a, done["id"], "schedule", {})
        self.service.transition(
            self.met_a, done["id"], "complete",
            {"result": "passed", "completed_at": "2026-05-01T09:00"},
        )
        open_order = self._order(instrument, standard["id"], "2026-05-02T08:00", "2026-05-02T09:00")
        self.service.transition(self.met_a, open_order["id"], "schedule", {})

        waiting = self._order(instrument, standard["id"], "2026-05-03T08:00", "2026-05-03T09:00")
        self.service.transition(self.met_a, waiting["id"], "enqueue", {})

        self.service.transition(self.admin, standard["id"], "suspend", {"reason": "repair"})

        self.assertEqual(self.service.get(open_order["id"])["status"], "pending")
        self.assertEqual(self.service.get(waiting["id"])["status"], "pending")
        # 已完成校准保留
        self.assertEqual(self.service.get(done["id"])["status"], "completed")
        # held 占用全部释放，完成记录仍在
        self.assertEqual(self.service.occupancy(state="held"), [])
        self.assertEqual(len(self.service.occupancy(state="completed")), 1)
        self.assertEqual(len(self.service.occupancy(state="released")), 1)

    def test_manual_void_releases_slot_and_promotes_queue(self):
        instrument = self._instrument()
        standard = self._standard()
        first = self._order(instrument, standard["id"], "2026-05-01T09:00", "2026-05-01T11:00")
        waiting = self._order(instrument, standard["id"], "2026-05-01T10:00", "2026-05-01T10:30")
        self.service.transition(self.met_a, first["id"], "schedule", {})
        self.service.transition(self.met_b, waiting["id"], "enqueue", {})

        voided = self.service.transition(
            self.met_a, first["id"], "void", {"reason": "instrument unavailable"}
        )
        self.assertEqual(voided["status"], "pending")
        self.assertEqual(self.service.get(waiting["id"])["status"], "scheduled")
        self.assertEqual(len(self.service.occupancy(state="held")), 1)

    def test_restart_reconciles_orphan_occupancy_without_double_hold(self):
        instrument = self._instrument()
        standard = self._standard()
        order = self._order(instrument, standard["id"], "2026-05-01T09:00", "2026-05-01T11:00")
        self.service.transition(self.met_a, order["id"], "schedule", {})

        # 模拟“排程写入一半失败”：占用账留下 held，工单却被打回 pending
        scheduled = self.service.get(order["id"])
        self.repo.update_entity(order["id"], scheduled["version"], "pending",
                                dict(scheduled["data"], note="simulated crash"))
        held = self.service.occupancy(state="held")
        self.assertEqual(len(held), 1)

        # 重启服务：按工单核对，孤儿占用释放，再排不会重复占位
        restarted = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.assertEqual(restarted.occupancy(state="held"), [])
        order2 = restarted.get(order["id"])
        fresh = restarted.transition(
            Actor("metrologist-a", "metrology"), order2["id"], "schedule", {}
        )
        self.assertEqual(fresh["status"], "scheduled")
        held = restarted.occupancy(state="held")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["work_order_id"], order["id"])


if __name__ == "__main__":
    unittest.main()
