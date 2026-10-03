from __future__ import annotations
import re
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
BATCH_ENTITY='调查批次'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
MERGE_ROLES=set(['investigator', 'safety_manager'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
SEVERITY_ORDER={s:i for i,s in enumerate(SEVERITIES)}
MEASURE_KINDS=set(['measure', 'action', 'corrective_action'])
EVIDENCE_KINDS=set(['evidence'])
_SCOPE_SPLIT=re.compile(r"[,，;；、\s]+")
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
def max_severity(severities):
    """返回一批严重度中的最高级别，用于批次重算主事故严重度。"""
    if not severities: raise ValidationError("severity列表不能为空")
    return max(severities, key=lambda s: SEVERITY_ORDER.get(s, -1))
def scope_tags(scope):
    """把范围字符串拆成标签集合，便于判断覆盖关系。"""
    if not isinstance(scope, str): return set()
    return {p.strip().lower() for p in _SCOPE_SPLIT.split(scope) if p.strip()}
def scope_covers(new_scope, original_scope):
    """新证据范围是否覆盖措施原范围：原范围标签全部被新范围包含，或原文为其子串。"""
    if not isinstance(new_scope, str) or not isinstance(original_scope, str): return False
    new_tags=scope_tags(new_scope); old_tags=scope_tags(original_scope)
    if not new_tags: return False
    if old_tags and old_tags <= new_tags: return True
    o=original_scope.strip().lower(); n=new_scope.strip().lower()
    return bool(o) and o in n
