# Ledgerly

## Supabase account setup

Create a Supabase project and set these environment variables where the app server runs:

```sh
export SUPABASE_URL="https://your-project-ref.supabase.co"
export SUPABASE_ANON_KEY="your-project-anon-key"
npm run dev
```

The app uses Supabase Auth for account creation and password sign-in. If email confirmation is enabled in Supabase Auth, new users must confirm their email before signing in. The anon key is used only by the server for Auth requests; never put a service role key in browser code.

The existing SQLite database keeps the app's finance records and a link to each Supabase Auth user. Account passwords are handled by Supabase and are not stored in SQLite. Existing local accounts are linked to the matching Supabase account by email when they first sign in.

