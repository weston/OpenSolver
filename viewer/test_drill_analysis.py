import copy
import os
import unittest

from jobs_api import jobs
from drill_sessions import Sessions
from drill_spots import merge_tree
import server as vsrv


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solver = vsrv.Solver(os.path.join(vsrv.REPO, 'solver'))
        cls.addClassCleanup(jobs.close_solver, cls.solver)

    def setUp(self):
        self.sessions = Sessions(':memory:')
        self.addCleanup(self.sessions.db.close)
        self.addCleanup(self.sessions.temp.cleanup)
        self.settings = {'source': 'current', 'analysis_lines': ['r', 'r:0', 'r:0:c', 'r:0:b100', 'r:0:b100:f', 'r:0:b100:c']}

    def review(self, board='2c3d4h5s9c', node='r:0', simulation='one'):
        actions = [{'id': node + ':b100', 'kind': 'bet', 'label': 'Bet 50%', 'amount': 100, 'fraction': .5, 'frequency': .2},
                   {'id': node + ':c', 'kind': 'check', 'label': 'Check', 'amount': None, 'frequency': .8}]
        hands = [{'combo': 'AcAd', 'node': node, 'actions': actions, 'chosen': [1, 0], 'weight': .75, 'pot': 200, 'grade': 'frequency'},
                 {'combo': 'KcKd', 'node': node, 'actions': [{**actions[1], 'frequency': .2}, {**actions[0], 'frequency': .8}],
                  'chosen': [1, 0], 'weight': .25, 'pot': 200, 'grade': 'frequency'}]
        return {'seat': 'OOP', 'node': {'id': node, 'board': board, 'pot': [0, 0, 200]},
                'stacks': [300, 300], 'big_blind': 100, 'analysis_simulation': simulation, 'feedback': {'hands': hands}}

    def save(self, session, review, settings=None):
        self.sessions.start(session, settings or self.settings)
        self.sessions.record(session, 1, review, (1, 2, 0))

    def analyze(self, body):
        return self.sessions.analysis(body, self.solver)

    def test_dealt_weights_align_actions_and_determine_volume(self):
        self.save('first', self.review())
        report = self.analyze({'sessions': ['first']})
        detail = report['detail']
        i = next(i for i, a in enumerate(detail['actions']) if a['kind'] == 'bet')
        self.assertAlmostEqual(detail['user'][i], .75)
        self.assertAlmostEqual(detail['solver'][i], .35)
        self.assertAlmostEqual(detail['gap'], .4)
        self.assertAlmostEqual(detail['volume'][0], .75)
        self.assertAlmostEqual(detail['volume'][1], .35)
        self.assertAlmostEqual(detail['pot_volume'][0], .375)
        self.assertAlmostEqual(detail['pot_volume'][1], .175)
        self.assertEqual({row['name'] for row in detail['categories']['made']}, {'straight', 'overpair'})
        self.assertEqual(report['hands'], 1)
        self.assertEqual(report['decisions'], 1)
        self.assertIsNone(next(n for n in report['nodes'] if n['id'] == 'r:0:b100:f').get('stats'))

    def test_made_hands_and_draws_keep_their_own_weighted_comparisons(self):
        review = self.review('Qh7h2h')
        top_pair, overpair = review['feedback']['hands']
        second_pair = copy.deepcopy(top_pair)
        top_pair.update(combo='QdAh', weight=2)
        second_pair.update(combo='QsAd', weight=1, chosen=[0, 1])
        second_pair['actions'][0]['frequency'] = .8
        second_pair['actions'][1]['frequency'] = .2
        overpair['weight'] = 1
        review['feedback']['hands'].append(second_pair)
        self.save('first', review)

        detail = self.analyze({'sessions': ['first']})['detail']
        made = {row['name']: row for row in detail['categories']['made']}
        draws = {row['name']: row for row in detail['categories']['draws']}
        pair = made['top_pair']
        bet = next(i for i, action in enumerate(pair['actions']) if action['kind'] == 'bet')
        self.assertEqual(pair['samples'], 2)
        self.assertAlmostEqual(pair['user'][bet], 2 / 3)
        self.assertAlmostEqual(pair['solver'][bet], .4)
        self.assertAlmostEqual(pair['volume'][0], 2 / 3)
        self.assertEqual(draws['flush_draw']['samples'], 1)
        self.assertEqual(draws['flush_draw']['user'][bet], 1)
        self.assertAlmostEqual(draws['flush_draw']['solver'][bet], .2)
        self.assertEqual(detail['samples'], 3)
        self.assertEqual(sum(row['samples'] for row in made.values()), 3)
        self.assertEqual(sum(row['samples'] for row in draws.values()), 3)
        self.assertAlmostEqual(detail['volume'][0], .5)
        self.assertAlmostEqual(detail['volume'][1], .5)

    def test_same_hole_cards_use_each_sessions_board_for_categories(self):
        for session, board in [('first', 'Qh7h2c'), ('second', 'Kh7h2c')]:
            review = self.review(board, simulation=session)
            review['feedback']['hands'] = review['feedback']['hands'][:1]
            review['feedback']['hands'][0]['combo'] = 'AdQs'
            self.save(session, review)

        detail = self.analyze({'sessions': ['first', 'second']})['detail']
        self.assertEqual({row['name'] for row in detail['categories']['made']}, {'top_pair', 'ace_high'})
        self.assertEqual([row['samples'] for row in detail['categories']['made']], [1, 1])
        self.assertEqual(detail['samples'], 2)

    def test_boards_pool_but_different_trees_and_seats_do_not(self):
        self.save('first', self.review())
        other = self.review('TcJdQhKsAs', simulation='two')
        other['seat'] = 'IP'
        self.save('second', other)
        deeper = self.review()
        deeper['stacks'] = [600, 600]
        self.save('third', deeper)
        report = self.analyze({'sessions': ['first', 'second', 'third']})
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual(report['simulations'], 2)
        self.assertEqual(report['detail']['samples'], 4)
        report = self.analyze({'sessions': ['first', 'second'], 'seat': 'IP', 'simulations': ['two']})
        self.assertEqual(report['detail']['samples'], 2)
        self.assertEqual(report['sessions'], 1)
        self.assertEqual(self.analyze({'sessions': []})['nodes'], [])

    def test_sessions_with_different_sizes_pool_each_hands_own_solver_baseline(self):
        self.save('live', self.review(), {**self.settings, 'source': 'live'})
        other = self.review('TcJdQhKsAs', simulation='two')
        other['node']['pot'][2] = 200.0
        for hand in other['feedback']['hands']:
            for action in hand['actions']:
                if action['kind'] == 'bet':
                    action.update(id='r:0:b200', amount=200, fraction=1, label='Bet 100%')
        self.save('saved', other, {'source': 'saved', 'batch': 'prior-sims'})
        report = self.analyze({'sessions': ['live', 'saved']})
        self.assertEqual(len(report['groups']), 1)
        self.assertEqual(report['sessions'], 2)
        self.assertEqual(report['detail']['samples'], 4)
        self.assertEqual([a['id'] for a in report['detail']['actions']], ['r:0:b100', 'r:0:b200', 'r:0:c'])
        self.assertEqual(report['detail']['user'], [.375, .375, .25])
        self.assertAlmostEqual(report['detail']['volume'][0], 1.125)
        self.assertAlmostEqual(report['detail']['volume'][1], .525)

    def test_original_baseline_includes_removed_size_and_same_user_distribution(self):
        review = self.review()
        original = copy.deepcopy(review['feedback'])
        for hand in original['hands']:
            for action in hand['actions']:
                action['frequency'] = .1 if action['kind'] == 'bet' else .4
            hand['actions'].append({'id': 'r:0:b200', 'kind': 'bet', 'label': 'Bet 100%', 'amount': 200, 'fraction': 1, 'frequency': .5})
        review['feedback']['original'] = original
        self.save('first', review)
        report = self.analyze({'sessions': ['first'], 'baseline': 'original'})
        self.assertAlmostEqual(report['detail']['volume'][0], .75)
        self.assertAlmostEqual(report['detail']['volume'][1], 1.1)
        removed = next(i for i, a in enumerate(report['detail']['actions']) if a['id'] == 'r:0:b200')
        self.assertEqual(report['detail']['user'][removed], 0)
        self.assertEqual(report['detail']['solver'][removed], .5)
        for category in report['detail']['categories']['made']:
            index = next(i for i, action in enumerate(category['actions']) if action['id'] == 'r:0:b200')
            self.assertEqual(category['solver'][index], .5)
            self.assertEqual(category['user'][index], 0)

    def test_runouts_share_the_betting_line_without_saved_simulation_files(self):
        settings = {'source': 'current', 'analysis_lines': ['r:0', 'r:0:c:c:?:c', 'r:0:c:c:?:b100']}
        self.save('first', self.review('2c3d4hAs', 'r:0:c:c:As'), settings)
        self.save('second', self.review('2c3d4hKs', 'r:0:c:c:Ks', 'two'), settings)
        report = self.analyze({'sessions': ['first', 'second']})
        self.assertEqual(len(report['groups']), 1)
        self.assertEqual(report['detail']['id'], 'r:0:c:c:?')
        self.assertEqual(report['detail']['samples'], 4)
        self.assertEqual(next(n for n in report['nodes'] if n['id'] == report['detail']['id'])['street'], 'Turn')

    def test_existing_live_sessions_match_new_sessions_and_include_undrilled_branches(self):
        tree = {'range_oop': 'AA,KK', 'range_ip': 'AA,KK', 'board': '2c3d4h5s9c', 'pot': 200,
                'stack': 300, 'river_bet': '50', 'river_raise': '50'}
        legacy = {'source': 'live', 'tree': tree, 'boards': 'fixed'}
        setup, _ = vsrv.setup_from_body(merge_tree(tree, True))
        self.save('old', self.review(), legacy)
        self.save('new', self.review('TcJdQhKsAs', simulation='two'), {**legacy, 'analysis_setup': setup})
        report = self.analyze({'sessions': ['old', 'new']})
        self.assertEqual(len(report['groups']), 1)
        self.assertEqual(report['detail']['samples'], 4)
        self.assertTrue(any(n['id'].startswith('r:0:c:b') and 'stats' not in n for n in report['nodes']))


if __name__ == '__main__':
    unittest.main()
