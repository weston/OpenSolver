import hashlib
import json
import re
from collections import defaultdict

import server as vsrv
from drill_spots import action_info, board_plan, merge_tree


STREETS = {0: 'Preflop', 3: 'Flop', 4: 'Turn', 5: 'River'}
CARD = re.compile(r'^[2-9TJQKA][cdhs]$')


def canonical(path):
    return ':'.join('?' if CARD.fullmatch(token) else token for token in path.split(':'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def tree_key(settings, street):
    setup = settings.get('analysis_setup')
    if setup is None and settings.get('source') == 'live' and settings.get('tree'):
        tree = merge_tree(settings['tree'], settings.get('merge_similar', True))
        board = board_plan(settings, tree)['board']
        setup, _ = vsrv.setup_from_body({**tree, 'board': board})
    if setup is not None:
        structure = [line.strip() for line in setup if line.split() and line.split()[0] in
                     ('set_pot', 'set_eff_stack', 'clear_lines', 'add_line', 'remove_line', 'force_line', 'set_rake')]
    else:
        structure = settings.get('analysis_lines') or settings.get('path') or ['batch', settings.get('batch')]
    return digest([street, structure])


def board_street(review):
    return len(''.join(review['node']['board'].split())) // 2


def review_identity(settings, session, review, key_cache):
    node = canonical(review['node']['id'])
    street = board_street(review) - node.split(':').count('?')
    key = (session, street)
    if key not in key_cache:
        key_cache[key] = tree_key(settings, street)
    board = ''.join(review['node']['board'].split())[:street * 2]
    simulation = review.get('analysis_simulation') or digest(review.get('simulation') or settings.get('path') or [session, board])
    stack = max(a + b for a, b in zip(review['stacks'], review['node']['pot']))
    tree = digest([key_cache[key], review['node']['pot'][2], stack, review['big_blind']])
    return tree, simulation, board, street, stack


def validate(body):
    sessions = body.get('sessions', [])
    simulations = body.get('simulations')
    if sessions is None or ('simulations' in body and simulations is None):
        raise vsrv.SolverError('invalid analysis selection')
    for values, maximum in ((sessions, 100), (simulations, 2000)):
        if values is not None and (not isinstance(values, list) or len(values) > maximum or
                                   any(not isinstance(value, str) or len(value) > 128 for value in values)):
            raise vsrv.SolverError('invalid analysis selection')
    if body.get('seat', 'both') not in ('both', 'OOP', 'IP') or body.get('baseline', 'played') not in ('played', 'original'):
        raise vsrv.SolverError('invalid analysis filter')
    for field in ('tree', 'node'):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 1024):
            raise vsrv.SolverError('invalid analysis selection')
    return list(dict.fromkeys(sessions))


class Totals:
    def __init__(self):
        self.weight = 0
        self.samples = 0
        self.decisions = 0
        self.actions = {}
        self.user = defaultdict(float)
        self.solver = defaultdict(float)
        self.volume = [0., 0.]
        self.pot_volume = [0., 0.]
        self.classes = {}

    def add(self, hand, expected, big_blind, weight, classes=True):
        self.weight += weight
        self.samples += 1
        for source, frequencies, target, index in ((hand, hand['chosen'], self.user, 0),
                                                  (expected, [a['frequency'] for a in expected['actions']], self.solver, 1)):
            for action, frequency in zip(source['actions'], frequencies):
                key = canonical(action['id'])
                self.actions[key] = {**action, 'id': key}
                target[key] += weight * frequency
                if action['kind'] in ('bet', 'raise'):
                    amount = weight * frequency * action['amount']
                    self.volume[index] += amount / big_blind
                    self.pot_volume[index] += amount / hand['pot']
        if classes:
            name = vsrv.combo_class(hand['combo'])
            group = self.classes.setdefault(name, Totals())
            group.add(hand, expected, big_blind, weight, classes=False)

    def view(self, include_classes=False):
        keys = sorted(self.actions)
        user = [self.user[key] / self.weight for key in keys]
        solver = [self.solver[key] / self.weight for key in keys]
        result = {'actions': [self.actions[key] for key in keys], 'user': user, 'solver': solver,
                  'samples': self.samples, 'decisions': self.decisions,
                  'gap': sum(abs(a - b) for a, b in zip(user, solver)) / 2,
                  'volume': [v / self.weight for v in self.volume],
                  'pot_volume': [v / self.weight for v in self.pot_volume]}
        if include_classes:
            result['classes'] = [{'name': name, **group.view()} for name, group in self.classes.items()]
        return result


def report(records, body):
    cache, groups, settings_by_tree = {}, {}, {}
    for session, number, settings, review in records():
        tree, simulation, board, street, stack = review_identity(settings, session, review, cache)
        group = groups.setdefault(tree, {'id': tree, 'name': settings.get('spot') or settings.get('batch') or 'Current tree',
                                        'street': street, 'pot': review['node']['pot'][2], 'stack': stack,
                                        'big_blind': review['big_blind'], 'simulations': {}, 'sessions': set(), 'decisions': 0})
        group['sessions'].add(session)
        group['decisions'] += 1
        sim = group['simulations'].setdefault(simulation, {'id': simulation, 'board': board, 'decisions': 0})
        sim['decisions'] += 1
        settings_by_tree[tree] = settings
    catalog = [{**g, 'sessions': len(g['sessions']), 'simulations': list(g['simulations'].values())} for g in groups.values()]
    tree = body.get('tree') or (catalog[0]['id'] if catalog else None)
    if tree not in groups:
        return {'groups': catalog, 'tree': None, 'nodes': [], 'detail': None}
    group = groups[tree]
    selected_sims = set(body.get('simulations', group['simulations']))
    nodes, totals = {}, {}
    selected_sessions, selected_hands, selected_sims_seen = set(), set(), set()
    seat = body.get('seat', 'both')
    baseline = body.get('baseline', 'played')

    def add_path(path):
        parts = canonical(path).split(':')
        inv, actor, street = [0, 0], 0, group['street']
        parent = None
        for length in range(2, len(parts) + 1):
            key = ':'.join(parts[:length])
            token = parts[length - 1]
            incoming = None
            if parent is not None:
                if token == '?':
                    actor, street = 0, 3 if street == 0 else street + 1
                else:
                    incoming = action_info({'id': parent, 'type': 'OOP_DEC' if actor == 0 else 'IP_DEC',
                                            'pot': [*inv, group['pot']]}, {'id': key}, group['stack'])
                    if token == 'c':
                        inv[actor] = max(inv)
                    elif token.startswith('b'):
                        inv[actor] = float(token[1:])
                    actor = 1 - actor
            nodes.setdefault(key, {'id': key, 'parent': parent, 'street': STREETS[street],
                                   'seat': ('OOP', 'IP')[actor], 'incoming': incoming, 'chance': token == '?'})
            parent = key

    settings = settings_by_tree[tree]
    if settings.get('analysis_lines'):
        for path in settings['analysis_lines']:
            if path.startswith('r:0'):
                add_path(path)
    elif settings.get('source') == 'live' and settings.get('tree'):
        cfg = vsrv.tree_config_from_body(merge_tree(settings['tree'], settings.get('merge_similar', True)))
        tb = vsrv.load_tree_builder()
        root = tb.Node(group['street'] - 3, (0, 0), tb.OOP, False, None,
                       removed=tuple(cfg.removed_lines), extra=tuple(cfg.extra_lines))
        pending = [('r:0', root)]
        while pending:
            path, current = pending.pop()
            add_path(path)
            if len(nodes) > tb.MAX_TREE_ACTIONS:
                raise vsrv.SolverError('tree is too large to analyze')
            if current.inv[current.to_act] < max(current.inv):
                add_path(path + ':f')
            for amount, child, aggressive in tb._children(cfg, current):
                target = path + (':b' + str(amount) if aggressive else ':c')
                if child and child.street > current.street:
                    target += ':?'
                add_path(target)
                if child:
                    pending.append((target, child))

    for session, number, settings, review in records():
        identity, simulation, _, _, _ = review_identity(settings, session, review, cache)
        if identity != tree or simulation not in selected_sims:
            continue
        add_path(review['node']['id'])
        for action in review['feedback'].get('original', review['feedback'])['hands'][0]['actions']:
            add_path(action['id'])
        if seat != 'both' and review['seat'] != seat:
            continue
        selected_sessions.add(session)
        selected_hands.add((session, number))
        selected_sims_seen.add(simulation)
        key = canonical(review['node']['id'])
        total = totals.setdefault(key, Totals())
        total.decisions += 1
        feedback = review['feedback']
        expected = feedback.get('original', feedback) if baseline == 'original' else feedback
        expected_hands = {hand['combo']: hand for hand in expected['hands']}
        hands = feedback['hands']
        mass = sum(hand.get('weight', 1) for hand in hands)
        for hand in hands:
            weight = hand.get('weight', 1) / mass
            if weight > 0:
                total.add(hand, expected_hands[hand['combo']], review['big_blind'], weight)
    for key, total in totals.items():
        nodes[key]['stats'] = total.view()
    selected_node = body.get('node')
    if selected_node not in totals:
        selected_node = max(totals, key=lambda key: totals[key].view()['gap'], default=None)
    detail = {'id': selected_node, **totals[selected_node].view(True)} if selected_node else None
    return {'groups': catalog, 'tree': tree, 'nodes': list(nodes.values()), 'detail': detail,
            'sessions': len(selected_sessions), 'hands': len(selected_hands), 'simulations': len(selected_sims_seen),
            'decisions': sum(total.decisions for total in totals.values()), 'big_blind': group['big_blind']}
