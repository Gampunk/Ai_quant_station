# Local demo setup

The backend runs in WSL. The connector and MT5 run on Windows. Nothing here touches the live servers.

## 1. Let WSL reach Windows on localhost

Open `C:\Users\meetr\.wslconfig` in Notepad and add one line under `[wsl2]`:

```ini
[wsl2]
memory=5GB
processors=8
swap=8GB
networkingMode=mirrored
```

Save it. In PowerShell, restart WSL. This closes every WSL window, including VS Code's connection.

```powershell
wsl --shutdown
```

Reopen WSL and VS Code as before.

## 2. Install the connector on Windows, once

In PowerShell:

```powershell
py -3.14 -m venv "$env:USERPROFILE\mt5-demo-venv"
& "$env:USERPROFILE\mt5-demo-venv\Scripts\python.exe" -m pip install -r "\\wsl.localhost\Ubuntu-24.04\home\gampunk\dev\Ai_quant_station\mt5_connector\requirements.txt"
```

This runs the connector straight from the WSL repository, so it always uses the branch you have checked out.

## 3. Prepare MT5

1. Open MetaTrader 5 and log into the **demo** account.
2. Turn on **Algo Trading** in the toolbar. Orders are rejected without it.

## 4. Start the connector

In PowerShell, leave this window open:

```powershell
$env:MT5_CONNECTOR_PORT = "5001"
$env:MT5_CONNECTOR_HOST = "127.0.0.1"
Remove-Item Env:MT5_API_TOKEN, Env:MT5_REQUIRE_DEMO -ErrorAction SilentlyContinue
& "$env:USERPROFILE\mt5-demo-venv\Scripts\python.exe" "\\wsl.localhost\Ubuntu-24.04\home\gampunk\dev\Ai_quant_station\mt5_connector\connector.py"
```

Expect `Auto-Connected to MT5 Terminal`, your demo login, and `Demo-only trading guard: ON`.

The token is left unset on purpose. The connector's token check is broken until step 3, and it only listens on your own machine.

## 5. Check it from WSL

Read-only first:

```bash
cd ~/dev/Ai_quant_station
backend/.venv/bin/python scripts/demo_check.py
```

Then one real round trip on the demo account, 0.01 lot, opened and closed within seconds:

```bash
backend/.venv/bin/python scripts/demo_check.py --trade
```

If your broker names gold differently, add `--symbol GOLD` or whatever MT5 shows in Market Watch.

Stop the connector with Ctrl+C in its PowerShell window.

## Without Windows

The same connector code runs against a fake terminal in WSL:

```bash
mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001
```
