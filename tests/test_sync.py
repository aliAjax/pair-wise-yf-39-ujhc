import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SyncBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field = Actor("ranger-7", "field")
        self.lab = Actor("lab-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _observation(self, event_id="E-1", location="North", observed_at="2026-04-01"):
        return self.service.create(
            self.admin,
            "observation",
            {
                "event_id": event_id,
                "species": "deer",
                "location": location,
                "observed_at": observed_at,
                "lat": 40.0,
                "lon": 116.0,
            },
        )

    def test_create_then_merge_by_natural_key(self):
        first = self.service.sync_batch(self.field, "B-1", [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "baseline_version": 1,
                "data": {
                    "event_id": "E-1",
                    "species": "deer",
                    "location": "North",
                    "observed_at": "2026-04-01",
                    "lat": 40.0,
                    "lon": 116.0,
                },
                "field_timestamps": {"location": "2026-04-01T08:00:00+00:00"},
            }
        ])
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["results"][0]["status"], "created")
        entity_id = first["results"][0]["entity_id"]

        # 同一事件拆出的第二条离线记录：地点修正。按 event_id 匹配后合并而不是报重复。
        second = self.service.sync_batch(self.field, "B-2", [
            {
                "offline_id": "off-2",
                "kind": "observation",
                "baseline_version": 1,
                "data": {
                    "event_id": "E-1",
                    "species": "deer",
                    "location": "North Ridge",
                    "observed_at": "2026-04-01",
                },
                "field_timestamps": {"location": "2026-04-02T09:00:00+00:00"},
            }
        ])
        result = second["results"][0]
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["entity_id"], entity_id)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["record"]["data"]["location"], "North Ridge")
        self.assertEqual(result["record"]["version"], 2)

    def test_same_field_conflict_keeps_server_and_lists_client(self):
        entity = self._observation()
        self.service.transition(
            self.admin,
            entity["id"],
            "submit",
            {"location": "ServerFixed", "observed_at": "2026-04-01"},
        )
        response = self.service.sync_batch(self.field, "B-3", [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "entity_id": entity["id"],
                "baseline_version": 1,
                "data": {"location": "ClientFix", "remark": "saw tracks"},
                "field_timestamps": {
                    "location": "2026-04-02T09:00:00+00:00",
                    "remark": "2026-04-02T09:05:00+00:00",
                },
            }
        ])
        result = response["results"][0]
        self.assertEqual(result["status"], "merged")
        # 双方都改 location：留服务端内容，客户端值进冲突清单
        self.assertEqual(result["record"]["data"]["location"], "ServerFixed")
        self.assertEqual(
            result["conflicts"],
            [
                {
                    "field": "location",
                    "server_value": "ServerFixed",
                    "client_value": "ClientFix",
                    "client_modified_at": "2026-04-02T09:00:00+00:00",
                    "resolution": "server_kept",
                }
            ],
        )
        # 只有客户端改的字段正常合并
        self.assertEqual(result["record"]["data"]["remark"], "saw tracks")
        self.assertEqual(result["record"]["version"], 3)

    def test_lab_result_merges_into_sample(self):
        observation = self._observation()
        self.service.transition(
            self.admin,
            observation["id"],
            "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        sample = self.service.create(
            self.admin, "sample", {"observation_id": observation["id"], "sample_code": "W-1"}
        )
        self.service.transition(self.admin, sample["id"], "send_lab", {"lab_id": "LAB-1"})
        response = self.service.sync_batch(self.lab, "B-4", [
            {
                "offline_id": "off-lab",
                "kind": "sample",
                "entity_id": sample["id"],
                "baseline_version": 1,
                "data": {"result": "positive", "result_at": "2026-04-03"},
                "field_timestamps": {
                    "result": "2026-04-03T10:00:00+00:00",
                    "result_at": "2026-04-03T10:00:00+00:00",
                },
            }
        ])
        result = response["results"][0]
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["record"]["data"]["result"], "positive")
        self.assertEqual(result["record"]["version"], 3)

    def test_failed_record_does_not_block_others(self):
        response = self.service.sync_batch(self.field, "B-5", [
            {
                "offline_id": "off-bad",
                "kind": "observation",
                "baseline_version": 1,
                "data": {"event_id": "E-bad", "location": "N", "observed_at": "2026-04-01"},
                "field_timestamps": {"location": "2026-04-01T08:00:00+00:00"},
            },
            {
                "offline_id": "off-good",
                "kind": "observation",
                "baseline_version": 1,
                "data": {
                    "event_id": "E-good",
                    "species": "boar",
                    "location": "South",
                    "observed_at": "2026-04-02",
                    "lat": 39.0,
                    "lon": 115.0,
                },
                "field_timestamps": {"location": "2026-04-02T08:00:00+00:00"},
            },
        ])
        self.assertEqual(response["status"], "partial")
        bad, good = response["results"]
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(bad["error_type"], "ValidationError")
        self.assertIsNone(bad["record"])
        self.assertEqual(good["status"], "created")
        self.assertEqual(self.service.get(good["entity_id"])["data"]["species"], "boar")

    def test_replay_returns_first_result(self):
        records = [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "baseline_version": 1,
                "data": {
                    "event_id": "E-1",
                    "species": "deer",
                    "location": "North",
                    "observed_at": "2026-04-01",
                    "lat": 40.0,
                    "lon": 116.0,
                },
                "field_timestamps": {"location": "2026-04-01T08:00:00+00:00"},
            }
        ]
        first = self.service.sync_batch(self.field, "B-6", records)
        second = self.service.sync_batch(self.field, "B-6", records)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["results"], first["results"])
        entity_id = first["results"][0]["entity_id"]
        self.assertEqual(self.service.get(entity_id)["version"], 1)
        self.assertEqual(len(self.service.audit_log("batch:B-6")), 1)

    def test_baseline_ahead_of_server_fails_record(self):
        entity = self._observation()
        response = self.service.sync_batch(self.field, "B-7", [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "entity_id": entity["id"],
                "baseline_version": 5,
                "data": {"location": "X"},
                "field_timestamps": {"location": "2026-04-02T09:00:00+00:00"},
            }
        ])
        result = response["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "ValidationError")
        self.assertEqual(self.service.get(entity["id"])["version"], 1)

    def test_fields_without_timestamp_are_not_client_changes(self):
        entity = self._observation()
        self.service.transition(
            self.admin,
            entity["id"],
            "submit",
            {"location": "ServerFixed", "observed_at": "2026-04-01"},
        )
        # data 里带着基线时的旧 location，但字段时间只标记了 note：
        # location 不算客户端修改，不产生误报冲突。
        response = self.service.sync_batch(self.field, "B-8", [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "entity_id": entity["id"],
                "baseline_version": 1,
                "data": {"location": "North", "note": "rechecked"},
                "field_timestamps": {"note": "2026-04-02T09:00:00+00:00"},
            }
        ])
        result = response["results"][0]
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["record"]["data"]["location"], "ServerFixed")
        self.assertEqual(result["record"]["data"]["note"], "rechecked")

    def test_batch_audit_entry_summarizes_results(self):
        response = self.service.sync_batch(self.field, "B-9", [
            {
                "offline_id": "off-1",
                "kind": "observation",
                "baseline_version": 1,
                "data": {
                    "event_id": "E-1",
                    "species": "deer",
                    "location": "North",
                    "observed_at": "2026-04-01",
                    "lat": 40.0,
                    "lon": 116.0,
                },
                "field_timestamps": {"location": "2026-04-01T08:00:00+00:00"},
            }
        ])
        self.assertEqual(response["audit"]["entity_id"], "batch:B-9")
        entries = self.service.audit_log("batch:B-9")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["action"], "sync_batch")
        self.assertEqual(entry["to_status"], "ok")
        self.assertEqual(entry["detail"]["total"], 1)
        self.assertEqual(
            entry["detail"]["results"][0]["offline_id"], "off-1"
        )

    def test_invalid_sample_result_fails_without_writing(self):
        observation = self._observation()
        self.service.transition(
            self.admin,
            observation["id"],
            "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        sample = self.service.create(
            self.admin, "sample", {"observation_id": observation["id"], "sample_code": "W-1"}
        )
        self.service.transition(self.admin, sample["id"], "send_lab", {"lab_id": "LAB-1"})
        response = self.service.sync_batch(self.lab, "B-10", [
            {
                "offline_id": "off-lab",
                "kind": "sample",
                "entity_id": sample["id"],
                "baseline_version": 1,
                "data": {"result": "maybe", "result_at": "2026-04-03"},
                "field_timestamps": {"result": "2026-04-03T10:00:00+00:00"},
            }
        ])
        result = response["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "ValidationError")
        self.assertNotIn("result", self.service.get(sample["id"])["data"])


if __name__ == "__main__":
    unittest.main()
