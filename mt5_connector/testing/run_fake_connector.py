"""
Run the real connector.py against the fake MetaTrader5 module.

For local development and tests only. It can never reach a broker.

    python mt5_connector/testing/run_fake_connector.py --port 5001
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fake_mt5"))
sys.path.insert(1, os.path.dirname(HERE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--trade-mode", choices=["demo", "real"], default="demo",
                        help="account type the fake terminal reports")
    args = parser.parse_args()

    os.environ["MT5_CONNECTOR_PORT"] = str(args.port)
    os.environ["MT5_CONNECTOR_HOST"] = args.host

    import MetaTrader5 as mt5
    if not getattr(mt5, "IS_FAKE", False):
        sys.exit("Refusing to start: the real MetaTrader5 package was loaded instead of the fake.")
    mt5._reset(trade_mode=mt5.ACCOUNT_TRADE_MODE_REAL if args.trade_mode == "real" else mt5.ACCOUNT_TRADE_MODE_DEMO)

    import connector
    import uvicorn

    # Mirror connector.py's own startup, which auto-initializes the terminal.
    mt5.initialize()
    connector.mt5_initialized = True
    print(f"FAKE MT5 connector on http://{args.host}:{args.port}  trade_mode={args.trade_mode}  "
          f"demo_guard={'ON' if connector.REQUIRE_DEMO else 'OFF'}")
    uvicorn.run(connector.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
