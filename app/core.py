import configparser
import logging
from pybit.unified_trading import HTTP
import time
import pytz
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import threading
import re

# グローバル変数
logs = []
is_arbitrage_running = threading.Event()
arbitrage_thread = None
liquidation_monitor_thread = None
current_position = {
    'coin': None,
    'qty': None,
    'linear_symbol': None,
    'spot_symbol': None,
    'fr': None,
    'entry_time': None,
    'fr_change_count': 0,
    'liquidation_price': None,
    'entry_price': None
}

# 固定ペア設定
FIXED_SPOT_SYMBOL = "ROSEUSDT"
FIXED_LINEAR_SYMBOL = "ROSEUSDT"

# 清算監視設定
LIQUIDATION_MONITOR_INTERVAL = 3600  # 1時間（秒）
LIQUIDATION_SAFETY_RATIO = 0.15  # 清算価格の15%手前でリバランス

def load_config(config_file='config/config.ini'):
    """設定ファイルを読み込む"""
    config = configparser.ConfigParser()
    config.read(config_file)
    
    # デフォルト設定がなければ作成
    if 'API' not in config:
        config['API'] = {}
    if 'SYSTEM' not in config:
        config['SYSTEM'] = {}
    if 'TRADE' not in config:
        config['TRADE'] = {}
    if 'FUNDING' not in config:
        config['FUNDING'] = {}
    if 'POSITION' not in config:
        config['POSITION'] = {}
    if 'LIQUIDATION' not in config:
        config['LIQUIDATION'] = {}
    
    # デフォルト値設定
    config['API'].setdefault('key', '')
    config['API'].setdefault('secret', '')
    config['API'].setdefault('testnet', 'True')
    config['SYSTEM'].setdefault('log_level', 'INFO')
    config['SYSTEM'].setdefault('debug_mode', 'true')
    config['SYSTEM'].setdefault('port', '8000')
    config['TRADE'].setdefault('leverage', '1')
    config['TRADE'].setdefault('max_trade_amount', '100000')
    config['FUNDING'].setdefault('funding_times', '01:00,09:00,17:00')
    config['LIQUIDATION'].setdefault('monitor_interval', '3600')
    config['LIQUIDATION'].setdefault('safety_ratio', '0.15')
    config['LIQUIDATION'].setdefault('auto_rebalance', 'true')
    
    # 設定を保存
    with open(config_file, 'w') as configfile:
        config.write(configfile)
    
    return config

def setup_logging(log_level):
    """ロギング設定 - Windows対応"""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('app.log', encoding='utf-8')  # UTF-8エンコーディング指定
        ]
    )

def get_session(config):
    """Bybit APIセッションを取得"""
    return HTTP(
        testnet=config['API']['testnet'].lower() == 'true',
        api_key=config['API']['key'],
        api_secret=config['API']['secret'],
        recv_window=20000  # レスポンスタイムアウト20秒
    )

def log_message(message):
    """ログメッセージを記録 - 特殊文字を安全な文字に置換"""
    global logs
    
    # 特殊文字を安全な文字に置換
    safe_message = message.replace('→', '->')
    
    print(f"LOG: {safe_message}")
    try:
        logging.info(safe_message)
    except UnicodeEncodeError:
        # フォールバック：ASCII文字のみでログ出力
        ascii_message = safe_message.encode('ascii', 'ignore').decode('ascii')
        logging.info(ascii_message)
    
    # ログをシンプル化して保存
    simplified_message = simplify_message(safe_message)
    if simplified_message:
        logs.append(simplified_message)
        if len(logs) > 100:
            logs.pop(0)
    else:
        logs.append(safe_message)
        if len(logs) > 100:
            logs.pop(0)

def simplify_message(message):
    """ログメッセージを分かりやすく整形"""
    # 清算監視関連
    if "Liquidation monitoring" in message:
        return f"🛡️ {message}"
    
    if "Risk detected" in message:
        return f"⚠️ {message}"
    
    if "Emergency rebalance" in message:
        return f"🚨 {message}"
    
    if "Position rebuilt successfully" in message:
        return f"✅ {message}"
    
    # ポジションオープン
    if "Successfully opened new position:" in message:
        return f"🚀 新規ポジション: ROSEUSDT"
    
    # 現在のペア情報
    elif "Current pair" in message:
        match = re.search(r"Current pair (\w+)/(\w+) - FR: ([\d.-]+)%, Cumulative FR: ([\d.-]+)%", message)
        if match:
            fr = float(match.group(3))
            cumulative_fr = float(match.group(4))
            return f"📊 ROSEUSDT: 現在FR {fr:.4f}%, 累積FR {cumulative_fr:.4f}%"
    
    # ポジションクローズ（実際にはクローズしない）
    elif "Closing current position" in message:
        return "✅ ポジション維持（FR変動検知）"
    
    # ポジション維持
    elif "Keeping current position" in message:
        return "✅ ポジション維持"
    
    # 機会なし
    elif "No viable arbitrage opportunities found" in message:
        return "🔍 機会なし、次のFR時間まで待機"
    
    # FR時間待機
    elif "Waiting for" in message:
        match = re.search(r"Waiting for ([\d.]+) seconds until next funding time at (.+)", message)
        if match:
            seconds = float(match.group(1))
            next_fr_time = match.group(2)
            hours, remainder = divmod(int(seconds), 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"⏳ 次のFR: {next_fr_time} ({hours:02d}時間{minutes:02d}分後)"
    
    # アービトラージ試行
    elif "Attempting arbitrage:" in message:
        return f"💡 試行: ROSEUSDT"
    
    # 上位候補
    elif "Top opportunities:" in message:
        return f"🏆 {message}"
    
    # ループ終了
    elif "Arbitrage loop ended" in message:
        return "🛑 アービトラージループ終了"
    
    # 数量の不一致
    elif "Spot quantity mismatch" in message:
        return f"⚠️ {message}"
    
    # 追加注文
    elif "Additional spot order" in message:
        return f"➕ {message}"
    
    return None  # シンプル化が不要なメッセージ

def get_instrument_info(session, category, symbol):
    """銘柄情報を取得"""
    try:
        info = session.get_instruments_info(category=category, symbol=symbol)
        return info['result']['list'][0]
    except Exception as e:
        log_message(f"Error getting instrument info for {symbol}: {str(e)}")
        return None

def calculate_cumulative_fr(session, symbol, days=5):
    """過去n日間の累積Funding Rateを計算"""
    try:
        # 1日あたり3回のFRなので、n日間なら3*n回分取得
        limit = days * 3
        history = session.get_funding_rate_history(
            category="linear",
            symbol=symbol,
            limit=limit
        )
        
        rates = [float(rate['fundingRate']) for rate in history['result']['list']]
        cumulative_fr = sum(rates)
        return cumulative_fr * 100  # パーセント表示のため100倍
    except Exception as e:
        log_message(f"Error calculating cumulative FR for {symbol}: {str(e)}")
        return 0

def get_liquidation_price(config, symbol):
    """現在のポジションの清算価格を取得"""
    session = get_session(config)
    
    try:
        positions = session.get_positions(
            category="linear",
            symbol=symbol
        )
        
        if positions['retCode'] == 0 and len(positions['result']['list']) > 0:
            for position in positions['result']['list']:
                if float(position['size']) > 0:
                    liq_price = float(position['liqPrice'])
                    mark_price = float(position['markPrice'])
                    unrealized_pnl = float(position['unrealisedPnl'])
                    
                    # 清算価格までの距離をパーセントで計算
                    if liq_price > 0:
                        distance_to_liq = abs(mark_price - liq_price) / mark_price * 100
                        return {
                            'liquidation_price': liq_price,
                            'mark_price': mark_price,
                            'distance_percent': distance_to_liq,
                            'unrealized_pnl': unrealized_pnl,
                            'size': float(position['size']),
                            'side': position['side']
                        }
        
        return None
    except Exception as e:
        log_message(f"Error getting liquidation price for {symbol}: {str(e)}")
        return None

def check_liquidation_risk(config):
    """清算リスクをチェック"""
    global current_position
    
    if not current_position['linear_symbol']:
        return False, None
    
    liq_info = get_liquidation_price(config, current_position['linear_symbol'])
    
    if liq_info:
        safety_ratio = float(config['LIQUIDATION'].get('safety_ratio', '0.15'))
        
        # 清算価格までの距離が安全比率を下回っているかチェック
        risk_threshold = safety_ratio * 100  # パーセント表示
        
        is_at_risk = liq_info['distance_percent'] <= risk_threshold
        
        if is_at_risk:
            log_message(f"Risk detected: Distance to liquidation {liq_info['distance_percent']:.2f}% <= {risk_threshold:.2f}%")
            log_message(f"Mark Price: ${liq_info['mark_price']:.4f}, Liquidation Price: ${liq_info['liquidation_price']:.4f}")
            log_message(f"Unrealized PnL: ${liq_info['unrealized_pnl']:.2f}")
        
        return is_at_risk, liq_info
    
    return False, None

def emergency_rebalance(config):
    """緊急時のポジション建て直し"""
    global current_position
    
    log_message("Emergency rebalance: Starting position reconstruction")
    
    # 現在のポジションを強制クローズ
    if close_current_position(config, force_close=True):
        log_message("Emergency rebalance: Old position closed successfully")
        
        # 残高が安定するまで少し待機
        time.sleep(10)  # 10秒に延長
        
        # 新しいポジションを建て直し（最大3回試行）
        for attempt in range(3):
            log_message(f"Emergency rebalance: Attempt {attempt + 1} to rebuild position")
            
            if execute_arbitrage_fixed(config):
                log_message("Position rebuilt successfully after emergency rebalance")
                return True
            else:
                log_message(f"Failed to rebuild position - attempt {attempt + 1}")
                if attempt < 2:  # 最後の試行でない場合は待機
                    time.sleep(5)
        
        log_message("Failed to rebuild position after emergency rebalance")
        return False
    else:
        log_message("Failed to close position during emergency rebalance")
        return False

def liquidation_monitor_loop(config):
    """清算価格監視ループ"""
    global is_arbitrage_running, current_position
    
    log_message("Liquidation monitoring started")
    
    monitor_interval = int(config['LIQUIDATION'].get('monitor_interval', '3600'))
    auto_rebalance = config['LIQUIDATION'].get('auto_rebalance', 'true').lower() == 'true'
    
    while is_arbitrage_running.is_set():
        try:
            # ポジションがある場合のみ監視
            if current_position['linear_symbol']:
                is_at_risk, liq_info = check_liquidation_risk(config)
                
                if liq_info:
                    log_message(f"Liquidation monitoring - Distance: {liq_info['distance_percent']:.2f}%, Mark: ${liq_info['mark_price']:.4f}, Liq: ${liq_info['liquidation_price']:.4f}")
                    
                    # 清算価格情報をポジションに保存
                    current_position['liquidation_price'] = liq_info['liquidation_price']
                    save_position_to_config(current_position)
                    
                    if is_at_risk and auto_rebalance:
                        log_message("Emergency rebalance triggered due to liquidation risk")
                        
                        if emergency_rebalance(config):
                            log_message("Emergency rebalance completed successfully")
                        else:
                            log_message("Emergency rebalance failed - manual intervention required")
                            # 緊急時は監視を一時停止
                            time.sleep(monitor_interval * 2)
                else:
                    log_message("Liquidation monitoring - No position data available")
            else:
                log_message("Liquidation monitoring - No position to monitor")
            
            # 次の監視まで待機
            start_time = time.time()
            while time.time() - start_time < monitor_interval and is_arbitrage_running.is_set():
                time.sleep(60)  # 1分ごとにチェック
                
        except Exception as e:
            log_message(f"Error in liquidation monitoring: {str(e)}")
            time.sleep(300)  # エラー時は5分待機
    
    log_message("Liquidation monitoring stopped")

def get_top_arbitrage_opportunities(config, top_n=3):
    """UI用：累積FRが高い上位n個のアービトラージ機会を取得（表示用のみ）"""
    session = get_session(config)
    
    # 現物の銘柄一覧を取得
    try:
        spot_instruments = session.get_instruments_info(category="spot")
        available_spot_symbols = {instrument['symbol'] for instrument in spot_instruments['result']['list']}
    except Exception as e:
        log_message(f"Error fetching available spot symbols: {str(e)}")
        return []
    
    # 先物の銘柄一覧とFRを取得
    try:
        tickers = session.get_tickers(category="linear")
        
        # FR情報が有効な銘柄のみフィルタリング
        funding_rates = []
        for ticker in tickers['result']['list']:
            if 'fundingRate' in ticker and ticker['fundingRate']:
                try:
                    current_fr = float(ticker['fundingRate']) * 100  # パーセント表示
                    symbol = ticker['symbol']
                    
                    # USDT建てかつスポットにも存在する銘柄のみ処理
                    if symbol.endswith('USDT') and f"{symbol.replace('USDT', '')}USDT" in available_spot_symbols:
                        # 累積FRを計算
                        cumulative_fr = calculate_cumulative_fr(session, symbol)
                        
                        funding_rates.append({
                            'linear_symbol': symbol,
                            'spot_symbol': f"{symbol.replace('USDT', '')}USDT",
                            'current_fr': current_fr,
                            'cumulative_fr': cumulative_fr
                        })
                except ValueError:
                    continue
    except Exception as e:
        log_message(f"Error fetching tickers: {str(e)}")
        return []
    
    # 累積FRで降順ソート
    funding_rates.sort(key=lambda x: x['cumulative_fr'], reverse=True)
    
    # 上位n個のみ返す
    top_opportunities = funding_rates[:top_n]
    
    # 詳細な情報を追加
    opportunities = []
    for opp in top_opportunities:
        spot_info = get_instrument_info(session, "spot", opp['spot_symbol'])
        linear_info = get_instrument_info(session, "linear", opp['linear_symbol'])
        
        if spot_info and linear_info:
            opportunities.append({
                'linear_symbol': opp['linear_symbol'],
                'spot_symbol': opp['spot_symbol'],
                'current_fr': opp['current_fr'],
                'cumulative_fr': opp['cumulative_fr'],
                'spot_info': spot_info,
                'linear_info': linear_info
            })
    
    return opportunities

def round_to_precision(value, precision):
    """指定された精度で値を丸める"""
    if precision == 0:
        return int(value)
    else:
        multiplier = 10 ** precision
        return round(value * multiplier) / multiplier

def execute_arbitrage_fixed(config):
    """固定ペア（ROSEUSDT）でアービトラージを実行 - 精度エラー修正版"""
    global current_position
    
    linear_symbol = FIXED_LINEAR_SYMBOL
    spot_symbol = FIXED_SPOT_SYMBOL
    
    session = get_session(config)
    
    try:
        # 現在のFRを取得
        futures_ticker = session.get_tickers(category="linear", symbol=linear_symbol)
        current_fr = float(futures_ticker['result']['list'][0]['fundingRate']) * 100
        cumulative_fr = calculate_cumulative_fr(session, linear_symbol)
        
        log_message(f"Attempting arbitrage: Spot: {spot_symbol}, Futures: {linear_symbol} with FR: {current_fr:.4f}%, Cumulative FR: {cumulative_fr:.4f}%")
        
        # レバレッジ設定（エラーが出ても続行）
        try:
            session.set_leverage(
                category="linear",
                symbol=linear_symbol,
                buyLeverage="1",
                sellLeverage="1"
            )
        except Exception as e:
            log_message(f"Warning: Could not set leverage for {linear_symbol}: {str(e)}")
        
        # 現在の価格を取得
        spot_ticker = session.get_tickers(category="spot", symbol=spot_symbol)
        futures_price = float(futures_ticker['result']['list'][0]['lastPrice'])
        spot_price = float(spot_ticker['result']['list'][0]['lastPrice'])
        
        # 価格乖離をチェック (5%以上の乖離があれば実行しない)
        price_diff_percent = abs(futures_price - spot_price) / spot_price * 100
        if price_diff_percent > 5:
            log_message(f"Price difference too large for {linear_symbol}/{spot_symbol}: {price_diff_percent:.2f}%")
            return False
        
        # 残高確認
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = float(usdt_balance)
        
        log_message(f"Current USDT balance: {usdt_balance:.2f}")
        
        # 取引額を決定 (残高の40%ずつを使用、合計80%まで)
        trade_amount_per_side = usdt_balance * 0.40
        
        # 最小取引額チェック
        min_trade_amount = 20.0  # 最小20 USDT
        if trade_amount_per_side < min_trade_amount:
            log_message(f"Trade amount too small: {trade_amount_per_side:.2f} USDT < {min_trade_amount} USDT")
            return False
        
        log_message(f"Trade amount per side: {trade_amount_per_side:.2f} USDT")
        
        # 先物側の数量計算 (USDTを先物価格で割る)
        futures_qty = Decimal(str(trade_amount_per_side)) / Decimal(str(futures_price))
        
        # 先物の注文量情報を取得
        linear_info = get_instrument_info(session, "linear", linear_symbol)
        if not linear_info:
            log_message(f"Failed to get instrument info for {linear_symbol}")
            return False
        
        linear_min_order_qty = Decimal(linear_info['lotSizeFilter']['minOrderQty'])
        linear_qty_step = Decimal(linear_info['lotSizeFilter']['qtyStep'])
        
        # 先物の注文数量を調整
        futures_qty = max(linear_min_order_qty, futures_qty)
        futures_qty = (futures_qty / linear_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * linear_qty_step
        
        log_message(f"Futures quantity: {futures_qty}")
        
        # 現物の注文量を計算（USDT金額ベース）- 精度を2桁に制限
        spot_order_value = float(futures_qty) * spot_price
        spot_order_value = round_to_precision(spot_order_value, 2)  # 2桁まで丸める
        
        # 最小注文価値チェック
        min_order_value = 10.0
        if spot_order_value < min_order_value:
            log_message(f"Spot order value too small: {spot_order_value:.2f} USDT < {min_order_value} USDT")
            return False
        
        log_message(f"Order details - Futures Qty: {futures_qty}, Spot Value: {spot_order_value:.2f} USDT")
        
        # 注文実行順序を変更：現物を先に実行
        log_message(f"Placing spot buy order first for {spot_symbol}")
        
        # 現物買い注文（金額指定）- 精度を明示的に制限
        spot_order = session.place_order(
            category="spot",
            symbol=spot_symbol,
            side="Buy",
            orderType="Market",
            qty=f"{spot_order_value:.2f}",  # 小数点2桁に制限
            isLeverage=0  # 現物取引
        )
        
        if spot_order['retCode'] != 0:
            log_message(f"Failed to place spot order: {spot_order['retMsg']}")
            return False
        
        log_message(f"Spot order successful, now placing futures order")
        
        # 少し待機
        time.sleep(1)
        
        # 先物売り注文
        futures_order = session.place_order(
            category="linear",
            symbol=linear_symbol,
            side="Sell",
            orderType="Market",
            qty=str(futures_qty)
        )
        
        if futures_order['retCode'] != 0:
            log_message(f"Failed to place futures order: {futures_order['retMsg']}")
            # 現物注文のロールバックは困難なので、警告ログのみ
            log_message(f"Warning: Spot order succeeded but futures order failed. Manual intervention may be required.")
            return False
        
        log_message(f"Both orders successful")
        
        # 注文が確定するまで少し待機
        time.sleep(3)
        
        # 実際の残高を確認
        coin_balance_result = session.get_wallet_balance(
            accountType="UNIFIED",
            coin=spot_symbol.replace('USDT', '')
        )
        
        actual_spot_qty = 0
        if coin_balance_result['retCode'] == 0:
            for coin_info in coin_balance_result['result']['list']:
                for coin in coin_info['coin']:
                    if coin['coin'] == spot_symbol.replace('USDT', ''):
                        actual_spot_qty = float(coin['walletBalance'])
                        break
        
        log_message(f"Final position - Futures: {futures_qty}, Spot: {actual_spot_qty}")
        
        # 注文の確認
        if futures_order['retCode'] == 0 and spot_order['retCode'] == 0:
            log_message(f"Successfully opened new position: {linear_symbol}/{spot_symbol}")
            
            # ポジション情報を更新
            current_position['coin'] = spot_symbol.replace('USDT', '')
            current_position['qty'] = str(futures_qty)  # 先物数量を基準とする
            current_position['linear_symbol'] = linear_symbol
            current_position['spot_symbol'] = spot_symbol
            current_position['fr'] = current_fr
            current_position['entry_time'] = time.time()
            current_position['fr_change_count'] = 0
            current_position['entry_price'] = futures_price
            current_position['liquidation_price'] = None  # 後で取得
            
            # ポジション情報をconfig.iniに保存
            save_position_to_config(current_position)
            
            return True
        else:
            log_message(f"Orders status - Spot: {spot_order['retCode']}, Futures: {futures_order['retCode']}")
            return False
        
    except Exception as e:
        log_message(f"Error executing arbitrage: {str(e)}")
        return False

def save_position_to_config(position, config_file='config/config.ini'):
    """ポジション情報をconfigに保存"""
    config = configparser.ConfigParser()
    config.read(config_file, encoding='utf-8')  # UTF-8エンコーディング指定
    
    if 'POSITION' not in config:
        config['POSITION'] = {}
    
    if position['coin']:
        config['POSITION']['coin'] = position['coin']
        config['POSITION']['qty'] = position['qty']
        config['POSITION']['linear_symbol'] = position['linear_symbol']
        config['POSITION']['spot_symbol'] = position['spot_symbol']
        config['POSITION']['fr'] = str(position['fr'])
        config['POSITION']['entry_time'] = str(position['entry_time'])
        config['POSITION']['fr_change_count'] = str(position['fr_change_count'])
        config['POSITION']['entry_price'] = str(position['entry_price']) if position['entry_price'] else ''
        config['POSITION']['liquidation_price'] = str(position['liquidation_price']) if position['liquidation_price'] else ''
    else:
        # ポジションがない場合は空にする
        for key in ['coin', 'qty', 'linear_symbol', 'spot_symbol', 'fr', 'entry_time', 'fr_change_count', 'entry_price', 'liquidation_price']:
            config['POSITION'][key] = ''
    
    with open(config_file, 'w', encoding='utf-8') as configfile:  # UTF-8エンコーディング指定
        config.write(configfile)

def load_position_from_config(config_file='config/config.ini'):
    """configからポジション情報を読み込む"""
    global current_position
    
    config = configparser.ConfigParser()
    config.read(config_file, encoding='utf-8')  # UTF-8エンコーディング指定
    
    if 'POSITION' in config:
        if config['POSITION'].get('coin', ''):
            current_position['coin'] = config['POSITION']['coin']
            current_position['qty'] = config['POSITION']['qty']
            current_position['linear_symbol'] = config['POSITION']['linear_symbol']
            current_position['spot_symbol'] = config['POSITION']['spot_symbol']
            current_position['fr'] = float(config['POSITION']['fr']) if config['POSITION']['fr'] else None
            current_position['entry_time'] = float(config['POSITION']['entry_time']) if config['POSITION']['entry_time'] else None
            current_position['fr_change_count'] = int(config['POSITION']['fr_change_count']) if config['POSITION']['fr_change_count'] else 0
            current_position['entry_price'] = float(config['POSITION']['entry_price']) if config['POSITION'].get('entry_price') else None
            current_position['liquidation_price'] = float(config['POSITION']['liquidation_price']) if config['POSITION'].get('liquidation_price') else None
            return True
    
    return False

def close_current_position(config, force_close=False):
    """現在のポジションをクローズ"""
    global current_position
    
    # ポジションがない場合は何もしない
    if not current_position['coin']:
        return True
    
    # force_close=Falseかつ固定ペアの場合はクローズしない（通常のFR変動時）
    if not force_close:
        log_message(f"Closing position triggered for {current_position['linear_symbol']}/{current_position['spot_symbol']} - but maintaining position as per fixed pair strategy")
        return True
    
    # force_close=Trueの場合は実際にクローズする（システム停止時または緊急時）
    session = get_session(config)
    
    try:
        log_message(f"Force closing position for {current_position['linear_symbol']}/{current_position['spot_symbol']}")
        
        # すべての注文をキャンセル
        try:
            session.cancel_all_orders(category="linear", settleCoin="USDT")
            session.cancel_all_orders(category="spot", settleCoin="USDT")
        except Exception as e:
            log_message(f"Warning: Could not cancel all orders: {str(e)}")
        
        # 先物ポジションを確認してクローズ
        futures_positions = session.get_positions(
            category="linear",
            symbol=current_position['linear_symbol']
        )
        
        futures_closed = False
        
        if futures_positions['retCode'] == 0 and len(futures_positions['result']['list']) > 0:
            for position in futures_positions['result']['list']:
                if float(position['size']) != 0:  # ポジションサイズが0でなければクローズ
                    log_message(f"Closing futures position for {current_position['linear_symbol']}, size: {position['size']}")
                    futures_close_order = session.place_order(
                        category="linear",
                        symbol=current_position['linear_symbol'],
                        side="Buy",  # SellポジションをクローズするのでサイドはBuy
                        orderType="Market",
                        qty=position['size'],
                        reduceOnly=True
                    )
                    if futures_close_order['retCode'] == 0:
                        log_message(f"Successfully closed futures position for {current_position['linear_symbol']}")
                        futures_closed = True
                    else:
                        log_message(f"Failed to close futures position: {futures_close_order['retMsg']}")
        
        # 少し待機して確実に注文が処理されるようにする
        time.sleep(2)
        
        # 現物の残高を確認して売却
        coin_balance_result = session.get_wallet_balance(
            accountType="UNIFIED",
            coin=current_position['coin']
        )
        
        if coin_balance_result['retCode'] == 0:
            for coin_info in coin_balance_result['result']['list']:
                for coin in coin_info['coin']:
                    if coin['coin'] == current_position['coin'] and float(coin['walletBalance']) > 0:
                        # 現物の残高が残っている場合
                        available_balance = float(coin['walletBalance'])
                        
                        # 現物銘柄の情報を取得
                        spot_info = get_instrument_info(session, "spot", current_position['spot_symbol'])
                        
                        if spot_info:
                            spot_min_order_qty = Decimal(spot_info['lotSizeFilter']['minOrderQty'])
                            spot_qty_step = Decimal(spot_info['lotSizeFilter'].get('basePrecision', '0.000001'))
                            
                            # 売却数量を調整
                            sell_qty = Decimal(str(available_balance))
                            sell_qty = (sell_qty / spot_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * spot_qty_step
                            
                            if sell_qty >= spot_min_order_qty:
                                log_message(f"Selling spot position for {current_position['spot_symbol']}, qty: {sell_qty}")
                                spot_sell_order = session.place_order(
                                    category="spot",
                                    symbol=current_position['spot_symbol'],
                                    side="Sell",
                                    orderType="Market",
                                    qty=str(sell_qty)
                                )
                                
                                if spot_sell_order['retCode'] == 0:
                                    log_message(f"Successfully sold spot position for {current_position['spot_symbol']}")
                                else:
                                    log_message(f"Failed to sell spot position: {spot_sell_order['retMsg']}")
                            else:
                                log_message(f"Spot position too small to sell: {sell_qty} < {spot_min_order_qty}")
        else:
            log_message(f"Failed to get coin balance: {coin_balance_result['retMsg']}")
        
        # ポジション情報をクリア
        current_position['coin'] = None
        current_position['qty'] = None
        current_position['linear_symbol'] = None
        current_position['spot_symbol'] = None
        current_position['fr'] = None
        current_position['entry_time'] = None
        current_position['fr_change_count'] = 0
        current_position['entry_price'] = None
        current_position['liquidation_price'] = None
        
        # ポジション情報をconfig.iniから削除
        save_position_to_config(current_position)
        
        log_message("All positions closed successfully")
        return True
    
    except Exception as e:
        log_message(f"Error closing position: {str(e)}")
        return False

def check_current_position(config):
    """現在のポジションをチェック（固定ペアでは永続保持）"""
    global current_position
    
    # ポジションがない場合は何もしない
    if not current_position['coin']:
        return
    
    session = get_session(config)
    
    try:
        # 現在のFRを取得
        ticker = session.get_tickers(
            category="linear", 
            symbol=current_position['linear_symbol']
        )
        
        if ticker['retCode'] == 0 and 'fundingRate' in ticker['result']['list'][0]:
            new_fr = float(ticker['result']['list'][0]['fundingRate']) * 100  # パーセント表示
            
            # FRを更新
            current_position['fr'] = new_fr
            
            # 累積FRを計算
            cumulative_fr = calculate_cumulative_fr(session, current_position['linear_symbol'])
            
            log_message(f"Current pair {current_position['linear_symbol']}/{current_position['spot_symbol']} - FR: {new_fr:.4f}%, Cumulative FR: {cumulative_fr:.4f}%")
            
            # 清算価格情報を更新
            liq_info = get_liquidation_price(config, current_position['linear_symbol'])
            if liq_info:
                current_position['liquidation_price'] = liq_info['liquidation_price']
                log_message(f"Position status - Mark: ${liq_info['mark_price']:.4f}, Liquidation: ${liq_info['liquidation_price']:.4f}, Distance: {liq_info['distance_percent']:.2f}%")
            
            # ポジション情報を保存
            save_position_to_config(current_position)
            
            # 固定ペアでは永続保持（FRがマイナスでもクローズしない）
            log_message(f"Keeping current position. FR: {new_fr:.4f}% (Fixed pair strategy - permanent hold)")
        else:
            log_message(f"Failed to get funding rate for {current_position['linear_symbol']}")
    
    except Exception as e:
        log_message(f"Error checking position: {str(e)}")

def check_position_balance(config):
    """スポットと先物のポジションバランスをチェックして調整"""
    global current_position
    
    # ポジションがない場合は何もしない
    if not current_position['coin']:
        return
    
    session = get_session(config)
    
    try:
        # 先物ポジションを確認
        futures_positions = session.get_positions(
            category="linear",
            symbol=current_position['linear_symbol']
        )
        
        futures_qty = 0
        if futures_positions['retCode'] == 0 and len(futures_positions['result']['list']) > 0:
            for position in futures_positions['result']['list']:
                if position['side'] == 'Sell':  # 売りポジションを確認
                    futures_qty = float(position['size'])
                    break
        
        # 現物残高を確認
        coin_balance_result = session.get_wallet_balance(
            accountType="UNIFIED",
            coin=current_position['coin']
        )
        
        spot_qty = 0
        if coin_balance_result['retCode'] == 0:
            for coin_info in coin_balance_result['result']['list']:
                for coin in coin_info['coin']:
                    if coin['coin'] == current_position['coin']:
                        spot_qty = float(coin['walletBalance'])
                        break
        
        log_message(f"Position balance check - Futures: {futures_qty}, Spot: {spot_qty}")
        
        # 基本的にはバランス調整はしない（固定ペア戦略）
        if futures_qty > 0 and spot_qty > 0:
            log_message(f"Position balance confirmed - maintaining both positions")
        elif futures_qty > 0 and spot_qty == 0:
            log_message(f"Warning: Futures position exists but no spot position found")
        elif futures_qty == 0 and spot_qty > 0:
            log_message(f"Warning: Spot position exists but no futures position found")
        else:
            log_message(f"No positions found in balance check")
    
    except Exception as e:
        log_message(f"Error checking position balance: {str(e)}")

def get_next_funding_time(config):
    """次のFunding時間までの待機時間を計算"""
    tokyo_tz = pytz.timezone('Asia/Tokyo')
    now = datetime.now(tokyo_tz)
    
    # 設定から時間を取得
    funding_times_str = config['FUNDING']['funding_times']
    funding_times = [datetime.strptime(t.strip(), "%H:%M").time() for t in funding_times_str.split(',')]
    funding_times.sort()
    
    # 現在の日付と次の日付の全ての時間を生成
    all_funding_times = []
    for t in funding_times:
        all_funding_times.append(tokyo_tz.localize(datetime.combine(now.date(), t)))
        all_funding_times.append(tokyo_tz.localize(datetime.combine(now.date() + timedelta(days=1), t)))
    
    # 現在より後の最も近い時間を見つける
    next_funding = min((t for t in all_funding_times if t > now), key=lambda x: x - now)
    
    wait_time = (next_funding - now).total_seconds()
    return wait_time, next_funding

def arbitrage_loop(config):
    """アービトラージループのメイン関数（固定ペア用）"""
    global is_arbitrage_running, current_position
    
    log_message("Starting arbitrage loop (Fixed ROSEUSDT strategy)")
    
    # 保存されたポジション情報を読み込む
    load_position_from_config()
    
    while is_arbitrage_running.is_set():
        # 次のFR時間までの待機時間を計算
        wait_time, next_fr_time = get_next_funding_time(config)
        
        # ポジションがある場合
        if current_position['coin']:
            # FR変換時にポジションチェック
            log_message(f"Waiting for {wait_time:.2f} seconds until next funding time at {next_fr_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 待機
            start_time = time.time()
            while time.time() - start_time < wait_time and is_arbitrage_running.is_set():
                time.sleep(1)
            
            # FR変換時間になったらチェック（システムが実行中であれば）
            if is_arbitrage_running.is_set():
                log_message(f"Funding time reached: {next_fr_time.strftime('%Y-%m-%d %H:%M:%S')}")
                # FR更新カウントを増やす
                current_position['fr_change_count'] += 1
                # 更新したポジション情報をconfig.iniに保存
                save_position_to_config(current_position)
                # ポジションチェック（固定ペアでは永続保持）
                check_current_position(config)
        
        # ポジションがない場合は新しいポジションを開く（固定ペア）
        else:
            # UI用の演出のため、上位の機会を取得して表示
            opportunities = get_top_arbitrage_opportunities(config, top_n=3)
            
            if opportunities:
                # 上位の機会をログ出力（UI演出用）
                opp_str = ", ".join([
                    f"{o['linear_symbol']} (FR: {o['current_fr']:.4f}%, Cum: {o['cumulative_fr']:.4f}%)"
                    for o in opportunities
                ])
                log_message(f"Top opportunities: {opp_str}")
            
            # 実際には固定ペア（ROSEUSDT）で実行
            log_message(f"Attempting arbitrage: Fixed pair ROSEUSDT strategy")
            
            if execute_arbitrage_fixed(config):
                # 初回ポジション作成後にポジションバランスをチェック
                log_message("Initial position opened, checking balance")
                check_position_balance(config)
            else:
                log_message("Failed to execute arbitrage for ROSEUSDT")
                
                # 次のFR時間まで待機
                log_message(f"Waiting for {wait_time:.2f} seconds until next funding time at {next_fr_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # 待機
                start_time = time.time()
                while time.time() - start_time < wait_time and is_arbitrage_running.is_set():
                    time.sleep(1)
    
    # ループ終了時の処理（固定ペアでは通常はクローズしない）
    if current_position['coin']:
        log_message("System stopped - maintaining position as per fixed pair strategy")
    
    log_message("Arbitrage loop ended")

def start_arbitrage(config):
    """アービトラージを開始"""
    global is_arbitrage_running, arbitrage_thread, liquidation_monitor_thread
    
    if not is_arbitrage_running.is_set():
        is_arbitrage_running.set()
        
        # メインのアービトラージスレッド開始
        arbitrage_thread = threading.Thread(target=arbitrage_loop, args=(config,))
        arbitrage_thread.daemon = True
        arbitrage_thread.start()
        
        # 清算監視スレッド開始
        liquidation_monitor_thread = threading.Thread(target=liquidation_monitor_loop, args=(config,))
        liquidation_monitor_thread.daemon = True
        liquidation_monitor_thread.start()
        
        log_message("System started (Fixed ROSEUSDT strategy with liquidation monitoring)")
        return True
    return False

def stop_arbitrage(force_close_positions=False):
    """アービトラージを停止"""
    global is_arbitrage_running, arbitrage_thread, liquidation_monitor_thread
    
    if is_arbitrage_running.is_set():
        is_arbitrage_running.clear()
        
        # メインスレッドの停止を待つ
        if arbitrage_thread and arbitrage_thread.is_alive():
            arbitrage_thread.join(timeout=30)  # 最大30秒待機
        
        # 清算監視スレッドの停止を待つ
        if liquidation_monitor_thread and liquidation_monitor_thread.is_alive():
            liquidation_monitor_thread.join(timeout=30)  # 最大30秒待機
        
        # force_close_positions=Trueの場合はポジションを強制クローズ
        if force_close_positions:
            config = load_config()
            if close_current_position(config, force_close=True):
                log_message("System stopped (All positions closed)")
            else:
                log_message("System stopped (Warning: Position close failed)")
        else:
            log_message("System stopped (Position maintained)")
        
        return True
    return False

def is_api_configured(config):
    """API設定が完了しているかチェック"""
    return config['API']['key'] and config['API']['secret']

def save_config(config, config_file='config/config.ini'):
    """設定を保存"""
    with open(config_file, 'w', encoding='utf-8') as configfile:  # UTF-8エンコーディング指定
        config.write(configfile)

def get_wallet_balance(config, coin="USDT"):
    """指定した通貨の残高を取得"""
    session = get_session(config)
    try:
        balance = session.get_wallet_balance(accountType="UNIFIED")
        coin_balance = next((c['walletBalance'] for c in balance['result']['list'][0]['coin'] if c['coin'] == coin), '0')
        return float(coin_balance)
    except Exception as e:
        log_message(f"Error getting wallet balance for {coin}: {str(e)}")
        return 0.0

def get_current_liquidation_info(config):
    """現在のポジションの清算情報を取得（API用）"""
    global current_position
    
    if not current_position['linear_symbol']:
        return {
            'has_position': False,
            'liquidation_price': None,
            'mark_price': None,
            'distance_percent': None,
            'unrealized_pnl': None
        }
    
    liq_info = get_liquidation_price(config, current_position['linear_symbol'])
    
    if liq_info:
        return {
            'has_position': True,
            'liquidation_price': liq_info['liquidation_price'],
            'mark_price': liq_info['mark_price'],
            'distance_percent': liq_info['distance_percent'],
            'unrealized_pnl': liq_info['unrealized_pnl'],
            'size': liq_info['size'],
            'side': liq_info['side']
        }
    else:
        return {
            'has_position': True,
            'liquidation_price': None,
            'mark_price': None,
            'distance_percent': None,
            'unrealized_pnl': None
        }

def manual_emergency_rebalance(config):
    """手動での緊急リバランス実行（API用）"""
    global current_position
    
    if not current_position['linear_symbol']:
        return False, "No position to rebalance"
    
    try:
        log_message("Manual emergency rebalance requested")
        
        if emergency_rebalance(config):
            return True, "Emergency rebalance completed successfully"
        else:
            return False, "Emergency rebalance failed"
    except Exception as e:
        log_message(f"Error in manual emergency rebalance: {str(e)}")
        return False, f"Error: {str(e)}"