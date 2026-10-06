from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# ---------------------------------------------------------------------------
# 时间与区间规则
# ---------------------------------------------------------------------------

def canonical_time(value):
    """把 2026-10-06 / 2026-10-06T09:00 统一为可字典序比较的秒级 ISO 串。"""
    if value is None:
        raise ValidationError("time value is required")
    text = str(value).strip().replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("bad time value: " + str(value))
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def canonical_window(start, end):
    start_at = canonical_time(start)
    end_at = canonical_time(end)
    if end_at <= start_at:
        raise ValidationError("time window must have end_at after start_at")
    return start_at, end_at


def windows_overlap(start_a, end_a, start_b, end_b):
    """半开区间 [start, end)；端点相接不算重叠，相邻工单可背靠背。"""
    return start_a < end_b and start_b < end_a


def window_covered(start, end, cover_start, cover_end):
    """标准器有效期 [cover_start, cover_end] 必须盖住整段（含右边界）。"""
    return cover_start <= start and end <= cover_end


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


# ---------------------------------------------------------------------------
# 自定义建单校验
# ---------------------------------------------------------------------------

def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_standard(actor, data, lookup):
    start_at, end_at = canonical_window(data.get("valid_from"), data.get("valid_until"))
    data["valid_from"] = start_at
    data["valid_until"] = end_at
    capacity = data.get("capacity", 1)
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        raise ValidationError("capacity must be a positive integer")


def _validate_work_order(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    standard = _find_one(lookup, "standard", "id", data.get("standard_id"))
    if not standard:
        raise ValidationError("standard does not exist")
    start_at, end_at = canonical_window(data.get("start_at"), data.get("end_at"))
    data["start_at"] = start_at
    data["end_at"] = end_at
    if not window_covered(
        start_at,
        end_at,
        standard["data"].get("valid_from", ""),
        standard["data"].get("valid_until", ""),
    ):
        raise ValidationError("standard validity does not cover the whole work window")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), "2026-09-24"):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    return {"released_by": actor.user_id}


def _validate_standard_renew(actor, entity, data, lookup):
    start_at, end_at = canonical_window(data.get("valid_from"), data.get("valid_until"))
    return {"valid_from": start_at, "valid_until": end_at}


def _validate_work_order_complete(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("work order result must be passed or failed")


CUSTOM_CREATE = {
    'calibration': _validate_calibration,
    'standard': _validate_standard,
    'work_order': _validate_work_order,
}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('result', 'release'): _validate_result_release,
    ('standard', 'renew'): _validate_standard_renew,
    ('work_order', 'complete'): _validate_work_order_complete,
}


class RuleEngine:
    ALIASES = {
        'instruments': 'instrument',
        'calibrations': 'calibration',
        'methods': 'method',
        'results': 'result',
        'standards': 'standard',
        'work_orders': 'work_order',
        'workorders': 'work_order',
    }
    INITIAL_STATUS = {
        'instrument': 'active',
        'calibration': 'requested',
        'method': 'draft',
        'result': 'pending',
        'standard': 'available',
        'work_order': 'pending',
    }
    TRANSITIONS = {
        'instrument': {
            'send_calibration': (('active',), 'calibrating'),
            'calibrate': (('calibrating',), 'active'),
            'quarantine': (('active',), 'quarantined'),
            'restore': (('quarantined',), 'active'),
        },
        'calibration': {
            'perform': (('requested', 'failed'), 'passed'),
            'approve': (('passed',), 'approved'),
            'reject': (('failed',), 'rejected'),
        },
        'method': {
            'validate_method': (('draft',), 'validated'),
            'revoke_method': (('validated',), 'revoked'),
        },
        'result': {
            'release': (('pending',), 'released'),
            'block': (('pending',), 'blocked'),
            'reanalyze': (('blocked',), 'pending'),
        },
        'standard': {
            'suspend': (('available',), 'suspended'),
            'reactivate': (('suspended',), 'available'),
            'expire': (('available', 'suspended'), 'expired'),
            'renew': (('available', 'suspended', 'expired'), None),
        },
        'work_order': {
            'schedule': (('pending',), 'scheduled'),
            'enqueue': (('pending',), 'queued'),
            'complete': (('scheduled',), 'completed'),
            'cancel': (('scheduled', 'queued'), 'cancelled'),
            'reschedule': (('queued',), 'scheduled'),
            'void': (('scheduled', 'queued'), 'pending'),
        },
    }
    CREATE_REQUIRED = {
        'instrument': ('name', 'serial'),
        'calibration': ('instrument_id', 'requested_at'),
        'method': ('name', 'version'),
        'result': ('sample_id', 'measurement'),
        'standard': ('name', 'serial', 'valid_from', 'valid_until'),
        'work_order': ('instrument_id', 'standard_id', 'start_at', 'end_at'),
    }
    ACTION_REQUIRED = {
        ('instrument', 'calibrate'): ('due_at', 'passed'),
        ('instrument', 'quarantine'): ('reason',),
        ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'),
        ('calibration', 'approve'): ('authorized_by',),
        ('calibration', 'reject'): ('reason',),
        ('method', 'validate_method'): ('parameters', 'instrument_ids'),
        ('method', 'revoke_method'): ('reason',),
        ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'),
        ('result', 'block'): ('reason',),
        ('result', 'reanalyze'): ('reason',),
        ('standard', 'suspend'): ('reason',),
        ('standard', 'expire'): ('reason',),
        ('standard', 'renew'): ('valid_from', 'valid_until'),
        ('work_order', 'complete'): ('result', 'completed_at'),
        ('work_order', 'cancel'): ('reason',),
        ('work_order', 'void'): ('reason',),
    }
    CREATE_ROLES = {
        'instrument': ('admin', 'technician'),
        'calibration': ('admin', 'metrology'),
        'method': ('admin', 'authorizer'),
        'result': ('admin', 'analyst'),
        'standard': ('admin', 'metrology'),
        'work_order': ('admin', 'metrology'),
    }
    ROLE_ACTIONS = {
        'send_calibration': ('admin', 'technician'),
        'calibrate': ('admin', 'metrology'),
        'quarantine': ('admin', 'metrology'),
        'restore': ('admin', 'metrology'),
        'perform': ('admin', 'metrology'),
        'approve': ('admin', 'authorizer'),
        'reject': ('admin', 'authorizer'),
        'validate_method': ('admin', 'authorizer'),
        'revoke_method': ('admin', 'authorizer'),
        'release': ('admin', 'analyst'),
        'block': ('admin', 'analyst'),
        'reanalyze': ('admin', 'analyst'),
        'suspend': ('admin', 'metrology'),
        'reactivate': ('admin', 'metrology'),
        'expire': ('admin', 'metrology'),
        'renew': ('admin', 'metrology'),
        'schedule': ('admin', 'metrology'),
        'enqueue': ('admin', 'metrology'),
        'complete': ('admin', 'metrology'),
        'cancel': ('admin', 'metrology'),
        'reschedule': ('admin', 'metrology'),
        'void': ('admin', 'metrology'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        patch = dict(data)
        if custom:
            extra = custom(actor, entity, data, lookup) or {}
            patch.update(extra)
        if next_status is None:
            next_status = entity["status"]
        return next_status, patch
