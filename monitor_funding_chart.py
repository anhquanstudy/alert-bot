import requests
import time
from datetime import datetime
from tabulate import tabulate
import plotext as plt
import sys
import os

# Constants
API_URL = "https://api.hyperliquid.xyz/info"
UPDATE_INTERVAL = 10  # Update every 10 seconds

def get_perp_dexs():
    """Retrieves the list of all perpetual DEXs."""
    try:
        response = requests.post(
            API_URL,
            json={"type": "perpDexs"},
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        # Silently fail or log if needed, mostly regular output handles errors
        return None

def fetch_market_data(dex_name=None):
    """Fetches meta and asset contexts for a given DEX name (None or 'Hyperliquid' for default)."""
    try:
        payload = {"type": "metaAndAssetCtxs"}
        if dex_name and dex_name != "Hyperliquid":
            payload["dex"] = dex_name

        response = requests.post(
            API_URL,
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return None

def find_asset_funding(data, asset_name):
    """Parses the API response to find the funding rate for the asset."""
    if not data:
        return None

    try:
        universe = data[0]['universe']
        asset_ctxs = data[1]

        for i, asset in enumerate(universe):
            if asset['name'] == asset_name:
                if i < len(asset_ctxs):
                    ctx = asset_ctxs[i]
                    if 'funding' in ctx:
                        return float(ctx['funding'])
                break
    except Exception as e:
        pass

    return None

def discover_all_assets():
    """Maps every base asset to all DEXs that list it."""
    print("Discovering all assets across all DEXs...")

    # 1. Get all DEXs
    perp_dexs = get_perp_dexs()
    if not perp_dexs:
        return None, None

    asset_map = {} # {base_asset_name: {dex_name: full_asset_name}}

    # 2. Process each DEX
    for dex in perp_dexs:
        # Default Hyperliquid is null in the list
        if dex is None:
            dex_name = "Hyperliquid"
            # Fetch HL meta directly since it's not in perpDexs list info
            hl_data = fetch_market_data()
            if hl_data:
                hl_universe = hl_data[0].get('universe', [])
                for a in hl_universe:
                    name = a['name']
                    if name not in asset_map: asset_map[name] = {}
                    asset_map[name][dex_name] = name
            continue

        dex_name = dex.get('name')
        asset_caps = dex.get('assetToStreamingOiCap', [])

        for asset_info in asset_caps:
            full_asset_name = asset_info[0]
            # Normalize name: "hyna:HYPE" -> "HYPE", "BTC" -> "BTC"
            base_name = full_asset_name.split(':')[-1] if ':' in full_asset_name else full_asset_name

            if base_name not in asset_map:
                asset_map[base_name] = {}
            asset_map[base_name][dex_name] = full_asset_name

    # Filter to only keep assets that exist on at least TWO DEXs
    shared_assets = {k: v for k, v in asset_map.items() if len(v) > 1}

    # Get unique list of all DEXs involved in shared assets
    involved_dexs = set()
    for mapping in shared_assets.values():
        involved_dexs.update(mapping.keys())

    return shared_assets, sorted(list(involved_dexs))

def main():
    shared_assets, relevant_dexs = discover_all_assets()

    if not shared_assets:
        print("No shared assets found across DEXs.")
        return

    print(f"Tracking {len(shared_assets)} shared assets across {len(relevant_dexs)} DEXs.")
    print("Monitored DEXs:", ", ".join(relevant_dexs))
    print("-" * 50)
    print("Initializing Terminal Monitor... (Please allow a moment for data collection)")

    # History storage: {asset_name: {'times': [], 'values': []}}
    history = {}

    try:
        while True:
            # 1. Fetch data for all relevant DEXs
            dex_data = {}
            for dex in relevant_dexs:
                data = fetch_market_data(dex)
                if data:
                    dex_data[dex] = data

            # 2. Extract funding rates and find spreads
            table_data = []
            timestamp = datetime.now()
            timestamp_str = timestamp.strftime('%H:%M:%S')
            current_deltas = [] # (asset_base, delta_value)

            for asset_base, mappings in shared_assets.items():
                asset_rates = []
                for dex_name, full_name in mappings.items():
                    if dex_name in dex_data:
                        rate = find_asset_funding(dex_data[dex_name], full_name)
                        if rate is not None:
                            asset_rates.append((dex_name, rate))

                if len(asset_rates) < 2:
                    continue

                # Find min and max funding rates
                min_dex, min_rate = min(asset_rates, key=lambda x: x[1])
                max_dex, max_rate = max(asset_rates, key=lambda x: x[1])

                delta = (max_rate - min_rate) * 100
                annualized_delta = delta * 365 * 24

                # Format for table
                table_data.append([
                    asset_base,
                    f"{min_dex}: {min_rate*100:.6f}%",
                    f"{max_dex}: {max_rate*100:.6f}%",
                    f"{delta:.6f}%",
                    f"{annualized_delta:.2f}%"
                ])
                current_deltas.append((asset_base, delta))

            # Sort currents by delta value (descending)
            current_deltas.sort(key=lambda x: x[1], reverse=True)
            table_data.sort(key=lambda x: float(x[3].replace('%','')), reverse=True)

            # Identify Top 5 assets for charting
            top_assets = [x[0] for x in current_deltas[:5]]

            # Update history
            for asset_base, delta in current_deltas:
                if asset_base not in history:
                    history[asset_base] = {'times': [], 'values': []}

                history[asset_base]['times'].append(timestamp_str)
                history[asset_base]['values'].append(delta)

                # Keep history reasonable (last 60 points = 10 minutes)
                if len(history[asset_base]['times']) > 60:
                     history[asset_base]['times'].pop(0)
                     history[asset_base]['values'].pop(0)

            # --- RENDER TO TERMINAL ---
            plt.clear_terminal()

            # 1. Print Header & Table
            print(f"[{timestamp_str}] Global Cross-DEX Funding Spreads (Top 10)")
            headers = ["Asset", "Best Long", "Best Short", "Delta", "APR"]
            print(tabulate(table_data[:10], headers=headers, tablefmt="simple"))
            print("-" * 50)

            # 2. Render Chart
            plt.clear_data()
            plt.clear_figure()

            plt.title("Top 5 Funding Spreads (Delta %)")
            plt.ylabel("Spread %")
            plt.xlabel("Time (Ticks)")
            plt.theme('dark')

            # Plot data using indices for x-axis to avoid date-parsing bugs
            for asset in top_assets:
                if asset in history:
                    data = history[asset]
                    if data['values']:
                        indices = list(range(len(data['values'])))
                        plt.plot(indices, data['values'], label=asset, marker='dot')

            # Set xticks to time strings for the first plotted asset
            if top_assets and top_assets[0] in history:
                times = history[top_assets[0]]['times']
                if times:
                    indices = list(range(len(times)))
                    # Show ~5 labels
                    step = max(1, len(indices) // 5)
                    plt.xticks(indices[::step], times[::step])

            plt.show()

            # Wait loop
            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping monitor.")
        sys.exit(0)

if __name__ == "__main__":
    main()
