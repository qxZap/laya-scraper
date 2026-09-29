/*
 * The only script on the page: copy a command to the clipboard.
 *
 * The button says what happened rather than flashing an icon, and it goes back
 * to "Copy" after a moment so it can be used again. If the clipboard is not
 * available, which is the case over plain HTTP, the button says so instead of
 * failing silently.
 */
for (const button of document.querySelectorAll('.copy')) {
  button.addEventListener('click', async () => {
    const block = document.getElementById(button.dataset.copy);
    if (!block) return;
    try {
      await navigator.clipboard.writeText(block.innerText.trim());
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Press Ctrl C';
    }
    setTimeout(() => { button.textContent = 'Copy'; }, 2000);
  });
}
