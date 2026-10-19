# Ledgerly

## Supabase setup

Create a Supabase project and provide its project URL and publishable (or legacy anon) key to the app server. For local development, add them to the root `.env` file:

```sh
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your-project-publishable-or-anon-key
ALPHAVANTAGE_API_KEY=your-market-data-api-key
npm run dev
```

The local Python server loads `.env` automatically and keeps existing shell environment variables as the override. See [`.env.example`](.env.example) for the supported settings. Local development keeps finance records in `budgeting.db`.

Before deploying, run [`supabase/schema.sql`](supabase/schema.sql) once in the Supabase SQL Editor. It creates the transaction, investment, and budget tables with row-level security policies tied to the signed-in Supabase user. Account passwords and finance records stay in Supabase; the Vercel function uses the user's access token and the publishable/anon key, and does not need a service-role key. If email confirmation is enabled in Supabase Auth, new users must confirm their email before signing in.

## Vercel deployment

This repository includes a Vercel Python Function at `api/[...path].py` for the API. Vercel serves the frontend files in `public/` from the site root. The linked Vercel project is recorded in `.vercel/project.json`; connect the repository to that project in Vercel and deploy it.

Set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `ALPHAVANTAGE_API_KEY` in the Vercel project's Development, Preview, and Production environments, then redeploy. The Supabase values are already configured for the linked project. Keep the Alpha Vantage key server-side; do not add a Supabase service-role key. Values from local `.env` files are not uploaded automatically.

Vercel builds use Python 3.12, pinned in `.python-version`. On this Mac, the bundled native Vercel CLI currently fails to start the Python runtime; run the JavaScript CLI with `VERCEL_CLI_USE_NATIVE_BINARY=0 vercel dev` to work around that local launcher issue. A local production build can be checked with `VERCEL_CLI_USE_NATIVE_BINARY=0 vercel build --yes --target production`.

Local SQLite records are separate from Supabase and are not uploaded or migrated by deployment. The investment form records stocks, ETFs, and bonds with purchase price and date. Stock and ETF prices refresh automatically when a signed-in user opens or returns to the app, at most once per 24 hours per browser; server-side quotes are cached for 24 hours. The investment list also shows the holding's total value at the stock's opening price on its purchase date when the provider has that date. Alpha Vantage's free daily history covers the latest 100 trading days; full history requires premium access. Configure `ALPHAVANTAGE_API_KEY` to enable quotes. The provider's free quote is end-of-day (25 requests per day); real-time or 15-minute delayed US quotes require a paid entitlement and `ALPHAVANTAGE_ENTITLEMENT=realtime` or `delayed`. Bond prices are entered manually.

Transactions accept one-time, weekly, monthly, yearly, or custom frequencies. Custom schedules are stored as a short description such as `Every 3 months`.


### Google AdSense

To show a responsive display ad in the signed-in dashboard, set `ADSENSE_CLIENT_ID` (for example, `ca-pub-1234567890123456`) and `ADSENSE_SLOT_ID` (the numeric ad unit ID) in local `.env` or the Vercel project environment. Ads remain disabled until both are configured. The site must also be added to and approved by AdSense.
