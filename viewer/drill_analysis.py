import hashlib
import json
import re
from collections import defaultdict
from functools import lru_cache

import server as vsrv
from drill_spots import action_info, live_tree


STREETS = {0: 'Preflop', 3: 'Flop', 4: 'Turn', 5: 'River'}
CARD = re.compile(r'^[2-9TJQKA][cdhs]$')


def canonical(path):
    return ':'.join('?' if CARD.fullmatch(token) else token for token in path.split(':'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def board_street(review):
    return len(''.join(review['node']['board'].split())) // 2


def review_identity(settings, session, review):
    node = canonical(review['node']['id'])
    street = board_street(review) - node.split(':').count('?')
    board = ''.join(review['node']['board'].split())[:street * 2]
    simulation = review.get('analysis_simulation') or digest(review.get('simulation') or settings.get('path') or [session, board])
    stack = max(a + b for a, b in zip(review['stacks'], review['node']['pot']))
    tree = digest([street, float(review['node']['pot'][2]), float(stack), float(review['big_blind'])])
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
    if 'answer_mode' in body and body['answer_mode'] not in ('frequencies', 'play'):
        raise vsrv.SolverError('invalid analysis mode')
    for field in ('tree', 'node', 'category'):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 1024):
            raise vsrv.SolverError('invalid analysis selection')
    offset = body.get('offset', 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise vsrv.SolverError('invalid analysis page')
    return list(dict.fromkeys(sessions))


def category_reader(solver):
    @lru_cache(maxsize=128)
    def read(board):
        names = [line.split() for line in solver.cmd('show_category_names').splitlines()]
        rows = [line.split() for line in solver.cmd('show_categories ' + board).splitlines()]
        if len(rows) != 2 or any(len(row) != vsrv.NUM_COMBOS for row in rows):
            raise vsrv.SolverError('unexpected show_categories output')
        categories = {}
        for i, combo in enumerate(vsrv.hand_order(solver)):
            key = ''.join(sorted((combo[:2], combo[2:])))
            categories[key] = [(kind, labels[int(row[i])])
                               for kind, labels, row in zip(('made', 'draws'), names, rows) if int(row[i]) >= 0]
        return categories
    return read


def weighted_hands(review, baseline):
    feedback = review['feedback']
    expected = feedback.get('original', feedback) if baseline == 'original' else feedback
    expected_hands = {hand['combo']: hand for hand in expected['hands']}
    hands = feedback['hands']
    mass = sum(hand.get('weight', 1) for hand in hands)
    for hand in hands:
        weight = hand.get('weight', 1) / mass
        if weight > 0:
            yield hand, expected_hands[hand['combo']], weight


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

    def add(self, hand, expected, big_blind, weight):
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

    def view(self):
        keys = sorted(self.actions)
        user = [self.user[key] / self.weight for key in keys]
        solver = [self.solver[key] / self.weight for key in keys]
        result = {'actions': [self.actions[key] for key in keys], 'user': user, 'solver': solver,
                  'samples': self.samples, 'decisions': self.decisions,
                  'gap': sum(abs(a - b) for a, b in zip(user, solver)) / 2,
                  'volume': [v / self.weight for v in self.volume],
                  'pot_volume': [v / self.weight for v in self.pot_volume]}
        return result


def report(records, body, read_categories):
    groups, settings_by_tree = {}, {}
    for session, number, settings, review in records():
        tree, simulation, board, street, stack = review_identity(settings, session, review)
        group = groups.setdefault(tree, {'id': tree, 'name': settings.get('spot') or settings.get('batch') or 'Current tree',
                                        'street': street, 'pot': review['node']['pot'][2], 'stack': stack,
                                        'big_blind': review['big_blind'], 'simulations': {}, 'sessions': set(), 'decisions': 0,
                                        'answer_modes': {'frequencies': 0, 'play': 0}})
        group['sessions'].add(session)
        group['decisions'] += 1
        sim = group['simulations'].setdefault(simulation, {'id': simulation, 'board': board, 'decisions': 0})
        sim['decisions'] += 1
        if body.get('seat', 'both') in ('both', review['seat']) and ('simulations' not in body or simulation in body['simulations']):
            group['answer_modes'][review['answer_mode']] += len(review['feedback']['hands'])
        settings_by_tree.setdefault(tree, {})[session] = settings
    catalog = [{**g, 'sessions': len(g['sessions']), 'simulations': list(g['simulations'].values())} for g in groups.values()]
    tree = body.get('tree') or (catalog[0]['id'] if catalog else None)
    if tree not in groups:
        return {'groups': catalog, 'tree': None, 'nodes': [], 'detail': None}
    group = groups[tree]
    answer_mode = body.get('answer_mode') or ('frequencies' if group['answer_modes']['frequencies'] else 'play')
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

    seen_shapes = set()
    for settings in settings_by_tree[tree].values():
        sizes = {key: value for key, value in (settings.get('tree') or {}).items()
                 if key not in ('board', 'range_oop', 'range_ip')}
        shape = digest([settings.get('analysis_lines'), sizes, settings.get('merge_similar')])
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        if settings.get('analysis_lines'):
            for path in settings['analysis_lines']:
                if path.startswith('r:0'):
                    add_path(path)
        elif settings.get('source') == 'live' and settings.get('tree'):
            cfg = vsrv.tree_config_from_body(live_tree(settings))
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
        identity, simulation, _, _, _ = review_identity(settings, session, review)
        if identity != tree or simulation not in selected_sims:
            continue
        add_path(review['node']['id'])
        for action in review['feedback'].get('original', review['feedback'])['hands'][0]['actions']:
            add_path(action['id'])
        if (seat != 'both' and review['seat'] != seat) or review['answer_mode'] != answer_mode:
            continue
        selected_sessions.add(session)
        selected_hands.add((session, number))
        selected_sims_seen.add(simulation)
        key = canonical(review['node']['id'])
        total = totals.setdefault(key, Totals())
        total.decisions += 1
        for hand, expected, weight in weighted_hands(review, baseline):
            total.add(hand, expected, review['big_blind'], weight)
    for key, total in totals.items():
        nodes[key]['stats'] = total.view()
    selected_node = body.get('node')
    if selected_node not in totals:
        selected_node = max(totals, key=lambda key: totals[key].view()['gap'], default=None)
    detail = {'id': selected_node, **totals[selected_node].view()} if selected_node else None
    if detail:
        categories = {'made': {}, 'draws': {}}
        examples, example_count = [], 0
        offset = body.get('offset', 0)
        for session, number, settings, review in records():
            if review['answer_mode'] != answer_mode:
                continue
            if canonical(review['node']['id']) != selected_node or (seat != 'both' and review['seat'] != seat):
                continue
            identity, simulation, _, _, _ = review_identity(settings, session, review)
            if identity != tree or simulation not in selected_sims or board_street(review) < 3:
                continue
            labels = read_categories(''.join(review['node']['board'].split()))
            for hand, expected, weight in weighted_hands(review, baseline):
                combo = hand['combo']
                key = ''.join(sorted((combo[:2], combo[2:])))
                for kind, name in labels[key]:
                    category = categories[kind].setdefault(name, Totals())
                    category.add(hand, expected, review['big_blind'], weight)
                    if body.get('category') == kind + ':' + name:
                        if offset <= example_count < offset + 25:
                            comparison = Totals()
                            comparison.add(hand, expected, review['big_blind'], 1)
                            examples.append({'session': session, 'number': number, 'combo': combo,
                                             'board': review['node']['board'], **comparison.view()})
                        example_count += 1
        detail['categories'] = {kind: [{'name': name, **total.view()} for name, total in buckets.items()]
                                for kind, buckets in categories.items()}
        if 'category' in body:
            detail['examples'] = {'hands': examples, 'total': example_count, 'offset': offset}
    return {'groups': catalog, 'tree': tree, 'nodes': list(nodes.values()), 'detail': detail,
            'answer_mode': answer_mode, 'answer_modes': group['answer_modes'],
            'sessions': len(selected_sessions), 'hands': len(selected_hands), 'simulations': len(selected_sims_seen),
            'decisions': sum(total.decisions for total in totals.values()), 'big_blind': group['big_blind']}
