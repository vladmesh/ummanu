
// Reload every 30 s while nobody is typing, which is what keeps the bottom bar's numbers current
// on a page nobody touches. Every switch carrying data-refresh-toggle is the same switch -- the
// bar has one on every page, the dashboard keeps its own -- and the choice is this browser's and is
// remembered. The guard is the point and is never loosened: focus in a field, or any field holding
// typed content, cancels the reload, so a half-written /po message is never discarded by it -- and
// a password field counts, because the /po login page's token is typed into one and a tick that
// cleared it would be the same loss with none of the text on screen to retype from.
(() => {
  const boxes = Array.from(document.querySelectorAll('input[data-refresh-toggle]'));
  let on = true;
  try { on = localStorage.getItem('ummanu.web.refresh') !== 'off'; } catch (error) { on = true; }
  for (const box of boxes) {
    box.checked = on;
    box.addEventListener('change', () => {
      on = box.checked;
      for (const other of boxes) other.checked = on;
      try { localStorage.setItem('ummanu.web.refresh', on ? 'on' : 'off'); } catch (error) {}
    });
  }
  function reloadIfIdle() {
    const active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.tagName === 'SELECT')) return;
    for (const field of document.querySelectorAll('textarea, input[type=text], input[type=password], input[type=search], input[type=email], input[type=url], input[type=number], input:not([type])')) if (field.value) return;
    window.location.reload();
    return true;
  }
  // The one reload rule, published for anything else on the page that wants the bar read again
  // (the Codex reset button): it reloads, or answers false when somebody is typing.
  window.ummanuReloadWhenIdle = () => reloadIfIdle() === true;
  window.setInterval(() => {
    if (!on) return;
    reloadIfIdle();
  }, 30000);
})();
