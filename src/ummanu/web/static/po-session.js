
// While a turn runs or a message is queued, poll this session's JSON. When the turn count, the last
// turn's state or the queue changes, read this same page again and swap its changing blocks in place.
// Nothing reloads: the composer, what is typed in it, its selection and its focus are never touched.
const SESSION = '__SESSION__';
const BLOCKS = __BLOCKS__;
const status = document.getElementById('po-status');
const draft = document.getElementById('po-text');
// Enter sends through the form's own submit path, Shift+Enter keeps the newline. A form goes out once:
// a refusal renders a fresh page, and an in-place update after the turn it started lets it send again.
const form = document.getElementById('po-send');
// The send button sits in the composer's control row, outside the form it submits through `form=`,
// so it is looked up by that association and not only inside the form.
const button = document.querySelector('button[form="po-send"]');
let submitted = false;
if (form) {
  form.addEventListener('submit', (event) => {
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
    if (button) button.disabled = true;
  });
}
if (form && draft) {
  draft.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (submitted || !draft.value.trim()) return;
    form.requestSubmit();
  });
}
function say(text) { if (status) status.textContent = text; }
// The polling baseline is what a page shows, carried by its #po-turn-state: the turn count, the last
// turn's state, the queue length and whether a turn runs. Null when the page does not carry it whole.
function shown(page) {
  const element = page.getElementById('po-turn-state');
  if (!element) return null;
  const data = element.dataset;
  const turns = Number(data.turns);
  const queued = Number(data.queued);
  if (data.turns === undefined || data.queued === undefined || data.last === undefined) return null;
  if (!Number.isInteger(turns) || !Number.isInteger(queued)) return null;
  if (data.running !== 'true' && data.running !== 'false') return null;
  return { turns: turns, last: data.last, queued: queued, running: data.running === 'true' };
}
// Read this page again and put its fresh blocks where the old ones are. Answers the baseline the
// swapped-in page shows, or the reason it could not, and then the page is left exactly as it was.
async function swapBlocks() {
  let fresh;
  try {
    const response = await fetch(window.location.pathname, { cache: 'no-store' });
    if (!response.ok) return { failed: String(response.status) };
    fresh = new DOMParser().parseFromString(await response.text(), 'text/html');
  } catch (error) { return { failed: (error && error.message) || 'network error' }; }
  const pairs = BLOCKS.map((id) => [document.getElementById(id), fresh.getElementById(id), id]);
  const missing = pairs.find(([here, there]) => !here || !there);
  if (missing) return { failed: 'the page has no #' + missing[2] };
  const state = shown(fresh);
  if (!state) return { failed: 'the page does not say its turn state' };
  for (const [here, there] of pairs) here.outerHTML = there.outerHTML;
  return { state: state };
}
function waitingText(running) {
  return running
    ? 'the turn is running; this page updates when it ends'
    : 'the message is queued; this page updates when its turn starts';
}
// The JSON is only the cheap change detector. Whether to keep polling, and against what, is decided by
// the page on screen alone: a turn that started between the JSON read and the page read is shown
// running by the page, and is followed to its end.
let seen = shown(document);
if (seen && (seen.running || seen.queued > 0)) {
  say(waitingText(seen.running));
  let busy = false;
  const timer = window.setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      let doc;
      try {
        const response = await fetch('/po/api/sessions/' + encodeURIComponent(SESSION) + '?cards=0', { cache: 'no-store' });
        if (!response.ok) { say('could not refresh (' + response.status + ')'); return; }
        doc = await response.json();
      } catch (error) { say('could not refresh (' + ((error && error.message) || 'network error') + ')'); return; }
      const changed = (doc.turns || []).length !== seen.turns
        || (doc.last_turn ? doc.last_turn.state : '') !== seen.last
        || (doc.queued || []).length !== seen.queued
        || Boolean(doc.running) !== seen.running;
      if (!changed) return;
      const swapped = await swapBlocks();
      // A failed read leaves the old baseline, so the next tick sees the same change and tries again.
      if (swapped.failed) { say('could not refresh the answer (' + swapped.failed + ')'); return; }
      seen = swapped.state;
      // The turn a send started is over or under way, so the composer sends the next message again.
      submitted = false;
      if (button) button.disabled = false;
      if (seen.running || seen.queued > 0) { say('the page is up to date; ' + waitingText(seen.running)); return; }
      window.clearInterval(timer);
      say('the answer arrived');
    } catch (error) {
      say('could not refresh the answer (' + ((error && error.message) || 'unexpected error') + ')');
    } finally {
      busy = false;
    }
  }, 3000);
}
