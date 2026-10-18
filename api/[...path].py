"""Route Vercel API requests through the app and durable Supabase storage."""

import os

os.environ["LEDGERLY_DATA_BACKEND"] = "supabase"

from server import Handler, init_db  # noqa: E402

init_db()


class handler(Handler):
    pass
