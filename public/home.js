// Report failed third-party scripts on the public page without requiring login.
window.addEventListener('error', event => {
  if (event.target?.matches?.('script[src*="/pagead/js/adsbygoogle.js"]')) {
    const message = document.querySelector('#ad-error');
    message.textContent = 'AdSense could not load. Check your connection or publisher setup.';
    message.hidden = false;
  }
}, true);
window.addEventListener('vercel-analytics-error', () => {
  const message = document.querySelector('#ad-error');
  message.textContent += ' Vercel Analytics could not load.';
  message.hidden = false;
});
