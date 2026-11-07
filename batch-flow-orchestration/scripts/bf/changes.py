"""Conservative structural check for a lead's declared internal plan adjustment."""
from . import model


def internal_revision(old, path):
    new = model.normalize(path if isinstance(path, dict) else model.load_plan_file(path))
    # Technical instruction/design references may change; commitments, resources, review and authorization may not.
    for key in ('goal', 'roles', 'workspace', 'repos', 'batches', 'review_groups', 'protected', 'envelope', 'env', 'holds', 'policy'):
        if new.get(key) != old.get(key):
            return False
    if set(new['deliverables']) != set(old['deliverables']):
        return False
    for target, spec in new['deliverables'].items():
        keys = set(spec) | set(old['deliverables'][target])
        if any(spec.get(k) != old['deliverables'][target].get(k)
               for k in keys - {'instruction', 'design_ref', 'contract_version'}):
            return False
    return True
