"""Effective, scoped permissions shared by dispatch, cards and the change gate."""
from . import model


def allowed(plan, order):
    return list(dict.fromkeys(list(order['input'].get('allow_protected') or []) +
                             list(plan['envelope'].get('protected', {}).get(order['target'], []))))


def missing(plan, order):
    # Required paths are explicit dispatch requirements, never inferred from prose.
    return [p for p in order['input'].get('required_protected', []) if p not in allowed(plan, order)]


def install(state, order, paths, source):
    order['input']['allow_protected'] = list(dict.fromkeys(
        list(order['input'].get('allow_protected') or []) + paths))
    order['input'].setdefault('authorization_sources', []).append({'source': source, 'paths': list(paths)})


def validate_paths(paths):
    return isinstance(paths, list) and bool(paths) and all(model.valid_exact_path(p) for p in paths)
