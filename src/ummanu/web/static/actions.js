
// The owner's actions: every form with a data-action posts a JSON body to that route, and shows
// the answer where the form is. The request id belongs to this browser and is kept until the
// operation answers success: a retry after a network failure repeats the same request, and the
// next comment is a new one.
function freshId() { return 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()); }
function keptId(key) {
  const name = 'ummanu.web.request.' + key;
  let id = sessionStorage.getItem(name);
  if (!id) { id = freshId(); sessionStorage.setItem(name, id); }
  return id;
}
function forgetId(key) { sessionStorage.removeItem('ummanu.web.request.' + key); }
async function postJson(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  let answer = null;
  try { answer = await response.json(); } catch (error) { answer = {error: {code: 'unreadable', message: 'the answer was not JSON (' + response.status + ')'}}; }
  return {ok: response.ok, status: response.status, answer: answer};
}
function tell(element, text, bad) { if (!element) return; element.textContent = text; element.className = 'feedback' + (bad ? ' bad' : ''); }
for (const form of document.querySelectorAll('form.act')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = form.querySelector('.feedback');
  const kind = form.dataset.kind;
  const key = form.dataset.key;
  const body = {request_id: keptId(key)};
  for (const field of form.elements) {
    if (!field.name) continue;
    if (field.type === 'checkbox') body[field.name] = field.checked; else body[field.name] = field.value;
  }
  if (kind === 'move' && !body.sprint_override) { delete body.sprint_override; delete body.sprint_override_reason; }
  if (kind === 'close' && !body.decisions.trim()) delete body.decisions;
  if (kind === 'close' && !window.confirm('Close this sprint? Its remaining cards and issues get the decisions stated, and the closeout is written.')) return;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  forgetId(key);
  const answer = result.answer;
  if (kind === 'comment') tell(out, (answer.saved === false ? 'already saved' : 'saved') + (answer.comment_id ? ' as ' + answer.comment_id : answer.event_id ? ' as ' + answer.event_id : ''), false);
  else if (kind === 'move') tell(out, 'moved (' + (answer.event_id || 'recorded') + '); the card\'s new state shows on the next page load', false);
  else if (kind === 'close') tell(out, 'closed; ' + ((answer.definition_of_done || {}).reason || ''), false);
  else tell(out, 'done', false);
  form.reset();
});
for (const form of document.querySelectorAll('form.pause')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = document.getElementById('pause-feedback');
  if (!window.confirm(form.dataset.confirm)) return;
  const body = {};
  for (const field of form.elements) if (field.name) body[field.name] = field.value;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  const answer = result.answer;
  tell(out, (answer.outcome || answer.kind || 'done') + (answer.state && answer.state.mode ? ' — ' + answer.state.mode : '') + '; reloading', false);
  window.setTimeout(() => window.location.reload(), 800);
});
