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
$('drill-continue').click();
await wait(() => (!$('drill-submit').classList.contains('hidden') && !$('drill-submit').disabled)
  || (!$('drill-next').classList.contains('hidden') && !$('drill-next').disabled));
check($('drill-number').textContent === 'Hand 2', 'continuing results stopped at the hand-complete screen');
check($('drill-next').classList.contains('hidden'), 'an extra next-hand click is required');
$('drill-sessions').click();
[...document.querySelectorAll('.session-tabs button')].find(b => b.textContent === 'Analysis').click();
await wait(() => document.querySelector('.analysis-detail h3'));
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
  check($('tip').closest('dialog[open]'), 'category tooltip is behind the dialog');
}
$('drill-sessions-dialog').close();
await wait(() => $('tip').parentElement === document.body);
check($('tip').classList.contains('hidden'), 'closing analysis left its tooltip visible');
check(testErrors.length === 0, testErrors.join('\n'));
return 'native window loads Browse, saves settings, submits a five-hand drill, continues to the next hand, and analyzes the session';
