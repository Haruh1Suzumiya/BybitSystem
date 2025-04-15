import configparser
import logging
from pybit.unified_trading import HTTP
import time
import datetime
import re
import pytz
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import threading

logs = []
debug_logs = []
is_arbitrage_running = False

class PositionTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.clear_position()

    def update_position(self, coin, qty, linear_symbol, spot_symbol, fr, entry_time):
        with self.lock:
            self.current_coin = coin
            self.current_qty = qty
            self.current_linear_symbol = linear_symbol
            self.current_spot_symbol = spot_symbol
            self.current_fr = fr
            self.entry_time = entry_time
            self.fr_change_count = 0  # FR変換回数をリセット

    def get_position(self):
        with self.lock:
            return (self.current_coin, self.current_qty, self.current_linear_symbol, 
                    self.current_spot_symbol, self.current_fr, self.entry_time, self.fr_change_count)

    def clear_position(self):
        with self.lock:
            self.current_coin = None
            self.current_qty = None
            self.current_linear_symbol = None
            self.current_spot_symbol = None
            self.current_fr = None
            self.entry_time = None
            self.fr_change_count = 0

    def has_position(self):
        with self.lock:
            return self.current_coin is not None

    def increment_fr_change_count(self):
        with self.lock:
            self.fr_change_count += 1

    def update_fr(self, new_fr):
        with self.lock:
            self.current_fr = new_fr

position_tracker = PositionTracker()

def load_config(config_file='config/config.ini'):
    config = configparser.ConfigParser()
    config.read(config_file)
    
    # Ensure all required sections and keys exist
    if 'API' not in config:
        config['API'] = {}
    if 'SYSTEM' not in config:
        config['SYSTEM'] = {}
    if 'TRADE' not in config:
        config['TRADE'] = {}
    if 'FUNDING' not in config:
        config['FUNDING'] = {}
    if 'TICKERS' not in config:
        config['TICKERS'] = {}
    
    # Set default values if not present
    config['API'].setdefault('key', '')
    config['API'].setdefault('secret', '')
    config['API'].setdefault('testnet', 'false')
    config['SYSTEM'].setdefault('log_level', 'INFO')
    config['SYSTEM'].setdefault('debug_mode', 'true')
    config['SYSTEM'].setdefault('port', '8000')
    config['TRADE'].setdefault('leverage', '1')
    config['TRADE'].setdefault('max_trade_amount', '100000')
    config['FUNDING'].setdefault('funding_times', '01:00,09:00,17:00')
    
    return config

def setup_logging(log_level):
    logging.basicConfig(level=getattr(logging, log_level))

def get_session(config):
    return HTTP(
        testnet=config['API']['testnet'].lower() == 'true',
        api_key=config['API']['key'],
        api_secret=config['API']['secret'],
        recv_window=20000  # Increase recv_window to 20 seconds
    )

def log_message(message, is_debug=False):
    global logs, debug_logs
    print(f"Logging: {message}")  # デバッグ用
    logging.info(message)
    
    if is_debug:
        debug_logs.append(message)
        if len(debug_logs) > 100:
            debug_logs.pop(0)
    else:
        simplified_message = simplify_message(message)
        if simplified_message:
            logs.append(simplified_message)
            if len(logs) > 100:
                logs.pop(0)

def simplify_message(message):
    if "Successfully opened new position:" in message:
        match = re.search(r"Successfully opened new position: (\w+)/(\w+)", message)
        if match:
            return f"🚀 新規ポジション: {match.group(1)}/{match.group(2)}"
    elif "Current pair" in message:
        match = re.search(r"Current pair (\w+)/(\w+) - FR: ([\d.-]+), Cumulative FR: ([\d.-]+)", message)
        if match:
            symbol = match.group(1)
            fr = float(match.group(3).rstrip('.')) * 100
            cumulative_fr = float(match.group(4).rstrip('.')) * 100
            fr_count_match = re.search(r"FR change count: (\d+)", message)
            if fr_count_match:
                fr_count = int(fr_count_match.group(1))
                return f"📊 {symbol}: FR {fr:.4f}%, 累積FR {cumulative_fr:.4f}%, 回数 {fr_count}"
            else:
                return f"📊 {symbol}: FR {fr:.4f}%, 累積FR {cumulative_fr:.4f}%"
    elif "Closing current position due to FR < 0.01%" in message:
        return "🔄 ポジションクローズ: FR < 0.01%"
    elif "Keeping current position" in message:
        return "✅ ポジション維持"
    elif "No viable arbitrage opportunities found" in message:
        return "🔍 機会なし、次のFR時間まで待機"
    elif "Waiting for" in message:
        match = re.search(r"Waiting for ([\d.]+) seconds until next funding time at ([\d:]+\s[AP]M)", message)
        if match:
            seconds = float(match.group(1))
            next_fr_time = match.group(2)
            hours, remainder = divmod(int(seconds), 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"⏳ 次のFR: {next_fr_time} ({hours:02d}時間{minutes:02d}分後)"
    elif "Attempting arbitrage:" in message:
        match = re.search(r"Attempting arbitrage: Spot: (\w+), Futures: (\w+) with FR: ([\d.-]+), Cumulative FR: ([\d.-]+)", message)
        if match:
            fr = float(match.group(3).rstrip('.')) * 100
            cumulative_fr = float(match.group(4).rstrip('.')) * 100
            return f"💡 試行: {match.group(1)}/{match.group(2)}, FR: {fr:.4f}%, 累積FR: {cumulative_fr:.4f}%"
    elif "Arbitrage loop ended" in message:
        return "🛑 アービトラージループ終了"
    return None  # シンプル化が不要なメッセージの場合はNoneを返す

def get_instrument_info(session, category, symbol):
    try:
        info = session.get_instruments_info(category=category, symbol=symbol)
        return info['result']['list'][0]
    except Exception as e:
        log_message(f"Error getting instrument info for {symbol}: {str(e)}")
        return None

def get_available_spot_symbols(session):
    try:
        instruments = session.get_instruments_info(category="spot")
        return set(instrument['symbol'] for instrument in instruments['result']['list'])
    except Exception as e:
        log_message(f"Error fetching available spot symbols: {str(e)}")
        return set()

def normalize_symbol(symbol):
    return re.sub(r'^(\d+)', '', symbol)

def find_arbitrage_opportunities(config):
    session = get_session(config)
    available_spot_symbols = get_available_spot_symbols(session)
    tickers = session.get_tickers(category="linear")
    funding_rates = []
    for ticker in tickers['result']['list']:
        if 'fundingRate' in ticker and ticker['fundingRate']:
            try:
                funding_rate = float(ticker['fundingRate'])
                funding_rates.append({
                    'symbol': ticker['symbol'],
                    'fundingRate': funding_rate
                })
            except ValueError:
                log_message(f"Invalid funding rate for {ticker['symbol']}: {ticker['fundingRate']}")
    funding_rates.sort(key=lambda x: x['fundingRate'])  # マイナス値を優先するためにソート順を変更
    
    opportunities = []
    for rate in funding_rates:
        symbol = rate['symbol']
        normalized_symbol = normalize_symbol(symbol)
        if symbol.endswith('USDT'):
            matching_spot_symbols = [s for s in available_spot_symbols if normalize_symbol(s) == normalized_symbol]
            if matching_spot_symbols:
                spot_symbol = matching_spot_symbols[0]
                spot_info = get_instrument_info(session, "spot", spot_symbol)
                linear_info = get_instrument_info(session, "linear", symbol)
                if spot_info and linear_info:
                    opportunities.append((symbol, spot_symbol, rate['fundingRate'], spot_info, linear_info))
    
    return opportunities

def execute_arbitrage(config, linear_symbol, spot_symbol, funding_rate, spot_info, linear_info):
    session = get_session(config)
    
    try:
        try:
            session.set_leverage(
                category="linear",
                symbol=linear_symbol,
                buyLeverage="1",
                sellLeverage="1"
            )
        except Exception as e:
            log_message(f"Warning: Could not set leverage for {linear_symbol}: {str(e)}")
        
        futures_ticker = session.get_tickers(category="linear", symbol=linear_symbol)
        spot_ticker = session.get_tickers(category="spot", symbol=spot_symbol)
        futures_price = float(futures_ticker['result']['list'][0]['lastPrice'])
        spot_price = float(spot_ticker['result']['list'][0]['lastPrice'])
        avg_price = (futures_price + spot_price) / 2

        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = float(usdt_balance)

    except Exception as e:
        log_message(f"Error getting prices or balance for {linear_symbol} and {spot_symbol}: {str(e)}")
        return None

    # 残高の48%をそれぞれのポジションに使用
    max_trade_amount = usdt_balance * 0.49
    
    # 現物の注文量情報を取得
    spot_min_order_qty = Decimal(spot_info['lotSizeFilter']['minOrderQty'])
    spot_max_order_qty = Decimal(spot_info['lotSizeFilter']['maxOrderQty'])
    spot_qty_step = Decimal(spot_info['lotSizeFilter'].get('basePrecision', '0.000001'))
    
    # 先物の注文量情報を取得
    linear_min_order_qty = Decimal(linear_info['lotSizeFilter']['minOrderQty'])
    linear_max_order_qty = Decimal(linear_info['lotSizeFilter']['maxOrderQty'])
    linear_qty_step = Decimal(linear_info['lotSizeFilter']['qtyStep'])
    
    # 現物と先物の制限を考慮して注文量を決定
    min_order_qty = max(spot_min_order_qty, linear_min_order_qty)
    max_order_qty = min(spot_max_order_qty, linear_max_order_qty, Decimal(str(max_trade_amount)) / Decimal(str(avg_price)))
    qty_step = max(spot_qty_step, linear_qty_step)
    
    qty = max(min_order_qty, min(Decimal(str(max_trade_amount)) / Decimal(str(avg_price)), max_order_qty))
    qty = (qty / qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * qty_step

    # 価格の精度を調整
    spot_price_tick_size = Decimal(spot_info['priceFilter']['tickSize'])
    linear_price_tick_size = Decimal(linear_info['priceFilter']['tickSize'])
    price_tick_size = max(spot_price_tick_size, linear_price_tick_size)
    price = (Decimal(str(avg_price)) / price_tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * price_tick_size

    # 数量と価格を文字列に変換
    qty_str = str(qty)
    price_str = str(price)

    log_message(f"Attempting to place orders for Spot: {spot_symbol}, Futures: {linear_symbol}")
    log_message(f"Order details - Quantity: {qty_str}, Price: {price_str}, Total value per position: {float(qty) * float(price):.2f} USDT")

    try:
        spot_order = session.place_order(
            category="spot",
            symbol=spot_symbol,
            side="Buy",
            orderType="Limit",
            qty=qty_str,
            price=price_str
        )
        if spot_order['retCode'] != 0:
            log_message(f"Failed to place spot order: {spot_order['retMsg']}")
            return {'retCode': spot_order['retCode'], 'retMsg': spot_order['retMsg']}

        futures_order = session.place_order(
            category="linear",
            symbol=linear_symbol,
            side="Sell",
            orderType="Limit",
            qty=qty_str,
            price=price_str
        )
        if futures_order['retCode'] != 0:
            log_message(f"Failed to place futures order: {futures_order['retMsg']}")
            # Spot order was successful, so we need to cancel it
            session.cancel_order(category="spot", symbol=spot_symbol, orderId=spot_order['result']['orderId'])
            return {'retCode': futures_order['retCode'], 'retMsg': futures_order['retMsg']}

        log_message(f"Successfully placed orders for Spot: {spot_symbol}, Futures: {linear_symbol}")
        return {'retCode': 0, 'qty': qty}
    except Exception as e:
        log_message(f"Error placing orders for Spot: {spot_symbol}, Futures: {linear_symbol}: {str(e)}")
        return {'retCode': -1, 'retMsg': str(e)}

def save_tickers_to_config(spot_ticker, futures_ticker, config_file='config/config.ini'):
    config = configparser.ConfigParser()
    config.read(config_file)
    
    if 'TICKERS' not in config:
        config['TICKERS'] = {}
    
    config['TICKERS']['spot_ticker'] = str(spot_ticker)
    config['TICKERS']['futures_ticker'] = str(futures_ticker)
    
    with open(config_file, 'w') as configfile:
        config.write(configfile)

def get_saved_tickers_from_config(config_file='config/config.ini'):
    config = configparser.ConfigParser()
    config.read(config_file)
    
    if 'TICKERS' not in config:
        return None, None
    
    try:
        return (
            config['TICKERS']['spot_ticker'],
            config['TICKERS']['futures_ticker']
        )
    except KeyError:
        return None, None

def clear_saved_tickers_from_config(config_file='config/config.ini'):
    config = configparser.ConfigParser()
    config.read(config_file)
    
    if 'TICKERS' in config:
        config.remove_section('TICKERS')
    
    with open(config_file, 'w') as configfile:
        config.write(configfile)

def arbitrage_loop(config, is_running_event):
    global position_tracker

    while is_running_event.is_set():
        opportunities = find_arbitrage_opportunities(config)
        
        current_coin, current_qty, current_linear_symbol, current_spot_symbol, current_fr, entry_time, fr_change_count = position_tracker.get_position()

        # Check FR value of current position
        if position_tracker.has_position():
            current_opportunity = next((opp for opp in opportunities if opp[0] == current_linear_symbol and opp[1] == current_spot_symbol), None)
            if current_opportunity:
                new_fr = current_opportunity[2]
                cumulative_fr = current_opportunity[3]
                
                position_tracker.update_fr(new_fr)
                position_tracker.increment_fr_change_count()
                fr_change_count += 1  # Increment local variable for immediate use
                
                if fr_change_count <= 3:
                    log_message(f"Current pair {current_linear_symbol}/{current_spot_symbol} - FR: {new_fr:.6f}, Cumulative FR: {cumulative_fr:.6f}. FR change count: {fr_change_count}")
                else:
                    log_message(f"Current pair {current_linear_symbol}/{current_spot_symbol} - FR: {new_fr:.6f}, Cumulative FR: {cumulative_fr:.6f}")
                
                if fr_change_count > 3 and new_fr < 0.0001:  # After 4th FR change (count > 3), if FR is below 0.01%
                    log_message(f"Closing current position due to FR < 0.01%")
                    close_all_positions(config, current_linear_symbol, current_spot_symbol, current_coin, current_qty, get_instrument_info(get_session(config), "spot", current_spot_symbol))
                    position_tracker.clear_position()
                    clear_saved_tickers_from_config()
                else:
                    if fr_change_count <= 3:
                        log_message(f"Keeping current position. FR: {new_fr:.6f}, Cumulative FR: {cumulative_fr:.6f}, FR change count: {fr_change_count}")
                    else:
                        log_message(f"Keeping current position. FR: {new_fr:.6f}, Cumulative FR: {cumulative_fr:.6f}")
            else:
                log_message(f"Current pair {current_linear_symbol}/{current_spot_symbol} not found in opportunities. Closing position.")
                close_all_positions(config, current_linear_symbol, current_spot_symbol, current_coin, current_qty, get_instrument_info(get_session(config), "spot", current_spot_symbol))
                position_tracker.clear_position()
                clear_saved_tickers_from_config()

        if not opportunities:
            log_message(f"No viable arbitrage opportunities found. Waiting for next funding time.")
            wait_time, next_fr_time = get_next_funding_time(config)
            log_message(f"Waiting for {wait_time:.2f} seconds until next funding time at {next_fr_time.strftime('%I:%M:%S %p')}")
            time.sleep(wait_time)
            continue

        # Open new position only if we don't have one
        if not position_tracker.has_position():
            executed_successfully = False
            for opportunity in opportunities:
                linear_symbol, spot_symbol, funding_rate, cumulative_fr, spot_info, linear_info = opportunity

                log_message(f"Attempting arbitrage: Spot: {spot_symbol}, Futures: {linear_symbol} with FR: {funding_rate:.6f}, Cumulative FR: {cumulative_fr:.6f}")

                result = execute_arbitrage(config, linear_symbol, spot_symbol, funding_rate, spot_info, linear_info)

                if result and result.get('retCode') == 0:
                    qty = result.get('qty')
                    position_tracker.update_position(
                        spot_symbol.replace('USDT', ''),
                        qty,
                        linear_symbol,
                        spot_symbol,
                        funding_rate,
                        time.time()
                    )
                    save_tickers_to_config(spot_symbol, linear_symbol)
                    log_message(f"Successfully opened new position: {linear_symbol}/{spot_symbol}. FR change count reset.")
                    executed_successfully = True
                    break
                elif result and result.get('retCode') == -2:
                    log_message(f"Price difference too large for {linear_symbol}/{spot_symbol}. Trying next opportunity.")
                    continue
                else:
                    log_message(f"Failed to execute arbitrage for Spot: {spot_symbol}, Futures: {linear_symbol}. Trying next opportunity.")

            if not executed_successfully:
                log_message("Failed to execute arbitrage for all opportunities. Waiting for next funding time.")

        # Wait until next FR time
        wait_time, next_fr_time = get_next_funding_time(config)
        log_message(f"Waiting for {wait_time:.2f} seconds until next funding time at {next_fr_time.strftime('%I:%M:%S %p')}")

        start_time = time.time()
        while time.time() - start_time < wait_time and is_running_event.is_set():
            time.sleep(1)

    # Close the last position if loop is terminated
    if position_tracker.has_position():
        current_coin, current_qty, current_linear_symbol, current_spot_symbol, _, _, _ = position_tracker.get_position()
        close_all_positions(config, current_linear_symbol, current_spot_symbol, current_coin, current_qty, get_instrument_info(get_session(config), "spot", current_spot_symbol))
        position_tracker.clear_position()
        clear_saved_tickers_from_config()

    log_message("Arbitrage loop ended.")

def calculate_cumulative_fr(session, symbol):
    try:
        history = session.get_funding_rate_history(
            category="linear",
            symbol=symbol,
            limit=9
        )
        
        cumulative_fr = sum(float(rate['fundingRate']) for rate in history['result']['list'])
        return cumulative_fr
    except Exception as e:
        log_message(f"Error calculating cumulative FR for {symbol}: {str(e)}")
        return 0

def find_arbitrage_opportunities(config):
    session = get_session(config)
    available_spot_symbols = get_available_spot_symbols(session)
    tickers = session.get_tickers(category="linear")
    funding_rates = []
    
    for ticker in tickers['result']['list']:
        if 'fundingRate' in ticker and ticker['fundingRate']:
            try:
                current_fr = float(ticker['fundingRate'])
                if current_fr >= 0.00009:  # 現在のFRが0.009%以上のものだけを対象とする
                    symbol = ticker['symbol']
                    cumulative_fr = calculate_cumulative_fr(session, symbol)
                    
                    if cumulative_fr >= 0.0009:  # 累積FRが0.09%以上のものだけを対象とする
                        funding_rates.append({
                            'symbol': symbol,
                            'currentFundingRate': current_fr,
                            'cumulativeFundingRate': cumulative_fr
                        })
            except ValueError:
                log_message(f"Invalid funding rate for {ticker['symbol']}: {ticker['fundingRate']}")
    
    # 累積FRでソート（高い順）
    funding_rates.sort(key=lambda x: x['cumulativeFundingRate'], reverse=True)
    
    opportunities = []
    for rate in funding_rates:
        symbol = rate['symbol']
        normalized_symbol = normalize_symbol(symbol)
        if symbol.endswith('USDT'):
            matching_spot_symbols = [s for s in available_spot_symbols if normalize_symbol(s) == normalized_symbol]
            if matching_spot_symbols:
                spot_symbol = matching_spot_symbols[0]
                spot_info = get_instrument_info(session, "spot", spot_symbol)
                linear_info = get_instrument_info(session, "linear", symbol)
                if spot_info and linear_info:
                    opportunities.append((symbol, spot_symbol, rate['currentFundingRate'], rate['cumulativeFundingRate'], spot_info, linear_info))
    
    return opportunities

def cancel_all_orders(config):
    session = get_session(config)
    try:
        # Cancel all orders for linear and spot
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        session.cancel_all_orders(category="spot", settleCoin="USDT")
        log_message("All orders cancelled")
    except Exception as e:
        log_message(f"Error in cancel_all_orders: {str(e)}")

def close_all_positions(config, linear_symbol, spot_symbol, current_coin, current_qty, spot_info):
    session = get_session(config)
    try:
        # Cancel all orders
        cancel_all_orders(config)
        
        # Close linear position
        try:
            session.place_order(
                category="linear",
                symbol=linear_symbol,
                side="Buy" if session.get_positions(category="linear", symbol=linear_symbol)['result']['list'][0]['side'] == "Sell" else "Sell",
                orderType="Market",
                qty="0",
                reduceOnly=True
            )
            log_message(f"Successfully closed linear position for {linear_symbol}")
        except Exception as e:
            log_message(f"Error closing linear position: {str(e)}")
        
        # Sell spot position
        if current_coin and current_qty:
            try:
                # Get current balance
                balance = session.get_wallet_balance(accountType="UNIFIED")
                coin_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == current_coin), '0')
                
                # Adjust quantity based on lotSizeFilter and current balance
                min_order_qty = Decimal(spot_info['lotSizeFilter']['minOrderQty'])
                max_order_qty = Decimal(spot_info['lotSizeFilter']['maxOrderQty'])
                qty_step = Decimal(spot_info['lotSizeFilter'].get('basePrecision', '0.000001'))
                
                adjusted_qty = max(min_order_qty, min(Decimal(coin_balance), Decimal(current_qty), max_order_qty))
                adjusted_qty = (adjusted_qty / qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * qty_step
                
                if adjusted_qty >= min_order_qty:
                    spot_order = session.place_order(
                        category="spot",
                        symbol=spot_symbol,
                        side="Sell",
                        orderType="Market",
                        qty=str(adjusted_qty)
                    )
                    
                    if spot_order['retCode'] != 0:
                        log_message(f"Failed to sell spot position: {spot_order['retMsg']}")
                    else:
                        log_message(f"Successfully sold spot position for {current_coin}")
                else:
                    log_message(f"Spot position for {current_coin} is too small to sell (less than min order quantity)")
            except Exception as e:
                log_message(f"Error selling spot position: {str(e)}")
        
        log_message("All orders cancelled and positions closed")
    except Exception as e:
        log_message(f"Error in close_all_positions: {str(e)}")

def get_next_funding_time(config):
    tokyo_tz = pytz.timezone('Asia/Tokyo')
    now = datetime.now(tokyo_tz)
    funding_times = [datetime.strptime(t.strip(), "%H:%M").time() for t in config['FUNDING']['funding_times'].split(',')]
    funding_times.sort()  # 時間順にソート

    # 現在の日付と次の日付を考慮したすべての funding time を生成
    all_funding_times = [
        tokyo_tz.localize(datetime.combine(now.date(), t))
        for t in funding_times
    ] + [
        tokyo_tz.localize(datetime.combine(now.date() + timedelta(days=1), t))
        for t in funding_times
    ]

    # 現在時刻より後の最も近い funding time を見つける
    next_funding = min((t for t in all_funding_times if t > now), key=lambda x: x - now)
    
    wait_time = (next_funding - now).total_seconds()
    return wait_time, next_funding

def is_api_configured(config):
    return config['API']['key'] and config['API']['secret']

def cancel_all_orders_and_close_positions(config):
    session = get_session(config)
    try:
        # Cancel all orders for linear and spot
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        session.cancel_all_orders(category="spot", settleCoin="USDT")
        
        # Close all linear positions
        positions = session.get_positions(category="linear", settleCoin="USDT")
        for position in positions['result']['list']:
            if float(position['size']) != 0:
                session.place_order(
                    category="linear",
                    symbol=position['symbol'],
                    side="Buy" if position['side'] == "Sell" else "Sell",
                    orderType="Market",
                    qty=position['size'],
                    reduceOnly=True
                )
        
        # Sell all spot positions
        balances = session.get_wallet_balance(accountType="UNIFIED")
        for coin in balances['result']['list'][0]['coin']:
            if coin['coin'] != 'USDT' and float(coin['walletBalance']) > 0:
                symbol = f"{coin['coin']}USDT"
                spot_info = get_instrument_info(session, "spot", symbol)
                if spot_info:
                    min_order_qty = Decimal(spot_info['lotSizeFilter']['minOrderQty'])
                    max_order_qty = Decimal(spot_info['lotSizeFilter']['maxOrderQty'])
                    qty_step = Decimal(spot_info['lotSizeFilter'].get('basePrecision', '0.000001'))
                    
                    balance = Decimal(coin['walletBalance'])
                    adjusted_qty = max(min_order_qty, min(balance, max_order_qty))
                    adjusted_qty = (adjusted_qty / qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * qty_step
                    
                    session.place_order(
                        category="spot",
                        symbol=symbol,
                        side="Sell",
                        orderType="Market",
                        qty=str(adjusted_qty)
                    )
        
        log_message("All orders cancelled and positions closed")
    except Exception as e:
        log_message(f"Error in cancel_all_orders_and_close_positions: {str(e)}")

def handle_user_stop(config):
    global position_tracker
    if position_tracker.has_position():
        current_coin, current_qty, current_linear_symbol, current_spot_symbol, _, _ = position_tracker.get_position()
    else:
        # If position_tracker doesn't have a position, try to get it from config
        spot_ticker, futures_ticker = get_saved_tickers_from_config()
        if spot_ticker and futures_ticker:
            current_spot_symbol = spot_ticker
            current_linear_symbol = futures_ticker
            current_coin = spot_ticker.replace('USDT', '')
            # We don't have the current_qty, so we'll let the close_all_positions function handle it
            current_qty = None
        else:
            log_message("No position found in tracker or config. Cancelling all orders.")
            cancel_all_orders(config)
            return

    spot_info = get_instrument_info(get_session(config), "spot", current_spot_symbol)
    if spot_info:
        close_all_positions(config, current_linear_symbol, current_spot_symbol, current_coin, current_qty, spot_info)
    else:
        log_message(f"Could not get instrument info for {current_spot_symbol}. Cancelling all orders.")
        cancel_all_orders(config)
    
    position_tracker.clear_position()
    clear_saved_tickers_from_config()
    log_message("Arbitrage stopped by user. All positions closed, orders cancelled, and ticker memory cleared.")

def handle_user_start(config):
    log_message("Arbitrage started by user. Ready to find new opportunities.")
    # ticker記録は arbitrage_loop 内で行われるため、ここでは特に何もしない
