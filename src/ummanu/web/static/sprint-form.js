
// The only thing this does is narrow the issue list to the product that is selected. Every row is
// already on the page with the product that owns it, so a browser that does not run this shows the
// whole list rather than an empty one.
const product = document.getElementById('product');
const issues = document.getElementById('issues');
function narrow() {
  if (!product || !issues) return;
  for (const row of issues.querySelectorAll('label')) {
    const owned = !product.value || row.dataset.product === product.value;
    row.hidden = !owned;
    if (!owned) row.querySelector('input').checked = false;
  }
}
if (product) { product.addEventListener('change', narrow); narrow(); }
