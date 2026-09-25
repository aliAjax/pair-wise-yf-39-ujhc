from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine

_MISSING = object()

# 自然键：离线记录不知道服务端 id 时，用业务键找到已有实体做合并
NATURAL_KEYS = {"observation": "event_id", "sample": "sample_code"}


class DomainService:
    SYNC_BATCH_AUDIT_PREFIX = "sync-batch:"

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None, audit_extra=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        detail = {"patch": patch}
        if audit_extra:
            detail.update(audit_extra)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            detail,
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def sync_batch(self, actor, payload):
        if not isinstance(payload, dict):
            raise ValidationError("batch payload must be a JSON object")
        batch_key = payload.get("batch_id") or payload.get("batch_key")
        if not isinstance(batch_key, str) or not batch_key.strip():
            raise ValidationError("batch_id is required")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ValidationError("records must be a non-empty list")
        existing = self.repository.get_sync_batch(actor.user_id, batch_key)
        if existing is not None:
            # 批次重传：直接沿用首次处理结果，不产生新的写入
            return existing
        results = []
        final_records = {}
        for record in records:
            try:
                outcome = self._apply_sync_record(actor, batch_key, record)
            except Exception as exc:
                # 单条失败不影响批次内其他记录
                outcome = {
                    "offline_id": record.get("offline_id") if isinstance(record, dict) else None,
                    "status": "error",
                    "entity_id": None,
                    "version": None,
                    "conflicts": [],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            results.append(outcome)
            if outcome.get("entity") is not None and outcome.get("offline_id"):
                final_records[outcome["offline_id"]] = outcome["entity"]
        status = "completed" if all(item["status"] != "error" for item in results) else "completed_with_errors"
        summary = [
            {key: value for key, value in item.items() if key != "entity"}
            for item in results
        ]
        batch_result = {
            "batch_id": batch_key,
            "status": status,
            "processed": len(results),
            "results": summary,
            "records": final_records,
            "audit": {
                "entity_id": self.SYNC_BATCH_AUDIT_PREFIX + batch_key,
                "url": "/api/audit?entity_id=" + self.SYNC_BATCH_AUDIT_PREFIX + batch_key,
                "batch_url": "/api/sync/batches/" + batch_key,
            },
        }
        self.repository.save_sync_batch(actor.user_id, batch_key, status, batch_result)
        self.audit.record(
            self.SYNC_BATCH_AUDIT_PREFIX + batch_key,
            actor,
            "sync_batch",
            None,
            status,
            {"batch_id": batch_key, "processed": len(results), "results": summary},
        )
        stored = self.repository.get_sync_batch(actor.user_id, batch_key)
        return stored or batch_result

    def sync_batch_status(self, actor, batch_key):
        result = self.repository.get_sync_batch(actor.user_id, batch_key)
        if result is None:
            raise NotFoundError("sync batch not found: " + str(batch_key))
        return {
            "batch": result,
            "audit": self.repository.list_audit(
                entity_id=self.SYNC_BATCH_AUDIT_PREFIX + batch_key
            ),
        }

    def _apply_sync_record(self, actor, batch_key, record):
        if not isinstance(record, dict):
            raise ValidationError("sync record must be an object")
        offline_id = record.get("offline_id")
        if not isinstance(offline_id, str) or not offline_id:
            raise ValidationError("offline_id is required")
        record_kind = record.get("kind")
        kind = self.rules.normalize_kind(record_kind) if record_kind else None
        data = record.get("data")
        if not isinstance(data, dict) or not data:
            raise ValidationError("data must be a non-empty object")
        field_timestamps = record.get("field_timestamps") or {}
        if not isinstance(field_timestamps, dict):
            raise ValidationError("field_timestamps must be an object")
        if "base_version" not in record:
            raise ValidationError("base_version is required")
        try:
            base_version = int(record.get("base_version"))
        except (TypeError, ValueError):
            raise ValidationError("base_version must be an integer")
        if base_version < 0:
            raise ValidationError("base_version must be >= 0")
        action = record.get("action")
        entity = self._resolve_sync_entity(kind, record, data)
        if entity is None:
            if action:
                raise ValidationError("action requires an existing entity")
            created = self.create(actor, kind or "observation", data)
            return self._sync_outcome(offline_id, "created", created, [])
        if kind and self.rules.normalize_kind(entity["kind"]) != kind:
            raise ValidationError("kind does not match entity " + entity["id"])
        if action:
            return self._apply_sync_action(
                actor, batch_key, offline_id, entity, action, data, base_version
            )
        return self._merge_sync_record(
            actor, batch_key, offline_id, entity, data, field_timestamps, base_version
        )

    def _resolve_sync_entity(self, kind, record, data):
        entity_id = record.get("entity_id")
        if entity_id:
            entity = self.repository.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + str(entity_id))
            return entity
        natural_key = NATURAL_KEYS.get(kind) if kind else None
        if natural_key and data.get(natural_key):
            matches = self._lookup(kind, natural_key, data[natural_key])
            if matches:
                return matches[0]
        return None

    def _apply_sync_action(self, actor, batch_key, offline_id, entity, action, data, base_version):
        if base_version != entity["version"]:
            # 基线版本落后：动作不重放，保留服务端现状，客户端意图列入冲突清单
            conflicts = [{
                "field": None,
                "action": action,
                "client_value": data,
                "server_version": entity["version"],
                "base_version": base_version,
                "resolution": "server_kept",
            }]
            return self._sync_outcome(offline_id, "conflict", entity, conflicts)
        updated = self.transition(
            actor,
            entity["id"],
            action,
            data,
            expected_version=entity["version"],
            audit_extra={"batch_id": batch_key, "offline_id": offline_id},
        )
        return self._sync_outcome(offline_id, "applied", updated, [])

    def _merge_sync_record(self, actor, batch_key, offline_id, entity, data, field_timestamps, base_version):
        server_fields, batch_fields = self._changes_since(entity, base_version, batch_key)
        merged = dict(entity["data"])
        applied = {}
        conflicts = []
        for field, value in data.items():
            if field in ("id", "kind"):
                continue
            current = entity["data"].get(field, _MISSING)
            if current is not _MISSING and current == value:
                continue
            if field in server_fields:
                # 同一字段双方都改：留服务端内容，客户端值列入冲突清单
                conflicts.append({
                    "field": field,
                    "server_value": entity["data"].get(field),
                    "client_value": value,
                    "resolution": "server_kept",
                })
                continue
            if field in batch_fields:
                # 批次内前面记录已写过该字段：字段时间更新的覆盖，否则保留
                previous_ts = batch_fields[field]
                mine = field_timestamps.get(field)
                if mine is None or (previous_ts is not None and str(mine) <= str(previous_ts)):
                    conflicts.append({
                        "field": field,
                        "server_value": entity["data"].get(field),
                        "client_value": value,
                        "resolution": "batch_kept",
                    })
                    continue
            merged[field] = value
            applied[field] = value
        if not applied:
            status = "conflict" if conflicts else "unchanged"
            return self._sync_outcome(offline_id, status, entity, conflicts)
        updated = self.repository.update_entity(entity["id"], None, entity["status"], merged)
        self.audit.record(
            entity["id"],
            actor,
            "sync_merge",
            entity["status"],
            updated["status"],
            {
                "batch_id": batch_key,
                "offline_id": offline_id,
                "applied": applied,
                "field_timestamps": {
                    field: field_timestamps.get(field) for field in applied
                },
                "conflicts": conflicts,
            },
        )
        status = "conflict" if conflicts else "merged"
        return self._sync_outcome(offline_id, status, updated, conflicts)

    def _changes_since(self, entity, base_version, batch_key):
        # 版本 v 对应第 v 条审计（create 为第 1 条，此后每次写入一条），
        # base_version 之后的审计条目即服务端在客户端基线之上的修改。
        entries = self.repository.list_audit(entity_id=entity["id"])
        server_fields = set()
        batch_fields = {}
        if base_version < 1:
            # 客户端没有有效基线：保守地把服务端现有字段都视为已修改
            server_fields.update(entity["data"].keys())
            later = entries[1:]
        else:
            later = entries[base_version:]
        for entry in later:
            detail = entry.get("detail") or {}
            if entry["action"] == "sync_merge" and detail.get("batch_id") == batch_key:
                for field, ts in (detail.get("field_timestamps") or {}).items():
                    batch_fields[field] = ts
                continue
            server_fields.update((detail.get("patch") or {}).keys())
            server_fields.update((detail.get("applied") or {}).keys())
        return server_fields, batch_fields

    @staticmethod
    def _sync_outcome(offline_id, status, entity, conflicts):
        return {
            "offline_id": offline_id,
            "status": status,
            "entity_id": entity["id"] if entity else None,
            "version": entity["version"] if entity else None,
            "conflicts": conflicts,
            "entity": entity,
        }
