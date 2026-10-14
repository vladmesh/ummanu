
const REF = '__REF__';
const KEY = 'ummanu.web.cursor.' + REF;
const notice = document.getElementById('events-notice');
const list = document.getElementById('events');
const seen = new Set(Array.from(list.children).map((li) => li.dataset.eventId));
// The cursor is the client's, and it is the only thing that decides where watching resumes. A
// reload or a reconnection reads the one this browser stored; only a first visit falls back to the
// end of the tail the server rendered. The server keeps nothing.
let cursor = sessionStorage.getItem(KEY) || '__CURSOR__';
let polling = true;
function render(item) {
  if (seen.has(item.event_id)) return;
  seen.add(item.event_id);
  const li = document.createElement('li');
  li.dataset.eventId = item.event_id;
  const time = document.createElement('time');
  time.textContent = item.occurred_at || '';
  const kind = document.createElement('b');
  kind.textContent = item.kind || '';
  li.append(time, kind, ' ' + (item.reason || item.outcome || ''));
  list.append(li);
  const empty = document.getElementById('events-empty');
  if (empty) empty.remove();
}
async function tail() {
  if (!polling) return;
  const url = '/api/tasks/' + encodeURIComponent(REF) + '/events?cursor=' + encodeURIComponent(cursor);
  let response;
  try { response = await fetch(url); } catch (error) { notice.textContent = 'the page could not reach this service: ' + error; return; }
  if (response.status === 400) {
    // A cursor this installation will not honour is not silently reset to the beginning: watching
    // stops, the reason is shown, and starting again from the current tail is the reader's choice.
    polling = false;
    const refused = await response.json();
    notice.className = 'unavailable';
    notice.textContent = 'this browser\'s position in the history was refused (' + refused.error.message + '). Reload to start from the current tail.';
    sessionStorage.removeItem(KEY);
    return;
  }
  if (!response.ok) { notice.textContent = 'the history could not be read just now (' + response.status + '); retrying.'; return; }
  const page = await response.json();
  notice.className = page.source.state === 'available' ? '' : 'unavailable';
  notice.textContent = page.source.state === 'available' ? '' : 'could not read this card\'s history: ' + page.source.reason;
  page.items.forEach(render);
  cursor = page.next_cursor;
  sessionStorage.setItem(KEY, cursor);
}
tail();
setInterval(tail, 4000);

const reviewForm = document.getElementById('review-form');
const feedback = document.getElementById('feedback');
if (reviewForm) reviewForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const worker = document.getElementById('worker-run').value;
  const profile = document.getElementById('review-profile').value.trim();
  // Same rule as a start: the id belongs to this browser and is kept, so a repeated submission
  // and a reconnection are answered with the review that already exists.
  const key = 'ummanu.web.review.' + worker + '.' + profile;
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    sessionStorage.setItem(key, id);
  }
  feedback.textContent = 'starting the review...';
  feedback.className = '';
  const response = await fetch('/api/runs/review', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({request_id: id, profile: profile, worker_run_id: worker, ref: REF}),
  });
  const answer = await response.json();
  if (!response.ok) {
    feedback.className = 'bad';
    feedback.textContent = answer.error.code + ': ' + answer.error.message;
    return;
  }
  feedback.textContent = 'review run ' + answer.review.run.run_id + ' — ' + answer.review.state.value;
});
