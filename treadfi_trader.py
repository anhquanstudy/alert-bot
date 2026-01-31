#!/usr/bin/env python3
"""
TreadFi Trader - Funding Rate Arbitrage Trading Tool

Strategy: Delta Neutral Funding Arbitrage
- Look at TOP 1 and TOP 2 spreads (by delta)
- Only ALERT if Top 1 or Top 2 is a priority pair (both DEXs in xyz/km/hyna)
- LONG on DEX with lowest funding, SHORT on DEX with highest funding
- Same asset, same size = price neutral, earn funding spread

Usage:
    python treadfi_trader.py                    # Run monitor
    python treadfi_trader.py add <wallet>       # Add wallet to track
    python treadfi_trader.py remove <wallet>    # Remove wallet
    python treadfi_trader.py wallets            # List tracked wallets
"""

import requests
import time
from datetime import datetime
from tabulate import tabulate
import plotext as plt
import sys
import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# =============================================================================
# HEALTH CHECK SERVER (for Render deployment)
# =============================================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = json.dumps({
                'status': 'healthy',
                'service': 'treadfi-trader',
                'timestamp': datetime.now().isoformat()
            })
            self.wfile.write(response.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def start_health_server():
    """Start HTTP server for health checks."""
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"Health server running on port {port}")
    server.serve_forever()


# =============================================================================
# CONFIGURATION
# =============================================================================

API_URL = "https://api.hyperliquid.xyz/info"
UPDATE_INTERVAL = 60  # seconds (increased for Telegram to avoid spam)
CONFIG_FILE = "treadfi_config.json"

# Trading Parameters
PRIORITY_DEXS = ["xyz", "km", "hyna"]  # Only trade when both DEXs are in this list
DELTA_ENTER_THRESHOLD = 0.008  # Delta >= 0.008% to enter
DELTA_EXIT_THRESHOLD = 0.003   # Delta < 0.003% to exit
DELTA_DROP_ALERT_PERCENT = 50  # Alert when delta drops 50% from peak
TOP_N_CHECK = 2  # Only check top N assets for entry signals

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')  # Will be set when user sends /start


# =============================================================================
# TELEGRAM BOT
# =============================================================================

def send_telegram(message, parse_mode='HTML'):
    """Send message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': parse_mode
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def get_telegram_updates(offset=None):
    """Get updates from Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        return []

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {'timeout': 1}
        if offset:
            params['offset'] = offset
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get('result', [])
    except Exception:
        pass
    return []


def handle_telegram_commands():
    """Handle incoming Telegram commands."""
    global TELEGRAM_CHAT_ID

    updates = get_telegram_updates()
    for update in updates:
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = str(message.get('chat', {}).get('id', ''))

        if text == '/start':
            # Save chat ID
            TELEGRAM_CHAT_ID = chat_id
            os.environ['TELEGRAM_CHAT_ID'] = chat_id

            welcome = """🚀 <b>TreadFi Trader Bot</b>

Funding Rate Arbitrage Monitor đã kết nối!

<b>Commands:</b>
/status - Xem top spreads hiện tại
/signals - Xem active signals
/help - Hướng dẫn

Bot sẽ tự động gửi alerts khi có tín hiệu entry/exit."""
            send_telegram_to(chat_id, welcome)

        elif text == '/status' and chat_id:
            TELEGRAM_CHAT_ID = chat_id
            if latest_spreads:
                status_msg = format_telegram_status(latest_spreads[:5])
                send_telegram_to(chat_id, status_msg)
            else:
                send_telegram_to(chat_id, "⏳ Đang khởi động, vui lòng chờ...")

        elif text == '/signals' and chat_id:
            TELEGRAM_CHAT_ID = chat_id
            if active_signals:
                msg = "📊 <b>Active Signals:</b>\n\n"
                for asset, sig in active_signals.items():
                    msg += f"• <b>{asset}</b>: L:{sig['long_dex']} S:{sig['short_dex']}\n"
                    msg += f"  Entry: {sig['entry_delta']:.4f}%\n"
            else:
                msg = "📊 Không có active signals."
            send_telegram_to(chat_id, msg)

        elif text == '/help' and chat_id:
            TELEGRAM_CHAT_ID = chat_id
            help_msg = f"""📖 <b>TreadFi Trader Help</b>

<b>Strategy:</b> Delta Neutral Funding Arbitrage
- Monitor Top {TOP_N_CHECK} spreads
- Alert khi priority pair ({'/'.join(PRIORITY_DEXS)})
- LONG DEX có funding thấp nhất
- SHORT DEX có funding cao nhất

<b>Signals:</b>
🟢 ENTER - Delta ≥ {DELTA_ENTER_THRESHOLD}%
⚠️ EXIT - Delta < {DELTA_EXIT_THRESHOLD}%
🚨 FLIP - DEXs đảo chiều
📉 DROP - Delta giảm >{DELTA_DROP_ALERT_PERCENT}%

<b>Commands:</b>
/status - Top spreads
/signals - Active signals
/help - Help này"""
            send_telegram_to(chat_id, help_msg)

    # Return last update ID for offset
    if updates:
        return updates[-1]['update_id'] + 1
    return None


def send_telegram_to(chat_id, message, parse_mode='HTML'):
    """Send message to specific chat."""
    if not TELEGRAM_BOT_TOKEN:
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': parse_mode
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def format_telegram_status(top_spreads):
    """Format top spreads for Telegram message."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    msg = f"📊 <b>TreadFi Status</b> [{timestamp}]\n\n"

    for i, s in enumerate(top_spreads, 1):
        is_pri = s['is_priority']
        pri_mark = "⭐" if is_pri else ""

        if s['asset'] in active_signals:
            signal = "✅"
        elif i <= TOP_N_CHECK and is_pri and s['delta'] >= DELTA_ENTER_THRESHOLD:
            signal = "🟢"
        else:
            signal = ""

        msg += f"#{i} {pri_mark}<b>{s['asset']}</b> {signal}\n"
        msg += f"   L:{s['long_dex']} → S:{s['short_dex']}\n"
        msg += f"   Δ: {s['delta']:.4f}% | APR: {s['apr']:.0f}%\n\n"

    if active_signals:
        msg += f"<b>Active Signals:</b> {len(active_signals)}"

    return msg


def telegram_polling_loop():
    """Background thread for handling Telegram commands in real-time."""
    global TELEGRAM_CHAT_ID
    last_update_id = None

    print("Telegram polling started...")

    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(5)
                continue

            # Get updates with long polling
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {'timeout': 30}
            if last_update_id:
                params['offset'] = last_update_id

            response = requests.get(url, params=params, timeout=35)
            if response.status_code != 200:
                time.sleep(5)
                continue

            updates = response.json().get('result', [])

            for update in updates:
                last_update_id = update['update_id'] + 1
                message = update.get('message', {})
                text = message.get('text', '')
                chat_id = str(message.get('chat', {}).get('id', ''))

                if not chat_id:
                    continue

                # Update global chat ID
                TELEGRAM_CHAT_ID = chat_id

                if text == '/start':
                    welcome = """🚀 <b>TreadFi Trader Bot</b>

Funding Rate Arbitrage Monitor đã kết nối!

<b>Commands:</b>
/status - Xem top spreads hiện tại
/signals - Xem active signals
/help - Hướng dẫn

Bot sẽ tự động gửi alerts khi có tín hiệu entry/exit."""
                    send_telegram_to(chat_id, welcome)

                elif text == '/status':
                    if latest_spreads:
                        status_msg = format_telegram_status(latest_spreads[:5])
                        send_telegram_to(chat_id, status_msg)
                    else:
                        send_telegram_to(chat_id, "⏳ Đang khởi động, vui lòng chờ...")

                elif text == '/signals':
                    if active_signals:
                        msg = "📊 <b>Active Signals:</b>\n\n"
                        for asset, sig in active_signals.items():
                            msg += f"• <b>{asset}</b>: L:{sig['long_dex']} S:{sig['short_dex']}\n"
                            msg += f"  Entry: {sig['entry_delta']:.4f}%\n"
                        send_telegram_to(chat_id, msg)
                    else:
                        send_telegram_to(chat_id, "📊 Không có active signals.")

                elif text == '/help':
                    help_msg = f"""📖 <b>TreadFi Trader Help</b>

<b>Strategy:</b> Delta Neutral Funding Arbitrage
- Monitor Top {TOP_N_CHECK} spreads
- Alert khi priority pair ({'/'.join(PRIORITY_DEXS)})
- LONG DEX có funding thấp nhất
- SHORT DEX có funding cao nhất

<b>Signals:</b>
🟢 ENTER - Delta ≥ {DELTA_ENTER_THRESHOLD}%
⚠️ EXIT - Delta < {DELTA_EXIT_THRESHOLD}%
🚨 FLIP - DEXs đảo chiều
📉 DROP - Delta giảm >{DELTA_DROP_ALERT_PERCENT}%

<b>Commands:</b>
/status - Top spreads
/signals - Active signals
/help - Help này"""
                    send_telegram_to(chat_id, help_msg)

        except Exception as e:
            print(f"Telegram polling error: {e}")
            time.sleep(5)

# =============================================================================
# GLOBAL STATE
# =============================================================================

# Track active signals
active_signals = {}  # {asset: {'entry_delta', 'peak_delta', 'long_dex', 'short_dex', 'entry_rank'}}

# Tracked wallets
tracked_wallets = []

# Latest spreads for Telegram status
latest_spreads = []

# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================

def load_config():
    """Load configuration from file."""
    global tracked_wallets
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                tracked_wallets = config.get('wallets', [])
                print(f"Loaded {len(tracked_wallets)} tracked wallet(s)")
    except Exception as e:
        print(f"Could not load config: {e}")


def save_config():
    """Save configuration to file."""
    try:
        config = {'wallets': tracked_wallets}
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Could not save config: {e}")


def add_wallet(address):
    """Add a wallet address to track."""
    global tracked_wallets
    if not address.startswith('0x') or len(address) != 42:
        print(f"Invalid wallet address format: {address}")
        print("Address must be 42 characters starting with 0x")
        return False

    if address.lower() not in [w.lower() for w in tracked_wallets]:
        tracked_wallets.append(address)
        save_config()
        print(f"Added wallet: {address}")
        return True
    else:
        print(f"Wallet already tracked: {address}")
        return False


def remove_wallet(address):
    """Remove a wallet address from tracking."""
    global tracked_wallets
    original_count = len(tracked_wallets)
    tracked_wallets = [w for w in tracked_wallets if w.lower() != address.lower()]

    if len(tracked_wallets) < original_count:
        save_config()
        print(f"Removed wallet: {address}")
    else:
        print(f"Wallet not found: {address}")


# =============================================================================
# API FUNCTIONS
# =============================================================================

def get_perp_dexs():
    """Retrieves the list of all perpetual DEXs."""
    try:
        response = requests.post(
            API_URL,
            json={"type": "perpDexs"},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fetch_market_data(dex_name=None):
    """Fetches meta and asset contexts for a given DEX."""
    try:
        payload = {"type": "metaAndAssetCtxs"}
        if dex_name and dex_name != "Hyperliquid":
            payload["dex"] = dex_name

        response = requests.post(
            API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fetch_user_positions(wallet_address, dex_name=None):
    """Fetch user's open positions on a specific DEX."""
    try:
        payload = {
            "type": "clearinghouseState",
            "user": wallet_address
        }
        if dex_name and dex_name != "Hyperliquid":
            payload["dex"] = dex_name

        response = requests.post(
            API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception:
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
    except Exception:
        pass

    return None


# =============================================================================
# DISCOVERY
# =============================================================================

def discover_all_assets():
    """Maps every base asset to all DEXs that list it."""
    print("Discovering all assets across all DEXs...")

    perp_dexs = get_perp_dexs()
    if not perp_dexs:
        return None, None

    asset_map = {}  # {base_asset_name: {dex_name: full_asset_name}}

    for dex in perp_dexs:
        if dex is None:
            dex_name = "Hyperliquid"
            hl_data = fetch_market_data()
            if hl_data:
                hl_universe = hl_data[0].get('universe', [])
                for a in hl_universe:
                    name = a['name']
                    if name not in asset_map:
                        asset_map[name] = {}
                    asset_map[name][dex_name] = name
            continue

        dex_name = dex.get('name')
        asset_caps = dex.get('assetToStreamingOiCap', [])

        for asset_info in asset_caps:
            full_asset_name = asset_info[0]
            base_name = full_asset_name.split(':')[-1] if ':' in full_asset_name else full_asset_name

            if base_name not in asset_map:
                asset_map[base_name] = {}
            asset_map[base_name][dex_name] = full_asset_name

    shared_assets = {k: v for k, v in asset_map.items() if len(v) > 1}

    involved_dexs = set()
    for mapping in shared_assets.values():
        involved_dexs.update(mapping.keys())

    return shared_assets, sorted(list(involved_dexs))


# =============================================================================
# SIGNAL LOGIC
# =============================================================================

def is_priority_pair(long_dex, short_dex):
    """Check if both DEXs are in the priority list."""
    return long_dex in PRIORITY_DEXS and short_dex in PRIORITY_DEXS


def process_signals(all_spreads, shared_assets):
    """
    Process signals based on TOP N overall ranking.

    Logic:
    1. Look at Top 1 and Top 2 from ALL spreads (sorted by delta)
    2. If Top 1 or Top 2 is a priority pair → potential ENTER
    3. If neither is priority → NO ENTRY (even if lower ranks are priority)

    Returns: list of alerts
    """
    global active_signals
    alerts = []

    # Get current top N assets (overall ranking)
    top_n_assets = set(s['asset'] for s in all_spreads[:TOP_N_CHECK])

    # Process each spread
    for rank, spread in enumerate(all_spreads, 1):
        asset = spread['asset']
        delta = spread['delta']
        long_dex = spread['long_dex']
        short_dex = spread['short_dex']
        apr = spread['apr']
        is_priority = spread['is_priority']

        # Check if we have an active signal for this asset
        if asset in active_signals:
            sig = active_signals[asset]
            entry_delta = sig['entry_delta']
            peak_delta = sig['peak_delta']
            original_long = sig['long_dex']
            original_short = sig['short_dex']

            # Update peak
            if delta > peak_delta:
                active_signals[asset]['peak_delta'] = delta
                peak_delta = delta

            # Update current rank
            active_signals[asset]['current_rank'] = rank

            # Check for FLIP (DEXs swapped)
            if long_dex != original_long or short_dex != original_short:
                del active_signals[asset]
                alerts.append(
                    f"🚨🚨 {asset} FLIP! (Rank #{rank})\n"
                    f"   Spread reversed! Was: LONG {original_long} / SHORT {original_short}\n"
                    f"   Now: LONG {long_dex} / SHORT {short_dex}\n"
                    f"   → CLOSE BOTH POSITIONS IMMEDIATELY!"
                )
                continue

            # Check if no longer priority pair
            if not is_priority:
                del active_signals[asset]
                alerts.append(
                    f"🚨 {asset} NO LONGER PRIORITY (Rank #{rank})\n"
                    f"   Best spread now: {long_dex} → {short_dex}\n"
                    f"   {long_dex if long_dex not in PRIORITY_DEXS else short_dex} is not in priority list\n"
                    f"   → Consider closing position"
                )
                continue

            # Check exit threshold
            if delta < DELTA_EXIT_THRESHOLD:
                del active_signals[asset]
                alerts.append(
                    f"⚠️ {asset} EXIT (Rank #{rank})\n"
                    f"   Delta: {delta:.4f}% < threshold {DELTA_EXIT_THRESHOLD}%\n"
                    f"   → Consider closing: LONG {long_dex} + SHORT {short_dex}"
                )
                continue

            # Check significant drop from peak
            drop_pct = ((peak_delta - delta) / peak_delta) * 100 if peak_delta > 0 else 0
            if drop_pct >= DELTA_DROP_ALERT_PERCENT:
                alerts.append(
                    f"📉 {asset} DELTA DROP (Rank #{rank})\n"
                    f"   Peak: {peak_delta:.4f}% → Now: {delta:.4f}% (-{drop_pct:.0f}%)"
                )

        else:
            # No active signal - check for new entry
            # IMPORTANT: Only consider Top N overall ranking
            if asset in top_n_assets:
                if is_priority and delta >= DELTA_ENTER_THRESHOLD:
                    active_signals[asset] = {
                        'entry_delta': delta,
                        'peak_delta': delta,
                        'long_dex': long_dex,
                        'short_dex': short_dex,
                        'entry_rank': rank,
                        'current_rank': rank
                    }

                    # Get full names
                    long_full = shared_assets.get(asset, {}).get(long_dex, asset)
                    short_full = shared_assets.get(asset, {}).get(short_dex, asset)

                    alerts.append(
                        f"🟢🟢 {asset} ENTRY SIGNAL (Rank #{rank})\n"
                        f"   Delta: {delta:.4f}% | APR: {apr:.0f}%\n"
                        f"   ┌─ LONG  {long_full} on {long_dex} (funding: {spread['long_rate']*100:.4f}%)\n"
                        f"   └─ SHORT {short_full} on {short_dex} (funding: {spread['short_rate']*100:.4f}%)\n"
                        f"   → Open SAME SIZE on both DEXs for delta neutral"
                    )

    return alerts


# =============================================================================
# POSITION TRACKING
# =============================================================================

def get_all_user_positions():
    """Fetch positions for all tracked wallets across priority DEXs."""
    all_positions = []

    for wallet in tracked_wallets:
        wallet_short = f"{wallet[:6]}...{wallet[-4:]}"

        for dex in PRIORITY_DEXS:
            positions_data = fetch_user_positions(wallet, dex)
            if not positions_data:
                continue

            asset_positions = positions_data.get('assetPositions', [])

            for pos_wrapper in asset_positions:
                pos = pos_wrapper.get('position', {})
                if not pos:
                    continue

                coin = pos.get('coin', '')
                szi = float(pos.get('szi', 0))

                if szi == 0:
                    continue

                base_coin = coin.split(':')[-1] if ':' in coin else coin
                entry_px = float(pos.get('entryPx', 0))
                position_value = float(pos.get('positionValue', 0))
                unrealized_pnl = float(pos.get('unrealizedPnl', 0))

                cum_funding = pos.get('cumFunding', {})
                funding_since_open = float(cum_funding.get('sinceOpen', 0))

                leverage_info = pos.get('leverage', {})
                leverage_value = leverage_info.get('value', 1)

                direction = "LONG" if szi > 0 else "SHORT"

                all_positions.append({
                    'wallet': wallet_short,
                    'wallet_full': wallet,
                    'dex': dex,
                    'asset': base_coin,
                    'full_coin': coin,
                    'direction': direction,
                    'size': abs(szi),
                    'entry_px': entry_px,
                    'position_value': position_value,
                    'unrealized_pnl': unrealized_pnl,
                    'funding_since_open': funding_since_open,
                    'leverage': f"{leverage_value}x"
                })

    return all_positions


def analyze_positions(positions, spread_data):
    """Analyze user positions against current spreads."""
    alerts = []
    analysis = []

    for pos in positions:
        asset = pos['asset']
        spread = spread_data.get(asset)

        pos_copy = pos.copy()

        if spread:
            best_long_dex = spread['long_dex']
            best_short_dex = spread['short_dex']
            delta = spread['delta']

            # Determine if position is on correct DEX
            if pos['direction'] == 'LONG':
                if pos['dex'] == best_long_dex:
                    status = "✅ CORRECT"
                elif pos['dex'] == best_short_dex:
                    status = "🚨 WRONG DEX"
                    alerts.append(
                        f"🚨 {asset}: LONG on {pos['dex']} but best long is {best_long_dex}!"
                    )
                else:
                    status = "⚠️ CHECK"
            else:  # SHORT
                if pos['dex'] == best_short_dex:
                    status = "✅ CORRECT"
                elif pos['dex'] == best_long_dex:
                    status = "🚨 WRONG DEX"
                    alerts.append(
                        f"🚨 {asset}: SHORT on {pos['dex']} but best short is {best_short_dex}!"
                    )
                else:
                    status = "⚠️ CHECK"

            pos_copy['status'] = status
            pos_copy['current_delta'] = delta
            pos_copy['best_long'] = best_long_dex
            pos_copy['best_short'] = best_short_dex
        else:
            pos_copy['status'] = "❓ NO DATA"
            pos_copy['current_delta'] = None
            pos_copy['best_long'] = "-"
            pos_copy['best_short'] = "-"

        analysis.append(pos_copy)

    return analysis, alerts


# =============================================================================
# DISPLAY FUNCTIONS
# =============================================================================

def print_alerts(alerts):
    """Print alert messages and send to Telegram."""
    if not alerts:
        return

    print("\n" + "=" * 70)
    print("🔔 ALERTS")
    print("=" * 70)
    for alert in alerts:
        print(alert)
        print()
    print("=" * 70)

    # Terminal bell for urgent
    if any("🚨" in a or "🟢" in a for a in alerts):
        print("\a")

    # Send to Telegram
    if TELEGRAM_CHAT_ID:
        for alert in alerts:
            # Convert to HTML format for Telegram
            tg_alert = alert.replace('🟢🟢', '🟢').replace('🚨🚨', '🚨')
            send_telegram(f"🔔 <b>ALERT</b>\n\n{tg_alert}")


def print_positions(analysis):
    """Print analyzed positions."""
    if not analysis:
        return

    print(f"\n💼 YOUR POSITIONS ({len(analysis)})")

    table_data = []
    total_pnl = 0
    total_funding = 0

    for pos in analysis:
        pnl = pos['unrealized_pnl']
        funding = pos['funding_since_open']
        total_pnl += pnl
        total_funding += funding

        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"${pnl:.2f}"
        funding_str = f"+${funding:.2f}" if funding >= 0 else f"${funding:.2f}"
        delta_str = f"{pos['current_delta']:.4f}%" if pos['current_delta'] else "-"

        table_data.append([
            pos['asset'],
            pos['dex'],
            pos['direction'],
            f"${pos['position_value']:.0f}",
            pnl_str,
            funding_str,
            pos['status'],
            delta_str
        ])

    headers = ["Asset", "DEX", "Side", "Value", "PnL", "Funding", "Status", "Spread"]
    print(tabulate(table_data, headers=headers, tablefmt="simple"))

    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"${total_pnl:.2f}"
    fund_str = f"+${total_funding:.2f}" if total_funding >= 0 else f"${total_funding:.2f}"
    print(f"  TOTAL: PnL {pnl_str} | Funding {fund_str}")


def print_active_signals(all_spreads):
    """Print active signals with current status."""
    if not active_signals:
        return

    print(f"\n📊 ACTIVE SIGNALS ({len(active_signals)})")

    # Build spread lookup
    spread_lookup = {s['asset']: s for s in all_spreads}

    table_data = []
    for asset, sig in sorted(active_signals.items(), key=lambda x: x[1].get('current_rank', 999)):
        spread = spread_lookup.get(asset, {})
        curr_delta = spread.get('delta', 0)
        curr_rank = sig.get('current_rank', '?')
        entry = sig['entry_delta']
        change = curr_delta - entry
        change_pct = (change / entry * 100) if entry > 0 else 0

        sign = "+" if change >= 0 else ""
        status = "✅" if change >= 0 else "📉"

        table_data.append([
            f"#{curr_rank}",
            asset,
            f"L:{sig['long_dex']} S:{sig['short_dex']}",
            f"{entry:.4f}%",
            f"{curr_delta:.4f}%",
            f"{sign}{change:.4f}%",
            status
        ])

    headers = ["Rank", "Asset", "Position", "Entry Δ", "Now Δ", "Change", ""]
    print(tabulate(table_data, headers=headers, tablefmt="simple"))


# =============================================================================
# MAIN
# =============================================================================

def print_help():
    print(f"""
TreadFi Trader - Delta Neutral Funding Arbitrage
=================================================

STRATEGY:
    1. Look at TOP {TOP_N_CHECK} spreads (by delta) from ALL pairs
    2. If Top 1 or Top 2 is a PRIORITY PAIR → ALERT
    3. Priority pair = both DEXs are in {PRIORITY_DEXS}

EXAMPLE:
    Top 10 overall:
    #1 TSLA  km → cash   0.054%  ❌ (cash not priority)
    #2 INTC  xyz → cash  0.051%  ❌ (cash not priority)
    ...
    #6 GOOGL km → xyz    0.028%  (priority but not top 2 → no alert)

    If GOOGL becomes #1 or #2 → 🟢 ALERT!

USAGE:
    python treadfi_trader.py              Run monitor
    python treadfi_trader.py add <addr>   Add wallet to track
    python treadfi_trader.py remove <addr> Remove wallet
    python treadfi_trader.py wallets      List wallets

SIGNALS:
    🟢 ENTER  Top 1-2 overall AND priority pair AND Delta >= {DELTA_ENTER_THRESHOLD}%
    ✅ HOLD   Active position, spread still good
    📉 DROP   Delta dropped >{DELTA_DROP_ALERT_PERCENT}% from peak
    ⚠️ EXIT   Delta < {DELTA_EXIT_THRESHOLD}%
    🚨 FLIP   DEXs swapped - close immediately!

CONFIG:
    Priority DEXs: {', '.join(PRIORITY_DEXS)}
    Entry: Top {TOP_N_CHECK} + Priority + Delta >= {DELTA_ENTER_THRESHOLD}%
    Exit: Delta < {DELTA_EXIT_THRESHOLD}%
""")


def main():
    load_config()

    # Start health server in background (for Render deployment)
    if os.environ.get('PORT'):
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()

    # Start Telegram polling in background
    if TELEGRAM_BOT_TOKEN:
        telegram_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
        telegram_thread.start()

    # Handle CLI commands
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()

        if cmd == "add" and len(sys.argv) > 2:
            add_wallet(sys.argv[2])
            return
        elif cmd == "remove" and len(sys.argv) > 2:
            remove_wallet(sys.argv[2])
            return
        elif cmd == "wallets":
            if tracked_wallets:
                print("Tracked wallets:")
                for w in tracked_wallets:
                    print(f"  {w}")
            else:
                print("No wallets tracked.")
                print("Add: python treadfi_trader.py add <wallet>")
            return
        elif cmd in ["help", "-h", "--help"]:
            print_help()
            return
        else:
            print(f"Unknown command: {cmd}")
            print("Use: python treadfi_trader.py help")
            return

    # Discover assets
    shared_assets, relevant_dexs = discover_all_assets()

    if not shared_assets:
        print("No shared assets found.")
        return

    print(f"Found {len(shared_assets)} cross-DEX assets")
    print(f"Priority DEXs: {', '.join(PRIORITY_DEXS)}")
    print(f"Alert condition: Top {TOP_N_CHECK} overall + Priority pair + Delta >= {DELTA_ENTER_THRESHOLD}%")

    if tracked_wallets:
        print(f"Tracking {len(tracked_wallets)} wallet(s)")
    else:
        print("No wallets. Add: python treadfi_trader.py add <wallet>")

    print("-" * 70)
    print("Starting... (Ctrl+C to stop)\n")

    history = {}

    try:
        while True:
            # Fetch market data
            dex_data = {}
            for dex in relevant_dexs:
                data = fetch_market_data(dex)
                if data:
                    dex_data[dex] = data

            # Calculate spreads
            all_spreads = []
            timestamp = datetime.now()
            timestamp_str = timestamp.strftime('%H:%M:%S')

            for asset_base, mappings in shared_assets.items():
                asset_rates = []
                for dex_name, full_name in mappings.items():
                    if dex_name in dex_data:
                        rate = find_asset_funding(dex_data[dex_name], full_name)
                        if rate is not None:
                            asset_rates.append((dex_name, rate, full_name))

                if len(asset_rates) < 2:
                    continue

                # Best Long = lowest funding, Best Short = highest funding
                min_item = min(asset_rates, key=lambda x: x[1])
                max_item = max(asset_rates, key=lambda x: x[1])

                long_dex, long_rate, long_full = min_item
                short_dex, short_rate, short_full = max_item

                delta = (short_rate - long_rate) * 100
                apr = delta * 365 * 24

                is_priority = is_priority_pair(long_dex, short_dex)

                spread_info = {
                    'asset': asset_base,
                    'long_dex': long_dex,
                    'short_dex': short_dex,
                    'long_rate': long_rate,
                    'short_rate': short_rate,
                    'long_full': long_full,
                    'short_full': short_full,
                    'delta': delta,
                    'apr': apr,
                    'is_priority': is_priority
                }

                all_spreads.append(spread_info)

            # Sort by delta descending
            all_spreads.sort(key=lambda x: x['delta'], reverse=True)

            # Store for Telegram status
            global latest_spreads
            latest_spreads = all_spreads

            # Build spread lookup
            spread_data = {s['asset']: s for s in all_spreads}

            # Process signals
            alerts = process_signals(all_spreads, shared_assets)

            # Update history
            for s in all_spreads:
                asset = s['asset']
                delta = s['delta']
                if asset not in history:
                    history[asset] = {'times': [], 'values': []}
                history[asset]['times'].append(timestamp_str)
                history[asset]['values'].append(delta)
                if len(history[asset]['times']) > 60:
                    history[asset]['times'].pop(0)
                    history[asset]['values'].pop(0)

            # Fetch and analyze positions
            position_analysis = []
            position_alerts = []
            if tracked_wallets:
                positions = get_all_user_positions()
                position_analysis, position_alerts = analyze_positions(positions, spread_data)
                alerts.extend(position_alerts)

            # === RENDER ===
            plt.clear_terminal()

            print(f"[{timestamp_str}] TreadFi Trader")
            print(f"Priority: {'/'.join(PRIORITY_DEXS)} | Alert: Top {TOP_N_CHECK} + Priority + Δ≥{DELTA_ENTER_THRESHOLD}%")
            print("=" * 70)

            # Positions
            if position_analysis:
                print_positions(position_analysis)
                print("-" * 70)

            # Top 10 spreads with clear indicators
            print(f"\n📋 TOP 10 SPREADS (by Delta)")
            table = []
            for i, s in enumerate(all_spreads[:10], 1):
                # Determine status
                is_top_n = i <= TOP_N_CHECK
                is_pri = s['is_priority']
                meets_threshold = s['delta'] >= DELTA_ENTER_THRESHOLD

                if s['asset'] in active_signals:
                    signal = "✅ HOLD"
                elif is_top_n and is_pri and meets_threshold:
                    signal = "🟢 ENTER"
                elif is_top_n and is_pri and not meets_threshold:
                    signal = f"⏳ (Δ<{DELTA_ENTER_THRESHOLD}%)"
                elif is_top_n and not is_pri:
                    signal = "❌ (not priority)"
                else:
                    signal = "-"

                pri_mark = "⭐" if is_pri else "  "

                table.append([
                    f"#{i}",
                    f"{pri_mark}{s['asset']}",
                    f"{s['long_dex']}: {s['long_rate']*100:+.4f}%",
                    f"{s['short_dex']}: {s['short_rate']*100:+.4f}%",
                    f"{s['delta']:.4f}%",
                    f"{s['apr']:.0f}%",
                    signal
                ])

            headers = ["Rank", "Asset", "Best Long (LONG here)", "Best Short (SHORT here)", "Delta", "APR", "Signal"]
            print(tabulate(table, headers=headers, tablefmt="simple"))

            # Legend
            print(f"\n  ⭐ = Priority pair ({'/'.join(PRIORITY_DEXS)})")
            print(f"  🟢 ENTER = Top {TOP_N_CHECK} + Priority + Delta >= {DELTA_ENTER_THRESHOLD}%")

            # Active signals
            print_active_signals(all_spreads)

            # Alerts
            print_alerts(alerts)

            print("-" * 70)

            # Chart
            top_5 = [s['asset'] for s in all_spreads[:5]]
            plt.clear_data()
            plt.clear_figure()
            plt.title("Top 5 Spreads")
            plt.ylabel("Delta %")
            plt.theme('dark')

            for asset in top_5:
                if asset in history and history[asset]['values']:
                    indices = list(range(len(history[asset]['values'])))
                    plt.plot(indices, history[asset]['values'], label=asset, marker='dot')

            if top_5 and top_5[0] in history:
                times = history[top_5[0]]['times']
                if times:
                    indices = list(range(len(times)))
                    step = max(1, len(indices) // 5)
                    plt.xticks(indices[::step], times[::step])

            plt.show()

            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        print("\n\nStopped.")
        if TELEGRAM_CHAT_ID:
            send_telegram("⚠️ TreadFi Trader đã dừng.")
        sys.exit(0)


if __name__ == "__main__":
    main()
