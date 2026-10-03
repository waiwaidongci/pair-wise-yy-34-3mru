from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
BATCH_ID_PREFIX='IB'; BATCH_ENTITY='批次'
BATCH_CREATE_ROLES=set(['investigator','safety_manager']); BATCH_MERGE_ROLES=set(['reporter','investigator','safety_manager']); MEASURE_VERIFY_ROLES=set(['safety_manager']); MEASURE_REOPEN_ROLES=set(['safety_manager','investigator'])
BATCH_VIEW_ROLES=set(['reporter','investigator','safety_manager','viewer'])
VERIFIED_STATUSES={'verified','reopened'}
def aggregate_severity(severities):
    chosen=SEVERITIES[0]
    for severity in severities:
        if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
        if SEVERITY_WEIGHT[severity]>SEVERITY_WEIGHT[chosen]: chosen=severity
    return chosen
def aggregate_quantity(quantities):
    return round(sum(float(q or 0.0) for q in quantities),6)
def normalize_scope(scope):
    if not isinstance(scope,str) or not scope.strip(): raise ValidationError("measure_scope不能为空")
    return scope.strip()
def scope_covers(evidence_scope,measure_scope):
    """新证据范围覆盖已验证措施的原范围时返回True。

    规则：新证据为全局范围（*）、与措施范围相同，或为其更具体的下级范围
    （以冒号分层，例如 line3:press 覆盖 line3）时判定覆盖；反之不覆盖，
    即措施原范围之外的新证据不会让措施失效。
    """
    evidence=normalize_scope(evidence_scope); measure=normalize_scope(measure_scope)
    if evidence=='*': return True
    if evidence==measure: return True
    return evidence.startswith(measure+':')
def covered_measures(verified_measures,evidence_scopes):
    covered=[]
    scopes=[normalize_scope(s) for s in evidence_scopes]
    for measure in verified_measures:
        scope=measure.get('measure_scope')
        if not scope: continue
        hits=[s for s in scopes if scope_covers(s,scope)]
        if hits: covered.append((measure,hits))
    return covered
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
