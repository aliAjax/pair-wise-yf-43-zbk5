from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, CapacityFull, ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

        # Custom (kind, action) handlers that need side effects beyond a status change.
        self._transition_handlers = {
            ("standard", "deactivate"): self._handle_standard_deactivate,
            ("standard", "activate"): self._handle_standard_activate,
            ("workorder", "schedule"): self._handle_workorder_schedule,
            ("workorder", "complete"): self._handle_workorder_complete,
        }

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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        handler = self._transition_handlers.get((kind, action))
        if handler:
            return handler(actor, entity, dict(data or {}), expected_version)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected_version, next_status, merged)
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

    def occupancies(self):
        return self.repository.list_occupancies()

    # ------------------------------------------------------------------
    # Scheduling: standards, work orders, and the occupancy ledger.
    # ------------------------------------------------------------------

    @staticmethod
    def _covers(standard, slot_start, slot_end):
        """A standard's validity period must cover the whole slot (date granularity)."""
        data = standard["data"]
        valid_from = str(data.get("valid_from", ""))[:10]
        valid_to = str(data.get("valid_to", ""))[:10]
        start = str(slot_start)[:10]
        end = str(slot_end)[:10]
        return valid_from <= start and valid_to >= end

    def _handle_workorder_schedule(self, actor, entity, data, expected_version):
        self.rules._ensure_role(actor, ("admin", "metrology"))
        slot_start = data.get("slot_start") or entity["data"].get("slot_start")
        slot_end = data.get("slot_end") or entity["data"].get("slot_end")
        if not slot_start or not slot_end:
            raise ValidationError("workorder requires slot_start and slot_end")
        if str(slot_start) >= str(slot_end):
            raise ValidationError("workorder slot_start must be before slot_end")
        standard_id = data.get("standard_id") or entity["data"].get("standard_id")
        # Idempotent retry: already scheduled on the same standard and slot -> no-op.
        if entity["status"] == "scheduled":
            current_std = entity["data"].get("standard_id")
            current_start = entity["data"].get("slot_start")
            current_end = entity["data"].get("slot_end")
            if (standard_id in (None, current_std)
                    and str(current_start) == str(slot_start)
                    and str(current_end) == str(slot_end)):
                active = self.repository.get_active_occupancy(entity["id"])
                if active and active["standard_id"] == current_std:
                    return entity
            raise ConflictError("workorder is already scheduled on another slot")
        if entity["status"] != "pending":
            raise ConflictError("cannot schedule workorder in status %s" % entity["status"])
        return self._place_workorder(entity, standard_id, slot_start, slot_end, actor, expected_version)

    def _place_workorder(self, wo, standard_id, slot_start, slot_end, actor, expected_version):
        explicit = bool(standard_id)
        if explicit:
            standard = self.repository.get_entity(standard_id)
            if not standard or self.rules.normalize_kind(standard["kind"]) != "standard":
                raise ValidationError("standard does not exist")
            if standard["status"] != "active":
                raise ValidationError("standard is not active")
            if not self._covers(standard, slot_start, slot_end):
                raise ValidationError("standard validity does not cover the requested slot")
            standards = [standard]
        else:
            standards = [
                item
                for item in self.repository.list_entities(kind="standard", status="active")
                if self._covers(item, slot_start, slot_end)
            ]
        if not standards:
            if explicit:
                raise ConflictError("no standard available for the requested slot")
            return self._enqueue(wo, slot_start, slot_end, actor, expected_version)
        for standard in standards:
            capacity = int(standard["data"].get("capacity", 1))
            merged = dict(wo["data"])
            merged.update(
                {"standard_id": standard["id"], "slot_start": slot_start, "slot_end": slot_end}
            )
            try:
                updated = self.repository.book_and_schedule(
                    wo["id"],
                    standard["id"],
                    slot_start,
                    slot_end,
                    capacity,
                    expected_version,
                    merged,
                )
            except CapacityFull:
                if explicit:
                    raise ConflictError(
                        "standard %s is fully booked for the requested slot" % standard["id"]
                    )
                continue
            except ConflictError:
                # Version drift (e.g. a concurrent retry): re-read and treat an
                # already-scheduled matching workorder as idempotent success.
                fresh = self.repository.get_entity(wo["id"])
                if (
                    fresh
                    and fresh["status"] == "scheduled"
                    and fresh["data"].get("standard_id") == standard["id"]
                ):
                    return fresh
                raise
            self.audit.record(
                wo["id"],
                actor,
                "schedule",
                wo["status"],
                "scheduled",
                {"standard_id": standard["id"], "slot_start": slot_start, "slot_end": slot_end},
            )
            return updated
        return self._enqueue(wo, slot_start, slot_end, actor, expected_version)

    def _enqueue(self, wo, slot_start, slot_end, actor, expected_version):
        merged = dict(wo["data"])
        merged["slot_start"] = slot_start
        merged["slot_end"] = slot_end
        merged["queued_at"] = utcnow()
        merged.pop("standard_id", None)
        updated = self.repository.update_entity(wo["id"], expected_version, "pending", merged)
        self.audit.record(
            wo["id"],
            actor,
            "enqueue",
            wo["status"],
            "pending",
            {"slot_start": slot_start, "slot_end": slot_end},
        )
        return updated

    def drain_queue(self, actor=None):
        """Schedule queued (pending) workorders onto available standards, FIFO."""
        actor = actor or Actor("system", "admin")
        pending = self.repository.list_entities(kind="workorder", status="pending")
        pending.sort(
            key=lambda w: (str(w["data"].get("queued_at") or w["created_at"]), w["id"])
        )
        scheduled = []
        for wo in pending:
            slot_start = wo["data"].get("slot_start")
            slot_end = wo["data"].get("slot_end")
            if not slot_start or not slot_end:
                continue
            standards = [
                item
                for item in self.repository.list_entities(kind="standard", status="active")
                if self._covers(item, slot_start, slot_end)
            ]
            for standard in standards:
                capacity = int(standard["data"].get("capacity", 1))
                merged = dict(wo["data"])
                merged["standard_id"] = standard["id"]
                try:
                    updated = self.repository.book_and_schedule(
                        wo["id"],
                        standard["id"],
                        slot_start,
                        slot_end,
                        capacity,
                        wo["version"],
                        merged,
                    )
                except (CapacityFull, ConflictError):
                    continue
                self.audit.record(
                    wo["id"],
                    actor,
                    "schedule",
                    "pending",
                    "scheduled",
                    {
                        "standard_id": standard["id"],
                        "slot_start": slot_start,
                        "slot_end": slot_end,
                        "auto": True,
                    },
                )
                scheduled.append(updated)
                break
        return scheduled

    def _handle_standard_deactivate(self, actor, entity, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, entity, "deactivate", data, self._lookup
        )
        reason = data.get("reason")
        # Dependent work orders that have not completed are voided and returned to
        # pending scheduling. Completed calibrations are retained untouched.
        workorders = self.repository.list_entities(kind="workorder")
        displaced = []
        for wo in workorders:
            if wo["data"].get("standard_id") != entity["id"]:
                continue
            if wo["status"] in ("scheduled", "in_progress"):
                self.repository.release_active_occupancy_for_workorder(wo["id"])
                merged = dict(wo["data"])
                merged["void_count"] = int(merged.get("void_count", 0)) + 1
                merged["last_void_reason"] = reason
                merged["standard_id"] = None
                updated = self.repository.update_entity(wo["id"], wo["version"], "pending", merged)
                self.audit.record(
                    wo["id"],
                    actor,
                    "void",
                    wo["status"],
                    "pending",
                    {"standard_id": entity["id"], "reason": reason},
                )
                displaced.append(updated)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected_version, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            "deactivate",
            entity["status"],
            next_status,
            {"reason": reason, "displaced": [item["id"] for item in displaced]},
        )
        self.drain_queue(actor)
        return updated

    def _handle_standard_activate(self, actor, entity, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, entity, "activate", data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected_version, next_status, merged)
        self.audit.record(
            entity["id"], actor, "activate", entity["status"], next_status, {}
        )
        self.drain_queue(actor)
        return updated

    def _handle_workorder_complete(self, actor, entity, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, entity, "complete", data, self._lookup
        )
        standard_id = entity["data"].get("standard_id")
        self.repository.release_active_occupancy_for_workorder(entity["id"])
        merged = dict(entity["data"])
        merged.update(patch)
        merged["standard_id"] = None
        updated = self.repository.update_entity(entity["id"], expected_version, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            "complete",
            entity["status"],
            next_status,
            {"standard_id": standard_id},
        )
        self.drain_queue(actor)
        return updated

    def recover(self, actor=None):
        """Reconcile work orders with their occupancies, one work order at a time.

        Guarantees no work order is left without an active occupancy while marked
        scheduled, and no work order keeps two active occupancies (the partial unique
        index already forbids duplicates at the database level).
        """
        actor = actor or Actor("system", "admin")
        report = {"reconciled": [], "ok": []}
        for wo in self.repository.list_entities(kind="workorder"):
            active = self.repository.get_active_occupancy(wo["id"])
            if active:
                if wo["status"] == "pending":
                    merged = dict(wo["data"])
                    merged["standard_id"] = active["standard_id"]
                    merged["slot_start"] = active["slot_start"]
                    merged["slot_end"] = active["slot_end"]
                    self.repository.update_entity(wo["id"], wo["version"], "scheduled", merged)
                    self.audit.record(
                        wo["id"],
                        actor,
                        "recover",
                        "pending",
                        "scheduled",
                        {"occupancy_id": active["id"]},
                    )
                    report["reconciled"].append(wo["id"])
                else:
                    report["ok"].append(wo["id"])
            else:
                if wo["status"] == "scheduled":
                    merged = dict(wo["data"])
                    merged["standard_id"] = None
                    self.repository.update_entity(wo["id"], wo["version"], "pending", merged)
                    self.audit.record(
                        wo["id"], actor, "recover", "scheduled", "pending", {}
                    )
                    report["reconciled"].append(wo["id"])
                else:
                    report["ok"].append(wo["id"])
        return report
