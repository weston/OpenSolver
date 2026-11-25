import itertools
from decimal import Decimal

import server as vsrv
from jobs_api import jobs


RANKS = 'AKQJT98765432'
DECK = [r + s for r in RANKS for s in 'shdc']
STREETS = {0: 'Preflop', 3: 'Flop', 4: 'Turn', 5: 'River'}
MERGE_THRESHOLD = 5


def board_cards(board):
    return [board[i:i + 2] for i in range(0, len(board), 2)]


def valid_board(value):
    if not isinstance(value, str):
        raise vsrv.SolverError('select a board')
    value = ''.join(value.split())
    cards = board_cards(value)
    if len(cards) not in (3, 4, 5) or len(set(cards)) != len(cards) or any(c not in DECK for c in cards):
        raise vsrv.SolverError('select 3, 4, or 5 distinct cards')
    return value


def board_plan(body, tree):
    mode = body.get('boards', 'random')
    result = {'boards': mode, 'board': '', 'board_pool': None}
    if mode == 'random':
        return result
    if mode in ('fixed', 'selected'):
        selected = [tree.get('board', '')] if mode == 'fixed' else body.get('selected_boards')
        if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
            raise vsrv.SolverError('select between 1 and 100 boards')
        selected = list(dict.fromkeys(valid_board(b) for b in selected))
        if len({len(b) for b in selected}) != 1:
            raise vsrv.SolverError('select boards from the same street')
        return {**result, 'board': selected[0], 'board_pool': selected, 'selected_boards': selected}
    if mode != 'filtered':
        raise vsrv.SolverError('invalid board mode')
    filters = body.get('board_filters', {})
    if not isinstance(filters, dict):
        raise vsrv.SolverError('invalid board filters')
    ranks = []
    for name in ('high', 'middle', 'low'):
        value = filters.get(name, RANKS)
        if not isinstance(value, str) or not value or any(r not in RANKS for r in value):
            raise vsrv.SolverError('choose ranks for each card position')
        ranks.append(set(value))
    pairing = filters.get('pairing', 'both')
    suits = filters.get('suits', ['rainbow', 'two-tone', 'monotone'])
    if pairing not in ('both', 'paired', 'unpaired') or not isinstance(suits, list) or not suits or any(s not in ('rainbow', 'two-tone', 'monotone') for s in suits):
        raise vsrv.SolverError('choose board textures')
    pool = []
    for cards in itertools.combinations(DECK, 3):
        if any(c[0] not in allowed for c, allowed in zip(cards, ranks)):
            continue
        board = ''.join(cards)
        paired = jobs.texture_pairing(board) != 'unpaired'
        if pairing != 'both' and paired != (pairing == 'paired'):
            continue
        if jobs.texture_suits(board) in suits:
            pool.append(board)
    if not pool:
        raise vsrv.SolverError('no boards match these filters')
    normalized = {name: ''.join(r for r in RANKS if r in allowed) for name, allowed in zip(('high', 'middle', 'low'), ranks)}
    normalized.update(pairing=pairing, suits=list(dict.fromkeys(suits)))
    return {**result, 'board_pool': pool, 'board_filters': normalized}


def merge_tree(tree, enabled):
    if not isinstance(tree, dict):
        raise vsrv.SolverError('choose a tree first')
    if not isinstance(enabled, bool):
        raise vsrv.SolverError('merge similar sizes must be on or off')
    extra = vsrv.pio_extra_lines(tree)
    extra += [f'#MergeSimilarBets#{enabled}', f'#MergeSimilarBetsThreshold#{MERGE_THRESHOLD}']
    return {**tree, 'pio_extra': extra}


def similar_size_removals(node, actions):
    own = int(node['pot'][0 if node['type'] == 'OOP_DEC' else 1])
    bets = [(int(own + a['amount']), a['id']) for a in actions if a['kind'] in ('bet', 'raise')]
    options = sorted((total, {action}) for total, action in bets)
    tb = vsrv.load_tree_builder()
    kept = {total for total, _ in tb._merge_similar(options, Decimal(MERGE_THRESHOLD), int(sum(node['pot'])), own)}
    return [action for total, action in bets if total not in kept]


def action_info(node, child, effective_stack):
    token = child["id"].rsplit(":", 1)[-1]
    actor = 0 if node["type"] == "OOP_DEC" else 1
    own, facing = node["pot"][actor], max(node["pot"][:2])
    if token == "f":
        return {"id": child["id"], "kind": "fold", "label": "Fold", "amount": None}
    if token == "c":
        return {"id": child["id"], "kind": "call" if facing > own else "check",
                "label": "Call" if facing > own else "Check", "amount": facing - own or None}
    amount = float(token[1:])
    raising = facing > own
    fraction = (amount - facing) / (sum(node["pot"]) + facing - own) if raising else (amount - own) / sum(node["pot"])
    allin = amount == effective_stack
    return {"id": child["id"], "kind": "raise" if raising else "bet",
            "label": ("All-in " if allin else "Raise " if raising else "Bet ") + f"{100 * fraction:.0f}%",
            "amount": amount - own, "fraction": fraction, "allin": allin}


def node_preview(body):
    tree = merge_tree(body.get('tree'), body.get('merge_similar', True))
    plan = board_plan(body, tree)
    cfg = vsrv.tree_config_from_body({**tree, 'board': plan['board']})
    tb = vsrv.load_tree_builder()
    node = tb.Node(max(0, len(cfg.board or '') // 2 - 3), (0, 0), tb.OOP, False, None,
                   removed=tuple(cfg.removed_lines), extra=tuple(cfg.extra_lines))
    requested = vsrv.node_id_arg({'id': [str(body.get('node', 'r:0'))]})
    tokens = requested.split(':')[2:]
    if len(tokens) > 64:
        raise vsrv.SolverError('node path is too long')
    node_id = 'r:0'
    path = [{'id': node_id, 'label': STREETS[node.street + 3], 'street': node.street + 3}]

    def choices_at(current, prefix):
        parent = {'id': prefix, 'type': 'OOP_DEC' if current.to_act == 0 else 'IP_DEC',
                  'pot': [*current.inv, cfg.pot]}
        choices = []
        for total, child, aggressive in tb._children(cfg, current):
            token = 'b' + str(total) if aggressive else 'c'
            target = prefix + ':' + token
            action = action_info(parent, {'id': target}, cfg.stack)
            if child is not None and child.street > current.street:
                target += ':?'
            choices.append((token, child, {**action, 'id': target, 'terminal': child is None,
                                          'actor': 'OOP' if current.to_act == 0 else 'IP', 'street': current.street + 3}))
        return choices

    while tokens:
        token = tokens.pop(0)
        match = next((c for c in choices_at(node, node_id) if c[0] == token), None)
        if match is None or match[1] is None:
            raise vsrv.SolverError('choose a decision in this tree')
        _, child, action = match
        node_id += ':' + token
        if child.street > node.street:
            if not tokens or not (tokens[0] == '?' or tokens[0] in DECK):
                raise vsrv.SolverError('choose the next street')
            node_id += ':' + tokens.pop(0)
        path.append({**action, 'id': node_id})
        node = child
    return {'node': node_id, 'path': path, 'street': STREETS[node.street + 3],
            'actor': 'OOP' if node.to_act == 0 else 'IP', 'decision': True,
            'choices': [c[2] for c in choices_at(node, node_id)]}
