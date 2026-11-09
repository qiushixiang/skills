"""Archived protocol fixtures; production defaults remain intent-v1/review-v1."""
from unittest.mock import patch
from bf import routing


def legacy_runtime(test):
    if not getattr(test, 'legacy_protocol', True):
        return
    original = routing.new_order
    def archived(*args, **kwargs):
        order = original(*args, **kwargs)
        order.pop('control_protocol', None)
        order.pop('performer', None)
        return order
    mock = patch.object(routing, 'new_order', archived)
    mock.start()
    test.addCleanup(mock.stop)
