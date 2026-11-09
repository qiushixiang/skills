"""Review and lead context continuity is independent of output-log volume."""
from bf import api, context, routing
from test_bf_core import Base


class ContextContinuity(Base):
    legacy_protocol = False

    def finished(self, obj, cid, cfg, size=10000):
        r = api.new_run(obj['id'], len(obj['runs']) + 1, 'lead', cfg['tool'], cfg['model'],
                        cfg['effort'], cid, 'fresh', 'test')
        r.update(result='VERIFIED', ended='2026-11-09T00:00:00Z', ended_ts=len(obj['runs']) + 1,
                 session_kb=size)
        obj['runs'].append(r)
        return r

    def decision(self, state, target, accounting=False):
        return routing.new_order(state, 'decide', target, {'by': 'user', 'ref': None},
                                 {'accounting_only': True} if accounting else {})

    def test_review_resumes_first_session_despite_large_logs(self):
        s = self.st(); o = next(o for o in s.orders.values() if o['type'] == 'review' and o['target'] == 'R12')
        cfg = s.plan['roles']['reviewer']
        self.finished(o, 'first-review', cfg)
        self.assertEqual(context.session_for(s, o, 'reviewer', cfg),
                         {'mode': 'resume', 'conversation_id': 'first-review', 'rule': 'review-resume'})

    def test_technical_decision_and_accounting_share_target_session_both_directions(self):
        for accounting_first in (False, True):
            s = self.st(); cfg = s.plan['roles']['lead']
            old = self.decision(s, 'D1', accounting_first)
            r = self.finished(old, 'lead-d1', cfg)
            next_ = self.decision(s, 'D1', not accounting_first)
            self.assertEqual(context.session_for(s, next_, 'lead', cfg)['conversation_id'], 'lead-d1')
            r2 = self.finished(next_, 'lead-d1', cfg)
            r2['ended_ts'] = r['ended_ts'] + 1
            again = self.decision(s, 'D1', accounting_first)
            self.assertEqual(context.session_for(s, again, 'lead', cfg)['conversation_id'], 'lead-d1')
            unrelated = self.decision(s, 'D2')
            self.assertEqual(context.session_for(s, unrelated, 'lead', cfg)['mode'], 'fresh')

    def test_lead_does_not_resume_author_session(self):
        s = self.st(); cfg = s.plan['roles']['lead']
        impl = s.orders[self.impl('D1')]
        self.finished(impl, 'author', cfg)
        old = self.decision(s, 'D1'); self.finished(old, 'author', cfg)
        for accounting in (False, True):
            obj = self.decision(s, 'D1', accounting)
            self.assertEqual(context.session_for(s, obj, 'lead', cfg)['rule'], 'decide-author-session')

    def test_backend_or_model_change_and_explicit_fresh_still_apply(self):
        s = self.st(); cfg = s.plan['roles']['lead']
        old = self.decision(s, 'D1'); self.finished(old, 'prior', cfg)
        obj = self.decision(s, 'D1', True)
        changed = dict(cfg, model='different-model')
        self.assertEqual(context.session_for(s, obj, 'lead', changed)['rule'], 'tool-change')
        obj['fresh_next'] = True
        self.assertEqual(context.session_for(s, obj, 'lead', cfg)['rule'], 'fresh-requested')

    def test_implementation_still_uses_size_limit(self):
        s = self.st(); obj = s.orders[self.impl('D1')]; cfg = s.plan['roles']['executor']
        r = self.finished(obj, 'implementation', cfg); r['performer'] = 'primary'
        self.assertEqual(context.session_for(s, obj, 'primary', cfg)['rule'], 'size-limit')

    def test_decision_and_accounting_cannot_use_shared_session_concurrently(self):
        s = self.st()
        technical = self.decision(s, 'D1'); technical['state'] = 'running'
        accounting = self.decision(s, 'D1', True)
        self.assertNotIn(accounting['id'], api.runnable(s, 10))
        technical['state'] = 'done'
        self.assertIn(accounting['id'], api.runnable(s, 10))
        technical['state'] = 'open'
        chosen = api.runnable(s, 10)
        self.assertEqual(sum(oid in chosen for oid in [technical['id'], accounting['id']]), 1)
