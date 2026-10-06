from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import RuleEngine

# 占用释放后由系统自动把排队工单调入空位时使用的身份。
SYSTEM_ACTOR = Actor("scheduler", "metrology")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # 排程写入可能在任意时刻中断；启动时先按工单核对并恢复占用账。
        self.recovered = self.repository.reconcile_occupancy()

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

    def _promote_queue(self, standard_id):
        """FIFO 提升该标准器的排队队列，直到空位用完；每张工单独立事务。"""
        promoted = []
        for order in self.repository.list_queued_for_standard(standard_id):
            standard = self.repository.get_entity(standard_id)
            covered = (
                standard["data"]["valid_from"] <= order["data"]["start_at"]
                and order["data"]["end_at"] <= standard["data"]["valid_until"]
            )
            if standard["status"] != "available" or not covered:
                # 标准器不可用或窗口失效（如 renew 缩短有效期）：跳过，不堵队。
                continue
            try:
                self.repository.hold_work_order(
                    order, standard, SYSTEM_ACTOR, action="reschedule",
                    detail={"reason": "queue promotion", "standard_id": standard_id},
                )
                promoted.append(order["id"])
            except ConflictError:
                # 队头放不进（容量满）就停止，保持先来后到顺序。
                break
        return promoted

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        data = dict(data or {})
        kind = self.rules.normalize_kind(entity["kind"])
        expected = int(expected_version) if expected_version is not None else entity["version"]

        if kind == "work_order":
            return self._work_order_action(actor, entity, action, data, expected)
        if kind == "standard":
            return self._standard_action(actor, entity, action, data, expected)
        return self._plain_transition(actor, entity, action, data, expected)

    def _plain_transition(self, actor, entity, action, data, expected):
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"], actor, action, entity["status"], updated["status"],
            {"patch": patch},
        )
        return updated

    def _work_order_action(self, actor, entity, action, data, expected):
        # 先跑状态机/角色/必填校验，占用与容量判断在仓储事务内再查一遍。
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        if expected != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"])
            )
        standard = self.repository.get_entity(entity["data"]["standard_id"])

        if action == "schedule":
            # 容量满即冲突：先到者留下，后到者收到冲突，不占位。
            updated = self.repository.hold_work_order(entity, standard, actor, "schedule",
                                                      {"patch": patch})
            return updated

        if action == "enqueue":
            # 容量用满时新工单排队（可先排队，也可在冲突后改走 enqueue）。
            return self.repository.enqueue_work_order(entity, standard, actor)

        if action == "complete":
            updated = self.repository.finish_work_order(
                entity, actor, patch.get("result"), patch.get("completed_at")
            )
            self._promote_queue(standard["id"])
            return updated

        if action == "cancel":
            updated = self.repository.cancel_work_order(
                entity, actor, patch.get("reason")
            )
            self._promote_queue(standard["id"])
            return updated

        if action == "void":
            updated = self.repository.void_work_order(entity, actor, patch.get("reason"))
            self._promote_queue(standard["id"])
            return updated

        if action == "reschedule":
            # 排队工单被队首提升之外的手工重排：走同一条先到先占事务。
            return self.repository.hold_work_order(
                entity, standard, actor, "reschedule", {"patch": patch}
            )

        raise ConflictError("unsupported work order action: " + action)

    def _standard_action(self, actor, entity, action, data, expected):
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        if expected != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"])
            )
        if action == "renew":
            return self.repository.renew_standard(entity, actor, patch)
        # 标准器状态一变化：依赖它的未完成工单作废退回待排，已完成的保留。
        # 作废工单不自动重排，由计量员核对新窗口后重新 schedule/enqueue。
        updated, _affected = self.repository.change_standard_status(
            entity, next_status, actor, action, patch
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

    def occupancy(self, standard_id=None, state=None):
        return self.repository.list_occupancy(standard_id=standard_id, state=state)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
