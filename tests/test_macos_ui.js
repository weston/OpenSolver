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
$('drill-submit').click();
await wait(() => !$('drill-continue').classList.contains('hidden'));
check(document.querySelectorAll('.feedback-hand').length === 5, 'drill results did not render');
check(testErrors.length === 0, testErrors.join('\n'));
return 'native window loads Browse, saves settings, and submits a five-hand drill';
