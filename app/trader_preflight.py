from __future__ import annotations

import asyncio
import os

from app.mexc_trade import MexcTradeClient


async def main() -> None:
    key = os.getenv("MEXC_API_KEY") or None
    secret = os.getenv("MEXC_API_SECRET") or None
    if not key or not secret:
        raise RuntimeError("MEXC_API_KEY and MEXC_API_SECRET are required for preflight")
    base = os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/")
    if base == "https://contract.mexc.com":
        base = "https://api.mexc.com"
    ws = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")
    client = MexcTradeClient(base, api_key=key, api_secret=secret, ws_url=ws)
    try:
        server_time = await client.ping()
        asset = await client.usdt_asset()
        mode = await client.position_mode()
        positions = await client.open_positions()
        await client.refresh_contract_specs()
        print("MEXC FUTURES API PREFLIGHT: PASS")
        print(f"REST base: {base}")
        print(f"Server time: {server_time}")
        print(f"USDT equity: {float(asset.get('equity') or 0):,.4f}")
        print(f"USDT available open: {float(asset.get('availableOpen') or 0):,.4f}")
        mode_label = 'hedge' if mode == 1 else 'one-way' if mode == 2 else 'unknown/unparseable'
        print(f"Position mode: {mode} ({mode_label})")
        print(f"Existing MEXC futures positions: {len(positions)}")
        if mode is not None and mode != 1:
            print("FAIL-SAFE: live trader requires hedge position mode (1).")
            raise SystemExit(2)
        print("Read/authentication checks passed. No order was placed by this preflight.")
        print("Order-placing permission still depends on the Futures permission enabled on this API key.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
