// Keep the board's own scroll across a reload: the browser restores only the document's. The
// position is this tab's, per filter, in sessionStorage; when that cannot be read the board opens
// at the top left.
(() => {
  const board = document.querySelector('.board');
  if (!board) return;
  const key = 'ummanu.web.board.scroll' + location.search;
  const lists = () => Array.from(board.querySelectorAll('.board-column')).map((column) => column.querySelector('ol.tiles'));
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (saved) {
      board.scrollLeft = Number(saved.left) || 0;
      lists().forEach((list, index) => { if (list) list.scrollTop = Number((saved.tops || [])[index]) || 0; });
    }
  } catch (error) {}
  window.addEventListener('pagehide', () => {
    try {
      sessionStorage.setItem(key, JSON.stringify({ left: board.scrollLeft, tops: lists().map((list) => (list ? list.scrollTop : 0)) }));
    } catch (error) {}
  });
})();
