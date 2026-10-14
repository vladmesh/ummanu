
// The Codex reset button on the bar. Every click asks first; the request id is made for the click
// and kept only while no answer came back, so pressing again after a network failure repeats the
// same request -- which the operation and the provider both answer once -- and any answer ends it.
(() => {
  const button = document.querySelector('button[data-codex-reset]');
  if (!button) return;
  const out = document.getElementById('codex-reset-feedback');
  const KEY = 'ummanu.web.request.codex-reset';
  const WORDS = {reset: 'reset: the Codex limit is reset', already_redeemed: 'already redeemed: this request was spent before',
    nothing_to_reset: 'nothing to reset: the provider says your usage does not need a reset right now; the credit stays',
    no_credit: 'no credit left', refused: 'refused', error: 'error', unknown: 'unknown'};
  function say(text, bad) { if (!out) return; out.textContent = text; out.className = 'bar-feedback' + (bad ? ' bad' : ''); }
  function kept() { try { return sessionStorage.getItem(KEY); } catch (error) { return null; } }
  function keep(id) { try { if (id) sessionStorage.setItem(KEY, id); else sessionStorage.removeItem(KEY); } catch (error) {} }
  button.addEventListener('click', async () => {
    if (!window.confirm(button.dataset.confirm)) return;
    const id = kept() || ('web-codex-reset-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()));
    keep(id);
    button.disabled = true;
    say('sending...', false);
    let response = null, answer = null;
    try {
      response = await fetch('/api/providers/codex/reset-limit', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({request_id: id})});
    } catch (error) {
      button.disabled = false;
      say('network failure; press again to repeat the same request', true);
      return;
    }
    keep(null);
    try { answer = await response.json(); } catch (error) { answer = null; }
    if (!response.ok || !answer || answer.error) {
      button.disabled = false;
      const failure = answer && answer.error ? answer.error.code + ': ' + answer.error.message : 'the answer was not JSON (' + response.status + ')';
      say(failure, true);
      return;
    }
    const outcome = String(answer.outcome || '');
    const good = outcome === 'reset' || outcome === 'already_redeemed';
    say((WORDS[outcome] || outcome) + (answer.reason ? ' — ' + answer.reason : ''), !good);
    if (!good) { button.disabled = false; return; }
    // Read the bar again through the page's one reload rule: never while a field holds typed text,
    // and never on a page that answers a POST, which carries no reload at all.
    if (outcome === 'reset') window.setTimeout(() => {
      if (!(window.ummanuReloadWhenIdle && window.ummanuReloadWhenIdle())) say(WORDS.reset + '; reload to read the new limits', false);
    }, 1500);
  });
})();
