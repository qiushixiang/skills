"""Check explicit policy invariants, not the engineering meaning of a lead's plan revision."""
from . import model


def internal_revision(old, path):
    new = model.normalize(path if isinstance(path, dict) else model.load_plan_file(path))
    # These are explicit user/runtime controls, not a claim about instruction semantics.
    for key in ('roles', 'workspace', 'repos', 'batches', 'review_groups', 'protected', 'envelope', 'env', 'holds', 'policy'):
        if new.get(key) != old.get(key):
            return False
    if set(new['deliverables']) != set(old['deliverables']):
        return False
    for target, spec in new['deliverables'].items():
        before = old['deliverables'][target]
        for key in ('batch', 'depends_on', 'resources', 'no_review', 'required_protected'):
            if spec.get(key) != before.get(key):
                return False
    return True
