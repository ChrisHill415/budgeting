// Vercel's plain HTML analytics integration; this app does not use Next.js.
if (window.location.protocol.startsWith('http') && !['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)) {
  window.va = window.va || function () { (window.vaq = window.vaq || []).push(arguments); };
  const analyticsScript = document.createElement('script');
  analyticsScript.defer = true;
  analyticsScript.src = '/_vercel/insights/script.js';
  analyticsScript.onerror = () => window.dispatchEvent(new Event('vercel-analytics-error'));
  document.head.append(analyticsScript);
}
