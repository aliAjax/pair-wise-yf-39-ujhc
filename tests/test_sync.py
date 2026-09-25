import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

OBSERVATION = {
    "event_id": "E-100",
    "species": "deer",
    "location": "North",
    "observed_at": "2026-04-01",
    "lat": 40.0,
    "lon": 116.0,
}


class SyncBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("sync-tester", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_observation(self, **overrides):
        data = dict(OBSERVATION)
        data.update(overrides)
        return self.service.create(self.actor, "observation", data)

    def test_merge_different_fields_into_same_event(self):
        obs = self._create_observation()
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-merge",
            "records": [
                {
                    "offline_id": "OFF-1",
                    "kind": "observation",
                    "base_version": 1,
                    "field_timestamps": {"location": "2026-04-02T08:00:00"},
                    "data": {"event_id": "E-100", "location": "South"},
                },
                {
                    "offline_id": "OFF-2",
                    "kind": "observation",
                    "base_version": 1,
                    "field_timestamps": {"note": "2026-04-02T09:00:00"},
                    "data": {"event_id": "E-100", "note": "lab pending"},
                },
            ],
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["processed"], 2)
        # 两条离线记录通过 event_id 合并到同一服务端实体
        first = result["records"]["OFF-1"]
        second = result["records"]["OFF-2"]
        self.assertEqual(first["id"], obs["id"])
        self.assertEqual(second["id"], obs["id"])
        final = self.service.get(obs["id"])
        self.assertEqual(final["data"]["location"], "South")
        self.assertEqual(final["data"]["note"], "lab pending")
        self.assertEqual(result["results"][0]["status"], "merged")
        self.assertEqual(result["results"][1]["status"], "merged")

    def test_same_field_conflict_keeps_server_value(self):
        obs = self._create_observation()
        self.service.transition(
            self.actor, obs["id"], "submit",
            {"location": "ServerLoc", "observed_at": "2026-04-01"},
        )
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-conflict",
            "records": [{
                "offline_id": "OFF-9",
                "kind": "observation",
                "entity_id": obs["id"],
                "base_version": 1,
                "field_timestamps": {"location": "2026-04-02T08:00:00"},
                "data": {"location": "ClientLoc"},
            }],
        })
        item = result["results"][0]
        self.assertEqual(item["status"], "conflict")
        self.assertEqual(len(item["conflicts"]), 1)
        conflict = item["conflicts"][0]
        self.assertEqual(conflict["field"], "location")
        self.assertEqual(conflict["server_value"], "ServerLoc")
        self.assertEqual(conflict["client_value"], "ClientLoc")
        self.assertEqual(conflict["resolution"], "server_kept")
        # 服务端内容保持不变
        self.assertEqual(self.service.get(obs["id"])["data"]["location"], "ServerLoc")

    def test_batch_replay_returns_first_result(self):
        obs = self._create_observation()
        batch = {
            "batch_id": "B-replay",
            "records": [{
                "offline_id": "OFF-1",
                "kind": "observation",
                "entity_id": obs["id"],
                "base_version": 1,
                "data": {"location": "South"},
            }],
        }
        first = self.service.sync_batch(self.actor, batch)
        version_after_first = self.service.get(obs["id"])["version"]
        replay = self.service.sync_batch(self.actor, batch)
        self.assertEqual(first, replay)
        # 重传不产生新的写入
        self.assertEqual(self.service.get(obs["id"])["version"], version_after_first)

    def test_record_failure_does_not_block_others(self):
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-partial",
            "records": [
                {
                    "offline_id": "OK-1",
                    "kind": "observation",
                    "base_version": 0,
                    "data": dict(OBSERVATION, event_id="E-200"),
                },
                {
                    "offline_id": "BAD-1",
                    "kind": "observation",
                    "base_version": 0,
                    "data": {"event_id": "E-201"},
                },
                {
                    "offline_id": "OK-2",
                    "kind": "observation",
                    "base_version": 0,
                    "data": dict(OBSERVATION, event_id="E-202"),
                },
            ],
        })
        self.assertEqual(result["status"], "completed_with_errors")
        statuses = {item["offline_id"]: item["status"] for item in result["results"]}
        self.assertEqual(statuses["OK-1"], "created")
        self.assertEqual(statuses["BAD-1"], "error")
        self.assertEqual(statuses["OK-2"], "created")
        self.assertIn("OK-1", result["records"])
        self.assertIn("OK-2", result["records"])
        self.assertNotIn("BAD-1", result["records"])

    def test_field_timestamps_decide_batch_internal_conflict(self):
        obs = self._create_observation()
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-ts",
            "records": [
                {
                    "offline_id": "R1",
                    "kind": "observation",
                    "entity_id": obs["id"],
                    "base_version": 1,
                    "field_timestamps": {"location": "2026-04-01T08:00:00"},
                    "data": {"location": "Earlier"},
                },
                {
                    "offline_id": "R2",
                    "kind": "observation",
                    "entity_id": obs["id"],
                    "base_version": 1,
                    "field_timestamps": {"location": "2026-04-03T08:00:00"},
                    "data": {"location": "Later"},
                },
            ],
        })
        # 字段时间更新的记录覆盖同字段的旧值
        self.assertEqual(self.service.get(obs["id"])["data"]["location"], "Later")
        self.assertEqual(result["results"][0]["status"], "merged")
        self.assertEqual(result["results"][1]["status"], "merged")

    def test_older_field_timestamp_loses_in_batch(self):
        obs = self._create_observation()
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-ts-older",
            "records": [
                {
                    "offline_id": "R1",
                    "kind": "observation",
                    "entity_id": obs["id"],
                    "base_version": 1,
                    "field_timestamps": {"location": "2026-04-03T08:00:00"},
                    "data": {"location": "Later"},
                },
                {
                    "offline_id": "R2",
                    "kind": "observation",
                    "entity_id": obs["id"],
                    "base_version": 1,
                    "field_timestamps": {"location": "2026-04-01T08:00:00"},
                    "data": {"location": "Earlier"},
                },
            ],
        })
        self.assertEqual(self.service.get(obs["id"])["data"]["location"], "Later")
        item = result["results"][1]
        self.assertEqual(item["status"], "conflict")
        self.assertEqual(item["conflicts"][0]["resolution"], "batch_kept")

    def test_action_record_runs_state_machine(self):
        obs = self._create_observation()
        self.service.transition(
            self.actor, obs["id"], "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        sample = self.service.create(
            self.actor, "sample",
            {"observation_id": obs["id"], "sample_code": "S-1"},
        )
        self.service.transition(self.actor, sample["id"], "send_lab", {"lab_id": "LAB-1"})
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-lab",
            "records": [{
                "offline_id": "LAB-1",
                "kind": "sample",
                "entity_id": sample["id"],
                "base_version": 2,
                "action": "lab_result",
                "data": {"result": "positive", "result_at": "2026-04-05"},
            }],
        })
        self.assertEqual(result["results"][0]["status"], "applied")
        final = result["records"]["LAB-1"]
        self.assertEqual(final["status"], "resulted")
        self.assertEqual(final["data"]["result"], "positive")

    def test_action_with_stale_base_version_conflicts(self):
        obs = self._create_observation()
        self.service.transition(
            self.actor, obs["id"], "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        sample = self.service.create(
            self.actor, "sample",
            {"observation_id": obs["id"], "sample_code": "S-2"},
        )
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-stale-action",
            "records": [{
                "offline_id": "LAB-2",
                "kind": "sample",
                "entity_id": sample["id"],
                "base_version": 0,
                "action": "send_lab",
                "data": {"lab_id": "LAB-9"},
            }],
        })
        item = result["results"][0]
        self.assertEqual(item["status"], "conflict")
        self.assertEqual(item["conflicts"][0]["resolution"], "server_kept")
        # 动作未执行，实体保持原状态
        self.assertEqual(self.service.get(sample["id"])["status"], "collected")

    def test_batch_audit_entry_and_status_endpoint(self):
        obs = self._create_observation()
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-audit",
            "records": [{
                "offline_id": "OFF-1",
                "kind": "observation",
                "entity_id": obs["id"],
                "base_version": 1,
                "data": {"location": "South"},
            }],
        })
        audit_ref = result["audit"]
        self.assertEqual(audit_ref["entity_id"], "sync-batch:B-audit")
        entries = self.service.audit_log(entity_id="sync-batch:B-audit")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "sync_batch")
        self.assertEqual(entries[0]["detail"]["results"][0]["offline_id"], "OFF-1")
        # 实体时间线上也能看到合并记录
        entity_entries = self.service.audit_log(entity_id=obs["id"])
        self.assertEqual(entity_entries[-1]["action"], "sync_merge")
        # 批次查询入口返回首次结果与审计
        status = self.service.sync_batch_status(self.actor, "B-audit")
        self.assertEqual(status["batch"], result)
        self.assertEqual(len(status["audit"]), 1)

    def test_unchanged_record_does_not_bump_version(self):
        obs = self._create_observation()
        result = self.service.sync_batch(self.actor, {
            "batch_id": "B-unchanged",
            "records": [{
                "offline_id": "OFF-1",
                "kind": "observation",
                "entity_id": obs["id"],
                "base_version": 1,
                "data": {"location": "North"},
            }],
        })
        self.assertEqual(result["results"][0]["status"], "unchanged")
        self.assertEqual(self.service.get(obs["id"])["version"], 1)

    def test_single_record_registration_still_works(self):
        obs = self._create_observation(event_id="E-300")
        updated = self.service.transition(
            self.actor, obs["id"], "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        self.assertEqual(updated["status"], "submitted")
        self.assertEqual(updated["version"], 2)


if __name__ == "__main__":
    unittest.main()
