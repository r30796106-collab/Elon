import tempfile
import threading
import unittest
from pathlib import Path

from engine.task_engine import TaskEngine
from engine.worker_coordinator import WorkerCoordinator, WorkerLeaseError


class WorkerCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.coordinator = WorkerCoordinator(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_is_atomic_across_workers(self):
        task_id = self.engine.create_task("One pending task")
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def claim(worker):
            barrier.wait(timeout=3)
            result = self.coordinator.claim_next(worker)
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["task_id"], task_id)
        self.assertEqual(claimed[0]["plan_execution"], "NOT_STARTED")
        self.assertEqual(self.engine.get_task(task_id)["status"], "pending")

    def test_lease_heartbeat_and_release_require_owner_token(self):
        task_id = self.engine.create_task("Lease lifecycle")
        claim = self.coordinator.claim_next("worker-a", 60)
        with self.assertRaises(WorkerLeaseError):
            self.coordinator.heartbeat(task_id, "worker-b", claim["lease_token"])
        renewed = self.coordinator.heartbeat(
            task_id, "worker-a", claim["lease_token"], 90
        )
        self.assertGreater(renewed["expires_at"], claim["expires_at"])
        with self.assertRaises(WorkerLeaseError):
            self.coordinator.release(task_id, "worker-b", claim["lease_token"])
        self.coordinator.release(task_id, "worker-a", claim["lease_token"])
        self.assertEqual(self.coordinator.list_leases(), [])
        self.assertEqual(self.engine.get_task(task_id)["status"], "pending")

    def test_expired_lease_requires_explicit_recovery(self):
        task_id = self.engine.create_task("Stale worker")
        claim = self.coordinator.claim_next("worker-a", 30)
        with self.engine._connect() as db:
            db.execute(
                "UPDATE worker_leases SET expires_at = ? WHERE task_id = ?",
                ("2000-01-01T00:00:00+00:00", task_id),
            )
        self.assertEqual(self.coordinator.claim_next("worker-b"), None)
        with self.assertRaises(WorkerLeaseError):
            self.coordinator.recover_expired(task_id)
        recovered = self.coordinator.recover_expired(task_id, confirm_stopped=True)
        self.assertTrue(recovered["recovered"])
        next_claim = self.coordinator.claim_next("worker-b")
        self.assertEqual(next_claim["task_id"], task_id)
        self.assertNotEqual(next_claim["lease_token"], claim["lease_token"])

    def test_expired_worker_cannot_heartbeat(self):
        task_id = self.engine.create_task("Expired heartbeat")
        claim = self.coordinator.claim_next("worker-a", 30)
        with self.engine._connect() as db:
            db.execute(
                "UPDATE worker_leases SET expires_at = ? WHERE task_id = ?",
                ("2000-01-01T00:00:00+00:00", task_id),
            )
        with self.assertRaises(WorkerLeaseError):
            self.coordinator.heartbeat(task_id, "worker-a", claim["lease_token"])

    def test_invalid_worker_and_lease_duration_rejected(self):
        self.engine.create_task("Validation")
        with self.assertRaises(ValueError):
            self.coordinator.claim_next("")
        with self.assertRaises(ValueError):
            self.coordinator.claim_next("worker", 5)
        with self.assertRaises(ValueError):
            self.coordinator.claim_next("worker", True)


if __name__ == "__main__":
    unittest.main()
