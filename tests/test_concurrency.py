import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ConcurrentScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(db)
        # 每个线程独立仓储连接，共享同一个 SQLite 文件。
        self.service_a = DomainService(SQLiteRepository(db), RuleEngine())
        self.service_b = DomainService(SQLiteRepository(db), RuleEngine())
        self.service = self.service_a
        self.admin = Actor("admin", "admin")
        self.met_a = Actor("metrologist-a", "metrology")
        self.met_b = Actor("metrologist-b", "metrology")

        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.standard = self.service.create(
            self.admin,
            "standard",
            {
                "name": "Gauge",
                "serial": "G-1",
                "valid_from": "2026-01-01",
                "valid_until": "2027-01-01",
                "capacity": 1,
            },
        )
        window = ("2026-05-01T09:00", "2026-05-01T11:00")
        self.order_a = self.service.create(
            self.met_a,
            "work_order",
            {"instrument_id": instrument["id"], "standard_id": self.standard["id"],
             "start_at": window[0], "end_at": window[1]},
        )
        self.order_b = self.service.create(
            self.met_b,
            "work_order",
            {"instrument_id": instrument["id"], "standard_id": self.standard["id"],
             "start_at": window[0], "end_at": window[1]},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_simultaneous_overlapping_submissions_one_winner(self):
        barrier = threading.Barrier(2)
        results = {}

        def submit(service, actor, order_id, key):
            barrier.wait()
            try:
                service.transition(actor, order_id, "schedule", {})
                results[key] = "scheduled"
            except ConflictError:
                results[key] = "conflict"
            except Exception as exc:  # pragma: no cover - 失败时暴露异常类型
                results[key] = "error:" + type(exc).__name__

        t1 = threading.Thread(target=submit,
                              args=(self.service_a, self.met_a, self.order_a["id"], "a"))
        t2 = threading.Thread(target=submit,
                              args=(self.service_b, self.met_b, self.order_b["id"], "b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sorted(results.values()), ["conflict", "scheduled"])
        # 只有一条 held，胜出去重
        held = self.service.occupancy(standard_id=self.standard["id"], state="held")
        self.assertEqual(len(held), 1)
        winner = held[0]["work_order_id"]
        self.assertEqual(self.service.get(winner)["status"], "scheduled")
        loser = self.order_b["id"] if winner == self.order_a["id"] else self.order_a["id"]
        self.assertEqual(self.service.get(loser)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
