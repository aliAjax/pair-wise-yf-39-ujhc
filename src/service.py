from urllib.parse import quote
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None, audit_extra=None):
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
        detail = {"kind": kind}
        if audit_extra:
            detail.update(audit_extra)
        self.audit.record(entity_id, actor, "create", None, status, detail)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
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
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
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

    def sync_batch(self, actor, batch_id, records):
        if not isinstance(records, list) or not records:
            raise ValidationError("records must be a non-empty list")
        batch_id = str(batch_id or uuid4())
        stored = self.repository.get_sync_batch(actor.user_id, batch_id)
        if stored is not None:
            replayed = dict(stored)
            replayed["replayed"] = True
            return replayed
        results = []
        for record in records:
            try:
                results.append(self._sync_record(actor, batch_id, record))
            except DomainError as exc:
                results.append({
                    "offline_id": record.get("offline_id") if isinstance(record, dict) else None,
                    "status": "failed",
                    "entity_id": None,
                    "version": None,
                    "record": None,
                    "conflicts": [],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                })
        failed = sum(1 for item in results if item["status"] == "failed")
        if not failed:
            status = "ok"
        elif failed == len(results):
            status = "failed"
        else:
            status = "partial"
        audit_ref = "batch:" + batch_id
        response = {
            "batch_id": batch_id,
            "status": status,
            "replayed": False,
            "results": results,
            "audit": {
                "entity_id": audit_ref,
                "url": "/api/audit?entity_id=" + quote(audit_ref),
            },
        }
        self.repository.save_sync_batch(actor.user_id, batch_id, response)
        self.audit.record(
            audit_ref,
            actor,
            "sync_batch",
            None,
            status,
            {
                "batch_id": batch_id,
                "total": len(results),
                "failed": failed,
                "results": [
                    {
                        "offline_id": item["offline_id"],
                        "status": item["status"],
                        "entity_id": item["entity_id"],
                        "conflicts": len(item["conflicts"]),
                    }
                    for item in results
                ],
            },
        )
        return response

    def _sync_record(self, actor, batch_id, record):
        if not isinstance(record, dict):
            raise ValidationError("each sync record must be an object")
        offline_id = record.get("offline_id")
        if not offline_id:
            raise ValidationError("offline_id is required")
        kind = record.get("kind")
        data = record.get("data")
        if not isinstance(data, dict) or not data:
            raise ValidationError("record data must be a non-empty object")
        field_timestamps = record.get("field_timestamps") or {}
        if not isinstance(field_timestamps, dict):
            raise ValidationError("field_timestamps must be an object")
        entity = None
        entity_id = record.get("entity_id") or data.get("id")
        if entity_id:
            entity = self.repository.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + str(entity_id))
        if entity is None:
            entity = self._sync_match(kind, data)
        if entity is None:
            created = self.create(
                actor,
                kind,
                data,
                audit_extra={"batch_id": batch_id, "offline_id": offline_id},
            )
            return {
                "offline_id": offline_id,
                "status": "created",
                "entity_id": created["id"],
                "version": created["version"],
                "record": created,
                "conflicts": [],
                "error": None,
            }
        if kind and entity["kind"] != self.rules.normalize_kind(kind):
            raise ValidationError(
                "kind mismatch: record %s, server %s" % (kind, entity["kind"])
            )
        return self._sync_merge(
            actor,
            batch_id,
            offline_id,
            entity,
            data,
            field_timestamps,
            record.get("baseline_version"),
        )

    def _sync_match(self, kind, data):
        field = self.rules.sync_match_field(kind)
        value = data.get(field) if field else None
        if not field or value in (None, ""):
            return None
        matches = self._lookup(kind, field, value)
        if not matches:
            return None
        if len(matches) > 1:
            raise ConflictError(
                "multiple %s records match %s=%s; use entity_id" % (kind, field, value)
            )
        return matches[0]

    def _sync_merge(self, actor, batch_id, offline_id, entity, data, field_timestamps, baseline_version):
        current_version = entity["version"]
        if baseline_version is None:
            raise ValidationError(
                "baseline_version is required for merging into " + entity["id"]
            )
        try:
            baseline = int(baseline_version)
        except (TypeError, ValueError):
            raise ValidationError("baseline_version must be an integer")
        if baseline < 1:
            raise ValidationError("baseline_version must be >= 1")
        if baseline > current_version:
            raise ValidationError(
                "baseline_version %s is ahead of server version %s" % (baseline, current_version)
            )
        # 字段时间标记了客户端离线改过的字段；缺省时保守地认为 data 全部字段都被改过。
        if field_timestamps:
            client_fields = [f for f in field_timestamps if f in data and f != "id"]
        else:
            client_fields = [f for f in data if f != "id"]
        server_changed = self._server_changed_fields(entity["id"], baseline)
        merged = dict(entity["data"])
        applied = []
        conflicts = []
        for field in client_fields:
            client_value = data[field]
            if field in server_changed:
                server_value = entity["data"].get(field)
                if server_value != client_value:
                    conflicts.append({
                        "field": field,
                        "server_value": server_value,
                        "client_value": client_value,
                        "client_modified_at": field_timestamps.get(field),
                        "resolution": "server_kept",
                    })
                    continue
            merged[field] = client_value
            applied.append(field)
        self.rules.validate_sync_merge(actor, entity["kind"], merged)
        updated = entity
        if applied:
            updated = self.repository.update_entity(
                entity["id"], current_version, entity["status"], merged
            )
            self.audit.record(
                entity["id"],
                actor,
                "sync_merge",
                entity["status"],
                updated["status"],
                {
                    "batch_id": batch_id,
                    "offline_id": offline_id,
                    "baseline_version": baseline,
                    "applied_fields": applied,
                    "conflicts": conflicts,
                    "field_timestamps": field_timestamps,
                    "version": updated["version"],
                },
            )
        if applied:
            status = "merged"
        elif conflicts:
            status = "conflict"
        else:
            status = "unchanged"
        return {
            "offline_id": offline_id,
            "status": status,
            "entity_id": entity["id"],
            "version": updated["version"],
            "record": updated,
            "conflicts": conflicts,
            "error": None,
        }

    def _server_changed_fields(self, entity_id, baseline_version):
        # 审计时间线里每个实体级条目恰好对应一次版本递增（create=1，其后每条 +1），
        # 因此序号大于基线版本的条目即为“服务端在基线之后”的修改。
        changed = set()
        for index, entry in enumerate(self.repository.list_audit(entity_id=entity_id)):
            if index + 1 <= baseline_version:
                continue
            detail = entry["detail"]
            patch = detail.get("patch")
            if patch:
                changed.update(patch.keys())
            applied = detail.get("applied_fields")
            if applied:
                changed.update(applied)
        return changed
