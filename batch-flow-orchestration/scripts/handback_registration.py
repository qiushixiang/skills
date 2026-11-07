"""Register role handbacks before scheduling more work. No prose interpretation."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile

import flow


class RegistrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RegistrationError(message)


def source_file(directory, name):
    path = directory / name
    require(path.resolve().parent == directory.resolve(), 'registration file escapes its card directory')
    return path


def read_operations(directory, card, state):
    manifest = source_file(directory, 'registration.json')
    if manifest.exists():
        value = json.loads(manifest.read_text())
        require(isinstance(value, dict) and set(value) == {'operations'}, 'registration.json needs only operations')
        operations = value['operations']
        require(isinstance(operations, list), 'registration operations must be an array')
        return operations
    # A successful, explicitly dispatched confirmation already identifies the
    # exact condition and responsible role. It needs no extra writable sidecar.
    if card['kind'] == 'CONFIRM_CONDITION' and state.get('result') == 'VERIFIED':
        return [dict(type='decision_progress', data=dict(id=card['decision'],
                     requirement=card['target'], status='confirmed'))]
    # Compatibility with old DECIDE cards: exact, conventional JSON filenames,
    # never file paths scraped from prose or a shell command in the report.
    if card['kind'] == 'DECIDE':
        resolve = source_file(directory, 'resolve_' + card['target'] + '.json')
        confirm = source_file(directory, 'confirm_' + card['target'] + '.json')
        if resolve.exists():
            operations = [dict(type='resolve', data=json.loads(resolve.read_text()))]
            if confirm.exists():
                operations.append(dict(type='decision_progress', data=json.loads(confirm.read_text())))
            return operations
    raise RegistrationError('role delivery has no registration.json; preserve it and repair registration, do not redispatch')


def normalize(operation, card, state, ledger, proof, root):
    require(isinstance(operation, dict) and set(operation) == {'type', 'data'},
            'registration operation needs type/data; actor is selected by the dispatcher')
    op = copy.deepcopy(operation); kind = op['type']; data = op['data']
    require(isinstance(kind, str), 'registration operation type must be a string')
    require(isinstance(data, dict) and 'note' not in data, 'use explicit evidence, not note-generated records')
    decision = card.get('decision') or (card['target'] if card['kind'] == 'DECIDE' else None)
    candidate = (data.get('decision_id') if kind == 'plan_update' else
                 data.get('replacement') if kind == 'supersede_decision' else data.get('id'))
    require(candidate is None or isinstance(candidate, str), 'decision id must be a string')
    if not decision and card['kind'] in {'PLAN_REPAIR', 'SYNC_DESIGN', 'PREPARE_RESUMPTION'}:
        if card['target'] in ledger['decisions'].get(candidate, {}).get('blocks', []):
            decision = candidate
    if kind == 'decision_progress':
        require(decision and data.get('id') == decision, 'confirmation targets another decision')
        require(data.get('status') in {'confirmed', 'closed'}, 'unsupported handback decision status')
        if data['status'] == 'confirmed':
            dec = ledger['decisions'].get(decision, {})
            requirement = next((r for r in (dec.get('resolution') or {}).get('requirements', [])
                                if r['id'] == data.get('requirement')), None)
            require(requirement and requirement['kind'] == 'confirmation'
                    and requirement.get('owner') == card['role'] and card['role'] != 'user',
                    'role may confirm only its own explicitly evidenced condition, never a user condition')
            if card['kind'] == 'CONFIRM_CONDITION':
                require(data['requirement'] == card['target'], 'confirmation targets another condition')
        actor = card.get('actor', 'orchestrator')
    else:
        require(card['role'] == 'architect', 'only technical-lead handbacks may submit management operations')
        if kind == 'signal':
            require(card['kind'] in {'PLAN_REPAIR', 'SYNC_DESIGN', 'PREPARE_RESUMPTION'}
                    and data.get('blocks') == [card['target']], 'signal is outside this card scope')
        elif kind == 'resolve':
            require(decision and data.get('id') == decision, 'resolve targets another decision')
        elif kind == 'plan_update':
            require(decision and data.get('decision_id') == decision, 'plan update needs this card decision')
        elif kind in {'classify_attempt', 'resume_attempt'}:
            dec = ledger['decisions'].get(decision, {})
            attempt = next((a for a in ledger['state']['attempts'] if a['id'] == data.get('attempt')), None)
            scoped = attempt and attempt['task'] in dec.get('blocks', [])
            if card['kind'] == 'PREPARE_RESUMPTION':
                latest = flow.latest(ledger, card['target'])
                scoped = attempt and latest and attempt['id'] == latest['id'] and attempt['task'] == card['target']
            require(scoped, 'attempt is outside this decision scope')
        elif kind == 'repin_requirement':
            require(decision and data.get('id') == decision, 'repin targets another decision')
        elif kind == 'supersede_decision':
            require(decision and data.get('replacement') == decision, 'replacement is outside this decision')
        elif kind == 'design_refresh':
            require(card['kind'] in {'SYNC_DESIGN', 'PLAN_REPAIR', 'DECIDE'}, 'unexpected design refresh')
        else:
            raise RegistrationError('unsupported handback operation: ' + str(kind))
        actor = 'architect'
    if kind in {'resolve', 'plan_update', 'design_refresh', 'repin_requirement', 'supersede_decision'}:
        context = data.get('context')
        require(context is None or isinstance(context, dict), 'context must be an object')
        require(not context or context.get('conversation_id') == state['conversation_id'],
                'author context must match the actual returned role session')
        data.setdefault('context', dict(conversation_id=state['conversation_id'], mode='fresh',
                                       reason='actual role handback from dispatch card ' + card['id']))
    for key in ('evidence', 'user_evidence'):
        if key in data:
            require(isinstance(data[key], list), key + ' must be an array')
            require(all(isinstance(p, str) and p for p in data[key]), key + ' needs nonempty file paths')
            data[key] = [str((root / p).resolve()) if not Path(p).is_absolute() else p for p in data[key]]
    if 'design' in data:
        require(isinstance(data['design'], dict) and isinstance(data['design'].get('path'), str), 'design needs a path')
        if not Path(data['design']['path']).is_absolute():
            data['design']['path'] = str((root / data['design']['path']).resolve())
    if 'evidence' in flow.OP_FIELDS[kind][0].split() or kind in {'classify_attempt', 'resume_attempt', 'repin_requirement'}:
        data['evidence'] = list(dict.fromkeys(data.get('evidence', []) + proof))
    return actor, op


def register(store, directory, card, state, apply_request):
    """Dry-run the complete sequence, then commit resumable, idempotent steps."""
    directory = Path(directory); root = Path(store).resolve().parent
    ledger = json.loads(Path(store).read_text())
    operations = read_operations(directory, card, state)
    require(operations, 'empty registration while the originating action is still pending')
    proof = [str(directory / 'result.md')]
    receipt = directory / ('run/receipt.json' if card['route'] == 'cli' else 'receipt.txt')
    require(receipt.is_file() and state.get('conversation_id'), 'missing actual role dispatch receipt/session')
    proof.append(str(receipt))
    source_hash = flow.digest(dict(operations=operations, proof={p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in proof}))
    previous = state.get('registration') or {}
    if previous and previous['source_hash'] != source_hash:
        committed = {h['request_id'] for h in ledger['history']}
        require(not any(r['request_id'] in committed for r in previous['requests']),
                'registration changed after partial commit; preserve history and request a correction')
    requests = []; draft = ledger
    with tempfile.TemporaryDirectory(prefix='batch-flow-registration-') as temporary:
        for index, operation in enumerate(operations):
            actor, op = normalize(operation, card, state, draft, proof, root)
            identifier = 'handback-' + card['id'] + '-' + source_hash[:16] + '-' + str(index)
            prior = next((h for h in draft['history'] if h['request_id'] == identifier), None)
            if prior:
                request = next((r for r in previous.get('requests', []) if r['request_id'] == identifier), None)
                require(request, 'committed registration has no saved original request')
            else:
                cached = next((r for r in previous.get('requests', []) if r['request_id'] == identifier), None)
                require(not cached or cached['expected_revision'] == draft['revision'],
                        'ledger changed during interrupted registration; explicitly rebase the remaining operations')
                request = dict(request_id=identifier, expected_revision=draft['revision'], actor=actor, operations=[op])
            # All steps, including later confirmations and budget gates, must
            # validate before the first real ledger mutation.
            draft, _ = flow.apply_request(draft, request, Path(temporary))
            requests.append(request)
    state['registration'] = dict(source_hash=source_hash, requests=requests, completed=False)
    flow.write_atomic(directory / 'state.json', state)
    for index, request in enumerate(requests):
        path = directory / ('registration-request-' + str(index) + '.json')
        flow.write_atomic(path, request)
        apply_request(path)
    state['registration']['completed'] = True
    flow.write_atomic(directory / 'state.json', state)
    return requests
