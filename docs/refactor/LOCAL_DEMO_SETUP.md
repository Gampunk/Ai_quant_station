# Local demo setup

Connect the backend in WSL to a real MT5 demo account running on Windows.

**This never touches the live servers.** Everything stays on your laptop. The connector only listens on your own machine, and it refuses to trade unless MT5 reports a demo account.

Work top to bottom. Every step says what you should see. If something differs, jump to Troubleshooting at the end.

---

## Before you start

- Finish checks 1 to 3 first. Step 1 below restarts WSL and closes VS Code.
- Stop anything still running in a WSL terminal, such as the fake connector, with Ctrl+C.
- Have your MT5 demo login ready.

To undo everything later, see Rollback at the end.

---

## Step 1. Let WSL reach Windows on localhost

WSL cannot currently see services running on Windows. This turns on mirrored networking, which makes them share `localhost`.

1. Open Notepad as your normal user and open this file:

   ```
   C:\Users\meetr\.wslconfig
   ```

2. Add one line under `[wsl2]`. The file should end up like this, with your existing values unchanged:

   ```ini
   [wsl2]
   memory=5GB
   processors=8
   swap=8GB
   networkingMode=mirrored
   ```

3. Save and close Notepad.

4. Open **PowerShell** and run:

   ```powershell
   wsl --shutdown
   ```

   Every WSL window closes, including VS Code's connection. This is expected.

5. Reopen your WSL terminal and VS Code as usual.

**Check it worked.** In WSL:

```bash
cat /proc/sys/kernel/hostname
```

In mirrored mode this prints your Windows machine name, `M-Raos-Laptop`, instead of a WSL-only name. A more direct check comes in step 5.

---

## Step 2. Install the connector on Windows, once

In **PowerShell**:

```powershell
py -3.14 -m venv "$env:USERPROFILE\mt5-demo-venv"
& "$env:USERPROFILE\mt5-demo-venv\Scripts\python.exe" -m pip install -r "\\wsl.localhost\Ubuntu-24.04\home\gampunk\dev\Ai_quant_station\mt5_connector\requirements.txt"
```

Expect it to finish with `Successfully installed MetaTrader5-5.0.6180 ...`.

This reads the connector straight out of your WSL repository, so it always runs the branch you have checked out. Nothing is copied.

---

## Step 3. Prepare MetaTrader 5

1. Open MetaTrader 5 and log into your **demo** account.
2. Confirm the title bar or the Navigator shows the demo account, not a live one.
3. Click **Algo Trading** in the toolbar so it is switched on. Orders are rejected without it.
4. Check what gold is called in **Market Watch**. It may be `XAUUSD`, `XAUUSD.m`, `GOLD` or similar. Write it down.
5. Leave MT5 open. The connector talks to this running terminal.

---

## Step 4. Start the connector

In **PowerShell**. Leave this window open for as long as you are testing.

```powershell
$env:MT5_CONNECTOR_PORT = "5001"
$env:MT5_CONNECTOR_HOST = "127.0.0.1"
Remove-Item Env:MT5_API_TOKEN, Env:MT5_REQUIRE_DEMO -ErrorAction SilentlyContinue
& "$env:USERPROFILE\mt5-demo-venv\Scripts\python.exe" "\\wsl.localhost\Ubuntu-24.04\home\gampunk\dev\Ai_quant_station\mt5_connector\connector.py"
```

Expect roughly this:

```
Auto-Connected to MT5 Terminal (Default)
   Account: 12345678 | Server: YourBroker-Demo

API Server starting on http://127.0.0.1:5001
Docs (Swagger UI): http://127.0.0.1:5001/docs
Demo-only trading guard: ON
```

Two lines matter. The account must be your demo one, and the guard must say **ON**.

The API token is deliberately left unset. The connector's token check is broken until step 3 of the refactor, and this connector only accepts connections from your own machine.

---

## Step 5. Check it from WSL, read-only

In **WSL**, at the repository root:

```bash
cd ~/dev/Ai_quant_station
backend/.venv/bin/python scripts/demo_check.py 2>&1 | tee ~/demo_check_readonly.log
```

Add your symbol if gold is not called `XAUUSD`:

```bash
backend/.venv/bin/python scripts/demo_check.py --symbol GOLD 2>&1 | tee ~/demo_check_readonly.log
```

Expect five PASS lines, your account number and broker, a live bid and ask, and three recent candles with sensible prices and today's dates. No trade is placed.

---

## Step 6. One real round trip on the demo account

This opens a 0.01 lot buy with a stop and target, moves the stop, closes the position, and prints both deals. It takes a few seconds and uses demo money.

```bash
backend/.venv/bin/python scripts/demo_check.py --trade 2>&1 | tee ~/demo_check_trade.log
```

Add `--symbol` again if you needed it in step 5.

Expect every line to say PASS, ending with `All stages passed`, and a small profit or loss of a few cents from the spread.

You can watch the position appear and disappear in the MT5 Toolbox under Trade and History.

---

## Step 7. Save the output

The two `tee` commands above already wrote:

```
~/demo_check_readonly.log
~/demo_check_trade.log
```

Send me both, plus what the PowerShell window printed when the connector started. If anything failed, send that too rather than retrying blindly.

---

## Step 8. Stop

- Ctrl+C in the PowerShell window running the connector.
- MT5 can stay open. You can switch Algo Trading off again if you prefer.

---

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| `connector reachable  FAIL` and a connection error | Mirrored networking is off, or the connector is not running | Recheck step 1, confirm the PowerShell window is still running |
| `py -3.14` not recognised | The Python launcher is missing or 3.14 is not installed | Run `py --list` to see installed versions and use one of those |
| `Auto-Connected` never appears, asks you to call /initialize | MT5 is closed, or several terminals are installed | Open MT5 and retry. For several terminals, set `$env:MT5_TERMINAL_PATH = "C:\Path\To\terminal64.exe"` before starting |
| `account is demo  FAIL` | MT5 is logged into a live account | Log into the demo account. Do not disable the guard |
| `quote for XAUUSD  FAIL` with 404 | Your broker names gold differently | Use `--symbol` with the Market Watch name |
| Order fails mentioning AutoTrading | Algo Trading is off | Switch it on in the MT5 toolbar |
| Order fails mentioning volume | The broker's minimum lot is above 0.01 | Send me the message, do not raise the size yourself |
| `ConnectorAddressBlocked` | The address is not local | You passed a `--url` that is not on your machine. Drop it and use the default |
| WSL feels broken, or a VPN stops working, after step 1 | Mirrored networking conflicts with some VPNs | See Rollback |

---

## Rollback

Remove the `networkingMode=mirrored` line from `C:\Users\meetr\.wslconfig`, then in PowerShell:

```powershell
wsl --shutdown
```

To remove the Windows install:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\mt5-demo-venv"
```

Nothing in the repository changes either way.

---

## No Windows, or just testing

The same connector code runs against a fake terminal entirely inside WSL, with no broker and no MT5:

```bash
mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001
```

Then run `demo_check.py` exactly as in steps 5 and 6.
