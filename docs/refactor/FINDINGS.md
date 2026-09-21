# Findings log

Problems found while working, recorded so they are not lost. Each is fixed in the step listed, not before.

| # | Found in | Problem | Impact | Fix in |
|---|---|---|---|---|
| 1 | Step 1 | No `SECRET_KEY` means a new random signing key on every use | Every login token is rejected | **Fixed in step 4** |
| 2 | Step 1 | `alembic upgrade` fails on a fresh database and is ignored | Future migrations will never apply | Step 11 |
| 3 | Step 1 | Default users with hardcoded passwords are created at startup | Known credentials on every install | **Fixed in step 4**. The live system still has these accounts and passwords |
| 4 | Step 1 | Price sync uses the Windows-only MT5 package even when a connector is set, then logs success | Parquet sync silently does nothing on Linux | Steps 8 and 12 |
| 5 | Step 1 | Lint script has no ESLint config | Lint has never run | Step 12 |
| 6 | Step 2 | Backend sends the terminal path as a JSON body, connector reads a query parameter | The chosen terminal path is ignored | Step 8 |
| 7 | Step 2 | Connector `/symbol` omits `trade_stops_level` | Autopilot's minimum stop distance safeguard never runs | Step 9 |
| 8 | Step 2 | Connector `/positions` omits the stop loss | Nothing can see a position's stop | Step 8 |
| 9 | Step 2 | Sandbox parses connector candle times, unix seconds, as nanoseconds | AI analysis sees every candle in January 1970, so resampling and time-of-day logic are wrong | Step 10 |
| 10 | Step 2 | Autopilot classifies stop or target hits by searching the deal comment for "sl" or "tp" | Any comment containing those letters is misclassified | Step 10 |
| 11 | Step 2 | Connector `/positions` shows local time, `/history` shows UTC, and history windows use naive local time | Times disagree between endpoints and across machines | Step 8 |
| 12 | Step 2 | Connector uses `datetime.utcfromtimestamp`, deprecated in Python 3.12 and later | Will break on a future Python | Step 8 |
| 13 | Step 2 | Backend requirements have no upper bounds, pandas 3.0 was installed | Builds are not reproducible | Step 11 |
| 14 | Step 2, seen on the live demo | Connector returns the pre-trade quote as the execution price, never `result.price` from the broker. Opening reported 4372.71 against an actual fill of 4372.63, closing reported 4372.49 against 4372.40 | Recorded entry and exit prices are wrong, so slippage is invisible and per-trade profit attribution is off | Connector half done in step 3; recording requested against filled stays in step 8 |
| 15 | Step 4 | A user's role is read from their login token, not the database | Changing or removing someone's role takes effect only when their token expires, up to 15 minutes | Step 6 |
| 16 | Step 4 | Deactivating a user does not end their access | A deactivated user keeps working until the token expires, and may be able to refresh it | Step 6 |
| 17 | Step 4 | Scratch scripts and three older docs still show the published passwords | Misleading, and the passwords are live on the current system | Step 7 |
| 18 | Step 4 | Backend tests take about six minutes, mostly rebuilding the database and hashing passwords before every test | Slow feedback discourages running them | Step 13 |
