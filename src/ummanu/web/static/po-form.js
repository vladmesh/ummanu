
// Narrow the model and effort selects to the chosen CLI. The page is served with every CLI's options in
// one optgroup each, so it still works with no script; the script keeps them aside and lists only the
// chosen CLI's, flat -- hiding options would leave the other CLIs' group labels showing in the list.
const cli = document.getElementById('po-cli');
const selects = ['po-model', 'po-effort'].map((id) => document.getElementById(id)).filter(Boolean);
const all = new Map(selects.map((select) => [select, [...select.querySelectorAll('option')]]));
function narrow() {
  for (const select of selects) {
    const current = select.value;
    const owned = all.get(select).filter((option) => option.dataset.cli === cli.value);
    select.replaceChildren(...owned);
    const kept = owned.find((option) => option.value === current);
    if (kept) kept.selected = true;
    else if (owned.length) owned[0].selected = true;
  }
}
if (cli) { cli.addEventListener('change', narrow); narrow(); }
