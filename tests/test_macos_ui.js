const check = (condition, message) => { if (!condition) throw Error(message); };
const wait = async (condition) => {
  const deadline = Date.now() + 30000;
  while (!condition()) {
    if (Date.now() > deadline) throw Error('Timed out: ' + condition);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
};
const $ = id => document.getElementById(id);
const tab = view => document.querySelector(`[data-view="${view}"]`).click();
await wait(() => $('drill-ahead').oninput);
check([...document.querySelectorAll('.view-tab')].map(b => b.textContent).join(',') === 'Tree,Browse,Drill,Jobs,Settings', 'missing app tabs');

tab('settings');
$('drill-ahead').value = '4';
$('drill-ahead').dispatchEvent(new Event('input', {bubbles: true}));
check(localStorage.getItem('ws-drill-ahead') === '4', 'settings could not be stored');
tab('study');
await wait(() => !$('btn-drill').classList.contains('hidden'));
check($('matrix').children.length === 169, 'the solved tree did not load into Browse');
document.querySelector('#matrix .cell').click();
await wait(() => document.querySelector('#side-hand .hand-all[aria-pressed=true]'));
check(!document.querySelector('#side-hand table.combos'), 'the intermediate class view is still present');
check(document.querySelectorAll('#side-hand .hand-combo-values').length > 0, 'combos are missing their equity and EV');
document.querySelector('#side-hand .hand-combo').click();
await wait(() => document.querySelector('#side-hand .hand-blockers[aria-busy=false]')?.dataset.combo !== 'all'
  && document.querySelectorAll('#side-hand .hand-combo[aria-pressed=true]').length === 1);
check(document.querySelector('.side-tabs .active').dataset.side === 'hand', 'selecting a combo left the Hand tab');
check(document.querySelectorAll('#side-hand .blk-stat').length === 3, 'the selected hand has no blocker summary');
check(document.querySelectorAll('#side-hand .hand-combo[aria-pressed=true]').length === 1, 'the selected combo is not highlighted');
const response = document.querySelector('#side-hand .blk-response');
check(response?.querySelector('thead th:nth-child(2)').textContent === 'Full range', 'opponent responses lost the full-range label');
check(response.querySelector('thead th:nth-child(3)').textContent.startsWith('Holding') && response.querySelectorAll('thead .pc').length === 2, 'opponent responses do not identify the held cards');
check(response.querySelectorAll('tbody tr:first-child .blk-response-bar').length === 2, 'opponent frequencies are not shown as separate comparisons');
for (const row of response.querySelectorAll('tbody tr')) {
  const frequencies = [...row.querySelectorAll('.blk-response-bar i')].map(bar => parseFloat(bar.style.width));
  const delta = Number(row.querySelector('.blk-response-delta').textContent.replace('−', '-'));
  check(Math.abs(delta - (frequencies[1] - frequencies[0])) < 0.51, 'opponent frequency delta does not match Holding minus Full range');
}
document.querySelector('.side-tabs [data-side=blockers]').click();
await wait(() => document.querySelector('#side-blockers .blk-cards'));
check(!document.querySelector('#side-blockers .blk-sum, #side-blockers .hand-combos, #side-blockers .blk-removed'), 'range blockers includes specific-hand details');
document.querySelector('.side-tabs [data-side=hand]').click();
await wait(() => document.querySelector('#side-hand .hand-blockers[aria-busy=false]'));
document.querySelector('.side-tabs [data-side=cats]').click();
await wait(() => document.querySelector('#side-cats .cats-share'));
check(document.querySelector('#side-cats thead').textContent.includes('Range %'), 'categories are missing the range percentage');
const category = name => document.querySelector(`#side-cats .cat-row[data-cat="${name}"]`);
const highlightedClasses = () => [...document.querySelectorAll('#matrix .cat-hl .lbl')].map(label => label.textContent).sort().join(',');
category('overpair').click();
category('ace_high').click();
check(highlightedClasses() === 'AA,AKo,AKs,KK', 'made categories do not combine with OR');
category('flush_draw').click();
check(highlightedClasses() === 'AKs', 'made categories and draws do not combine with AND');
category('no_draw').click();
check(highlightedClasses() === 'AA,AKo,AKs,KK', 'draw categories do not combine with OR');
category('no_draw').click();
category('ace_high').click();
check(highlightedClasses() === '' && $('matrix').classList.contains('catmode'), 'empty category intersections do not clear highlights');
$('matrix').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
check(!document.querySelector('#side-cats .cat-row.sel') && !$('matrix').classList.contains('catmode'), 'Escape does not clear category selections');
document.querySelector('#matrix .cell').click();
document.querySelector('.side-tabs [data-side=graphs]').click();
await wait(() => document.querySelector('#side-graphs svg'));
const chart = document.querySelector('#side-graphs svg');
check(Math.abs(chart.viewBox.baseVal.width - chart.getBoundingClientRect().width) < 2, 'the graph scales up its axes');
check(document.querySelector('.side-tabs [data-side=beta]').textContent === 'Experimental', 'the Beta tab was not renamed');
document.querySelector('[data-graph=mixing]').click();
const mixingOrder = document.querySelector('.mixing-order select');
mixingOrder.value = 'mixing';
mixingOrder.dispatchEvent(new Event('change', {bubbles: true}));
await wait(() => document.querySelector('.mixing-chart'));
const mixingChart = document.querySelector('.mixing-chart');
check(mixingChart.querySelector('.mixing-area-label').textContent === `Area: ${document.querySelector('.mixing-stats b').textContent}`, 'curve area and mean mixing disagree');
check(Math.abs(mixingChart.viewBox.baseVal.width - mixingChart.getBoundingClientRect().width) < 2, 'mixing chart scales up its axes');
mixingChart.dispatchEvent(new KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
const purest = parseFloat(document.querySelector('.mixing-score b').textContent);
mixingChart.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
const mostMixed = parseFloat(document.querySelector('.mixing-score b').textContent);
check(purest >= 0 && mostMixed <= 100 && mostMixed >= purest && mixingChart.getAttribute('aria-valuenow') === '100.0', 'mixing curve does not run from purest to most mixed on a 0–100% scale');
check(document.querySelectorAll('.mixing-hand .pc').length === 2 && document.querySelectorAll('.mixing-action').length >= 2, 'mixing inspection lost the combo or action frequencies');
const mixingSummary = document.querySelector('.mixing-stats').textContent;
for (const metric of ['eq', 'ev']) {
  mixingOrder.value = metric;
  mixingOrder.dispatchEvent(new Event('change', {bubbles: true}));
  await wait(() => document.querySelector('.mixing-chart')?.dataset.sort === metric);
  check(document.querySelector('.mixing-stats').textContent === mixingSummary, 'sorting changes mixing summary statistics');
  check(document.querySelector('.mixing-sort-value').textContent.startsWith(metric === 'eq' ? 'Equity' : 'EV'), 'inspected hand is missing its sorting value');
}
document.querySelector('[data-graph=eq]').click();
await wait(() => document.querySelector('#side-graphs .gr-stats'));
document.querySelector('[data-graph=mixing]').click();
await wait(() => document.querySelector('#side-graphs .mixing-chart'));
document.querySelector('.side-tabs [data-side=beta]').click();
await wait(() => $('side-beta').dataset.key && document.querySelector('#side-beta .rl-main'));
check(!document.querySelector('#side-beta .mixing-chart, [data-experiment]'), 'mixing remains in Experimental');
check(!document.querySelector('#side-beta .rl-main').classList.contains('hidden'), 'runout visualization no longer opens in Experimental');
document.querySelector('.side-tabs [data-side=hand]').click();
await wait(() => document.querySelector('#side-hand .hand-blockers[aria-busy=false]'));

const post = async (path, body) => {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  check(response.ok && !data.error, JSON.stringify(data));
  return data;
};
await post('/api/drill/start', {source: 'current', mode: 'node', seat: 'OOP', format: 'set', count: 5});
tab('drill');
await wait(() => !$('drill-submit').classList.contains('hidden') && !$('drill-submit').disabled);
check(document.querySelectorAll('#drill-actions .mix-slider').length === 5, 'five hands did not render');
check(document.querySelectorAll('#drill-actions .drill-action').length === 0, 'pure-action shortcut buttons still render');
check(document.querySelectorAll('#drill-hero .drill-card-back').length === 2, 'hero does not have one concealed hand');
check(document.querySelector('.villain-seat .dealer-button')?.textContent === 'BTN', 'IP is missing the button');
$('drill-submit').click();
await wait(() => !$('drill-continue').classList.contains('hidden'));
check(document.querySelectorAll('.feedback-hand').length === 5, 'drill results did not render');
for (const hand of document.querySelectorAll('.feedback-hand')) {
  check([...hand.querySelectorAll('.feedback-bar-row > span')].map(label => label.textContent).join(',') === 'Solver,You', 'drill comparison order changed');
}
const resultState = await (await fetch('/api/drill/state')).json();
check(document.querySelectorAll('#drill-feedback .feedback-browse').length === 5, 'each result needs a Browse hand button');
for (const index of [0, 1]) {
  const combo = resultState.hand.feedback.hands[index].combo;
  document.querySelectorAll('#drill-feedback .feedback-browse')[index].click();
  await wait(() => !$('view-study').classList.contains('hidden') && document.querySelector('#side-hand .hand-blockers[aria-busy=false]'));
  const selected = [...document.querySelectorAll('#side-hand .hand-combo[aria-pressed=true] .pc')].map(card => card.title).sort().join('');
  check(selected === [combo.slice(0, 2), combo.slice(2)].sort().join(''), `Browse selected ${selected} instead of ${combo} for result ${index}`);
  check(document.querySelectorAll('#side-hand .hand-combo[aria-pressed=true]').length === 1, 'Browse selected more than one combo');
  check(document.querySelectorAll('#side-hand .hand-head .pc').length === 2, 'Browse is missing the exact hand');
  check(document.querySelectorAll('#side-hand .hand-action-ev').length > 0, 'Browse is missing action EVs');
  tab('drill');
  await wait(() => !document.querySelector('#drill-feedback .feedback-browse').disabled);
  const afterBrowse = await (await fetch('/api/drill/state')).json();
  check(afterBrowse.token === resultState.token && afterBrowse.hand.phase === 'feedback', 'browsing advanced the drill');
}
$('drill-continue').click();
await wait(() => (!$('drill-submit').classList.contains('hidden') && !$('drill-submit').disabled)
  || (!$('drill-next').classList.contains('hidden') && !$('drill-next').disabled));
check($('drill-number').textContent === 'Hand 2', 'continuing results stopped at the hand-complete screen');
check($('drill-next').classList.contains('hidden'), 'an extra next-hand click is required');
$('drill-sessions').click();
await wait(() => document.querySelector('.session-table tbody input'));
check(!$('drill-sessions-panel').classList.contains('hidden'), 'Sessions did not open as a Drill section');
check($('drill-practice-panel').classList.contains('hidden'), 'Practice remained visible in Sessions');
document.querySelector('.session-toolbar .primary').click();
await wait(() => document.querySelector('.analysis-detail h3'));
check($('drill-analysis').getAttribute('aria-selected') === 'true', 'Analysis did not open as a Drill section');
check(document.querySelector('.analysis-positions [aria-pressed=true]').textContent === 'OOP', 'analysis did not select one position');
check(document.querySelectorAll('.analysis-node.measured').length > 0, 'session analysis did not render the drilled tree');
check(document.querySelector('.analysis-detail').textContent.includes('5 dealt hands'), 'session analysis lost the submitted hands');
check([...document.querySelectorAll('.analysis-comparison > span')].map(label => label.textContent).join(',') === 'Solver,You', 'analysis comparison order changed');
check(document.querySelectorAll('.analysis-category').length > 0, 'hand categories did not render');
for (const pair of document.querySelectorAll('.analysis-mini-bars, .analysis-category-bars')) {
  check([...pair.children].map(bar => bar.getAttribute('aria-label').split(' · ')[0]).join(',') === 'Solver,You', 'analysis bar identity or order changed');
}
const bars = document.querySelector('.analysis-category-bars').children;
for (let i = 0; i < 2; i++) {
  const segment = [...bars[i].children].find(segment => segment.getBoundingClientRect().width > 0);
  segment.dispatchEvent(new MouseEvent('mousemove', {bubbles: true, clientX: 100, clientY: 100}));
  check($('tip').textContent.includes(i === 0 ? 'Solver' : 'You'), 'category hover does not identify the player');
  check($('tip').parentElement === document.body, 'category tooltip was not in the analysis page');
}
document.querySelector('.analysis-category summary').click();
await wait(() => document.querySelector('.category-example'));
const playedBoard = resultState.hand.node.board.match(/[2-9TJQKA][shdc]/g).sort().join('');
for (const cards of document.querySelectorAll('.category-example-cards')) {
  const board = [...cards.querySelectorAll('.board .pc')].map(card => card.title).sort().join('');
  check(cards.querySelectorAll('.combo-cards .pc').length === 2 && board === playedBoard, 'category expansion lost the hole cards or board');
}
check([...document.querySelectorAll('.category-example .analysis-comparison > span')].slice(0, 2).map(label => label.textContent).join(',') === 'Solver,You', 'expanded hand comparison lost player order');
$('drill-sessions').click();
check($('tip').classList.contains('hidden'), 'switching sections left its tooltip visible');
await wait(() => document.querySelector('.session-table tbody input')?.checked);
$('drill-practice-tab').click();
check(!$('drill-practice-panel').classList.contains('hidden') && $('drill-number').textContent === 'Hand 2', 'switching sections changed the active hand');
$('drill-stop').click();
await wait(() => !$('drill-start').classList.contains('hidden') && !$('drill-start').disabled);
$('drill-answer_mode').value = 'play';
$('drill-answer_mode').dispatchEvent(new Event('change'));
check($('drill-frequency-options').classList.contains('hidden'), 'normal play still asks for hands per node');
await post('/api/drill/start', {source: 'current', mode: 'hand', answer_mode: 'play', seat: 'OOP', format: 'set', count: 5, merge_similar: false});
await wait(() => document.querySelector('.drill-play-action:not(:disabled)'));
check(!document.querySelector('#drill-actions .mix-slider'), 'normal play has frequency sliders');
check($('drill-submit').classList.contains('hidden'), 'normal play requires Submit');
const playHero = [...$('drill-hero').querySelectorAll('.pc')].map(card => card.title).sort().join('');
check(playHero.length === 4, 'normal play does not show the hero hand');
let played = 0;
for (; played < 12 && $('drill-next').classList.contains('hidden'); played++) {
  const button = [...document.querySelectorAll('.drill-play-action')].find(button => /^(Check|Call)$/.test(button.querySelector('strong').textContent));
  check(button && !button.disabled, 'no playable action');
  const action = button.dataset.action;
  button.click();
  await wait(() => !document.querySelector('.drill-play-action:disabled')
    && (!document.querySelector('.drill-play-action') || document.querySelector('.drill-play-action').dataset.action !== action));
  check($('drill-continue').classList.contains('hidden') && $('drill-feedback').classList.contains('hidden'), 'normal play stopped at frequency feedback');
  check([...$('drill-hero').querySelectorAll('.pc')].map(card => card.title).sort().join('') === playHero, 'normal play changed the hero cards');
}
check(played > 0 && !$('drill-next').classList.contains('hidden'), 'normal play did not finish the hand');
$('drill-sessions').click();
await wait(() => [...document.querySelectorAll('.session-table tbody tr')].some(row => row.textContent.includes('Normal play')));
[...document.querySelectorAll('.session-toolbar button')].find(button => button.textContent === 'Select matching').click();
document.querySelector('.session-toolbar .primary').click();
await wait(() => document.querySelector('.analysis-modes [data-mode=play]:not(:disabled)'));
document.querySelector('.analysis-modes [data-mode=play]').click();
await wait(() => document.querySelector('.analysis-modes [data-mode=play]').getAttribute('aria-pressed') === 'true'
  && /\d+ actions?\b/.test(document.querySelector('.analysis-detail')?.textContent));
check(!document.querySelector('.analysis-modes [data-mode=frequencies]').disabled, 'frequency sessions were lost from analysis');
$('drill-practice-tab').click();
$('drill-stop').click();
await wait(() => !$('drill-start').classList.contains('hidden') && !$('drill-start').disabled);
await post('/api/drill/start', {source: 'current', mode: 'hand', answer_mode: 'play', feedback_timing: 'action', seat: 'OOP', merge_similar: false});
await wait(() => document.querySelector('.drill-play-action:not(:disabled)'));
for (let decision = 0; decision < 12 && $('drill-number').textContent === 'Hand 1'; decision++) {
  const button = [...document.querySelectorAll('.drill-play-action')].find(button => /^(Check|Call)$/.test(button.querySelector('strong').textContent));
  check(button && !button.disabled, 'no playable action with feedback');
  button.click();
  await wait(() => !$('drill-continue').classList.contains('hidden') && !$('drill-continue').disabled);
  check(!$('drill-feedback').classList.contains('hidden'), 'action feedback did not appear');
  $('drill-continue').click();
  await wait(() => $('drill-continue').classList.contains('hidden')
    && (document.querySelector('.drill-play-action:not(:disabled)') || !$('drill-next').classList.contains('hidden')));
  check($('drill-next').classList.contains('hidden'), 'finishing action feedback requires a second Next hand click');
}
check($('drill-number').textContent === 'Hand 2', 'one Continue click did not start the next hand');
check(testErrors.length === 0, testErrors.join('\n'));
return 'native window runs frequency drills and normal play, preserves hero cards, and analyzes the modes separately';
