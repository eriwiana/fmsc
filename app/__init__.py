"""Process-wide setup that must run before anything else imports.

Importing any `app.*` module runs this first, so the timezone is set before
Litestar, SQLAlchemy or the tests touch localtime.
"""

import os
import time

# Run the process in Asia/Jakarta so logs and localtime are WIB. Domain logic uses explicit
# aware UTC datetimes + Postgres now(), so this is cosmetic, not correctness.
os.environ.setdefault("TZ", "Asia/Jakarta")
time.tzset()
