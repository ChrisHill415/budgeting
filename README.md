# Budgeting
Track your income, savings, and spending.

The public front page is at `/`. Use the top-right Log in button to open the
existing sign-in and dashboard at `/app.html`.

AdSense is installed in both page heads for publisher `ca-pub-6779870512574535`.
Enable Auto ads for the deployed domain in AdSense under Ads → By site → Edit → Auto ads → Apply to site. Google must approve the site before it can serve ads.

For an additional responsive display ad at the bottom of the dashboard, set
`ADSENSE_CLIENT_ID=ca-pub-6779870512574535` and `ADSENSE_SLOT_ID` to the display ad unit ID on the server. The existing page script is reused.
