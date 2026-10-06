from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_standard(actor, data, lookup):
    valid_from = data.get("valid_from")
    valid_to = data.get("valid_to")
    if not valid_from or not valid_to:
        raise ValidationError("standard requires valid_from and valid_to")
    if str(valid_from)[:10] > str(valid_to)[:10]:
        raise ValidationError("standard valid_from must be on or before valid_to")
    capacity = data.get("capacity")
    if capacity is None:
        raise ValidationError("standard requires capacity")
    try:
        cap = int(capacity)
    except (TypeError, ValueError):
        raise ValidationError("standard capacity must be an integer")
    if cap < 1:
        raise ValidationError("standard capacity must be at least 1")
    data["capacity"] = cap


def _validate_workorder(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("workorder requires an existing instrument")
    slot_start = data.get("slot_start")
    slot_end = data.get("slot_end")
    if not slot_start or not slot_end:
        raise ValidationError("workorder requires slot_start and slot_end")
    if str(slot_start) >= str(slot_end):
        raise ValidationError("workorder slot_start must be before slot_end")
    standard_id = data.get("standard_id")
    if standard_id:
        standard = _find_one(lookup, "standard", "id", standard_id)
        if not standard:
            raise ValidationError("referenced standard does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


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


CUSTOM_CREATE = {'calibration': _validate_calibration, 'standard': _validate_standard, 'workorder': _validate_workorder}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'standards': 'standard', 'workorders': 'workorder'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'standard': 'active', 'workorder': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}, 'standard': {'deactivate': (('active',), 'inactive'), 'activate': (('inactive',), 'active')}, 'workorder': {'schedule': (('pending',), 'scheduled'), 'start': (('scheduled',), 'in_progress'), 'complete': (('in_progress',), 'completed')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement'), 'standard': ('name', 'serial', 'valid_from', 'valid_to', 'capacity'), 'workorder': ('instrument_id', 'slot_start', 'slot_end')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',), ('standard', 'deactivate'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst'), 'standard': ('admin', 'metrology'), 'workorder': ('admin', 'metrology')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst'), 'deactivate': ('admin', 'metrology'), 'activate': ('admin', 'metrology'), 'schedule': ('admin', 'metrology'), 'start': ('admin', 'metrology'), 'complete': ('admin', 'metrology')}

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
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
