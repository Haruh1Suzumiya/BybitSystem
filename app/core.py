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
current_position = {
    'coin': None,
    'qty': None,
    'linear_symbol': None,
    'spot_symbol': None,
    'fr': None,
    'entry_time': None,
    'fr_change_count': 0
}

# 固定ペア設定
FIXED_SPOT_SYMBOL = "ZKJUSDT"
FIXED_LINEAR_SYMBOL = "ZKJUSDT"

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
    
    # 設定を保存
    with open(config_file, 'w') as configfile:
        config.write(configfile)
    
    return config

def setup_logging(log_level):
    """ロギング設定"""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('app.log')
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
    """ログメッセージを記録"""
    global logs
    print(f"LOG: {message}")
    logging.info(message)
    
    # ログをシンプル化して保存
    simplified_message = simplify_message(message)
    if simplified_message:
        logs.append(simplified_message)
        if len(logs) > 100:
            logs.pop(0)
    else:
        logs.append(message)
        if len(logs) > 100:
            logs.pop(0)

def simplify_message(message):
    """ログメッセージを分かりやすく整形"""
    # ポジションオープン
    if "Successfully opened new position:" in message:
        return f"🚀 新規ポジション: ZKJUSDT"
    
    # 現在のペア情報
    elif "Current pair" in message:
        match = re.search(r"Current pair (\w+)/(\w+) - FR: ([\d.-]+)%, Cumulative FR: ([\d.-]+)%", message)
        if match:
            fr = float(match.group(3))
            cumulative_fr = float(match.group(4))
            return f"📊 ZKJUSDT: 現在FR {fr:.4f}%, 累積FR {cumulative_fr:.4f}%"
    
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
        return f"💡 試行: ZKJUSDT"
    
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

def execute_arbitrage_fixed(config):
    """固定ペア（ZKJUSDT）でアービトラージを実行"""
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
        
        # レバレッジ設定
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
        
        # 取引額を決定 (残高の40%ずつを使用、合計80%まで)
        trade_amount_per_side = usdt_balance * 0.48
        
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
        
        # 現物の注文量を先物と同じにする（コインの数量）
        spot_qty = futures_qty
        
        # 実際の注文価値を計算（チェック用）
        spot_order_value = float(spot_qty) * spot_price
        futures_order_value = float(futures_qty) * futures_price
        
        min_order_value = 10.0
        if spot_order_value < min_order_value:
            log_message(f"Spot order value too small: {spot_order_value:.2f} USDT < {min_order_value} USDT")
            return False
        
        log_message(f"Placing market orders for {spot_symbol} and {linear_symbol}")
        log_message(f"Order details - Qty: {spot_qty}, Spot Value: {spot_order_value:.2f} USDT, Futures Value: {futures_order_value:.2f} USDT")
        
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
            return False
        
        # 先物注文が成功したら現物買い注文
        spot_order = session.place_order(
            category="spot",
            symbol=spot_symbol,
            side="Buy",
            orderType="Market",
            qty=str(spot_qty)
        )
        
        if spot_order['retCode'] != 0:
            log_message(f"Failed to place spot order: {spot_order['retMsg']}")
            # 先物ポジションをクローズ
            session.place_order(
                category="linear",
                symbol=linear_symbol,
                side="Buy",
                orderType="Market",
                qty=str(futures_qty),
                reduceOnly=True
            )
            return False
        
        # 注文が確定するまで少し待機
        time.sleep(2)
        
        # 現物の実際の残高を確認
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
        
        # 現物数量が先物数量と一致しない場合、追加注文を試みる
        max_retry = 3  # 最大再試行回数
        retry_count = 0
        
        while actual_spot_qty < float(spot_qty) * 0.97 and retry_count < max_retry:  # 3%の許容範囲
            missing_qty = float(spot_qty) - actual_spot_qty
            if missing_qty > 0:
                log_message(f"Spot quantity mismatch. Got: {actual_spot_qty}, Expected: {spot_qty}. Placing additional order for {missing_qty}")
                
                # 追加注文の価値を計算
                additional_order_value = missing_qty * spot_price
                
                # 最小注文量と注文ステップを考慮
                adjusted_missing_qty = (Decimal(str(missing_qty)) / linear_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * linear_qty_step
                
                # 最小注文価値をチェック
                if additional_order_value < min_order_value:
                    # 最小注文価値を満たす量に調整
                    min_qty_for_value = Decimal(str(min_order_value / spot_price))
                    adjusted_missing_qty = max(
                        linear_min_order_qty,
                        (min_qty_for_value / linear_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * linear_qty_step
                    )
                    log_message(f"Adjusting order to meet minimum value: {float(adjusted_missing_qty) * spot_price:.2f} USDT")
                
                # 追加注文が小さすぎる場合はスキップ
                if adjusted_missing_qty < linear_min_order_qty:
                    log_message(f"Missing quantity too small for additional order: {adjusted_missing_qty} < {linear_min_order_qty}")
                    break
                
                additional_spot_order = session.place_order(
                    category="spot",
                    symbol=spot_symbol,
                    side="Buy",
                    orderType="Market",
                    qty=str(adjusted_missing_qty)
                )
                
                if additional_spot_order['retCode'] != 0:
                    log_message(f"Failed to place additional spot order: {additional_spot_order['retMsg']}")
                    
                    # 特定のエラーメッセージに基づいて対応
                    if "Order value exceeded lower limit" in additional_spot_order['retMsg']:
                        import re
                        match = re.search(r'lower limit is (\d+\.?\d*)', additional_spot_order['retMsg'])
                        if match:
                            min_limit = float(match.group(1))
                            new_qty = Decimal(str(min_limit / spot_price))
                            better_qty = max(linear_min_order_qty, 
                                            (new_qty / linear_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * linear_qty_step)
                            
                            log_message(f"Retrying with quantity to meet exchange minimum: {better_qty}")
                            retry_order = session.place_order(
                                category="spot",
                                symbol=spot_symbol,
                                side="Buy",
                                orderType="Market",
                                qty=str(better_qty)
                            )
                            
                            if retry_order['retCode'] == 0:
                                log_message(f"Additional order with adjusted qty successful")
                            else:
                                log_message(f"Still failed after adjustment: {retry_order['retMsg']}")
                else:
                    log_message(f"Additional spot order placed successfully for {adjusted_missing_qty}")
                    
                    # 再度残高を確認
                    time.sleep(2)
                    updated_balance = session.get_wallet_balance(
                        accountType="UNIFIED",
                        coin=spot_symbol.replace('USDT', '')
                    )
                    
                    if updated_balance['retCode'] == 0:
                        for coin_info in updated_balance['result']['list']:
                            for coin in coin_info['coin']:
                                if coin['coin'] == spot_symbol.replace('USDT', ''):
                                    actual_spot_qty = float(coin['walletBalance'])
                                    break
                    
                    log_message(f"Updated spot quantity: {actual_spot_qty}")
            
            retry_count += 1
        
        # 最終的な現物数量と先物数量を表示
        log_message(f"Final position - Futures: {futures_qty}, Spot: {actual_spot_qty}")
        
        # 注文の確認
        if futures_order['retCode'] == 0 and spot_order['retCode'] == 0:
            log_message(f"Successfully opened new position: {linear_symbol}/{spot_symbol}")
            
            # ポジション情報を更新
            current_position['coin'] = spot_symbol.replace('USDT', '')
            current_position['qty'] = str(spot_qty)  # 理想的な数量を保存
            current_position['linear_symbol'] = linear_symbol
            current_position['spot_symbol'] = spot_symbol
            current_position['fr'] = current_fr
            current_position['entry_time'] = time.time()
            current_position['fr_change_count'] = 0
            
            # ポジション情報をconfig.iniに保存
            save_position_to_config(current_position)
            
            return True
        else:
            log_message(f"Failed to place orders: Spot {spot_order['retMsg']}, Futures {futures_order['retMsg']}")
            return False
        
    except Exception as e:
        log_message(f"Error executing arbitrage: {str(e)}")
        return False

def save_position_to_config(position, config_file='config/config.ini'):
    """ポジション情報をconfigに保存"""
    config = configparser.ConfigParser()
    config.read(config_file)
    
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
    else:
        # ポジションがない場合は空にする
        for key in ['coin', 'qty', 'linear_symbol', 'spot_symbol', 'fr', 'entry_time', 'fr_change_count']:
            config['POSITION'][key] = ''
    
    with open(config_file, 'w') as configfile:
        config.write(configfile)

def load_position_from_config(config_file='config/config.ini'):
    """configからポジション情報を読み込む"""
    global current_position
    
    config = configparser.ConfigParser()
    config.read(config_file)
    
    if 'POSITION' in config:
        if config['POSITION'].get('coin', ''):
            current_position['coin'] = config['POSITION']['coin']
            current_position['qty'] = config['POSITION']['qty']
            current_position['linear_symbol'] = config['POSITION']['linear_symbol']
            current_position['spot_symbol'] = config['POSITION']['spot_symbol']
            current_position['fr'] = float(config['POSITION']['fr']) if config['POSITION']['fr'] else None
            current_position['entry_time'] = float(config['POSITION']['entry_time']) if config['POSITION']['entry_time'] else None
            current_position['fr_change_count'] = int(config['POSITION']['fr_change_count']) if config['POSITION']['fr_change_count'] else 0
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
    
    # force_close=Trueの場合は実際にクローズする（システム停止時）
    session = get_session(config)
    
    try:
        log_message(f"Force closing position for {current_position['linear_symbol']}/{current_position['spot_symbol']}")
        
        # すべての注文をキャンセル
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        session.cancel_all_orders(category="spot", settleCoin="USDT")
        
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
                                    
                                    # 再試行（分割して売却）
                                    if spot_sell_order['retMsg'] and "insufficient balance" in spot_sell_order['retMsg'].lower():
                                        # 残高の90%で再試行
                                        retry_qty = (sell_qty * Decimal('0.9')).quantize(Decimal(str(spot_qty_step)), rounding=ROUND_DOWN)
                                        if retry_qty >= spot_min_order_qty:
                                            log_message(f"Retrying with smaller quantity: {retry_qty}")
                                            retry_order = session.place_order(
                                                category="spot",
                                                symbol=current_position['spot_symbol'],
                                                side="Sell",
                                                orderType="Market",
                                                qty=str(retry_qty)
                                            )
                                            if retry_order['retCode'] == 0:
                                                log_message(f"Successfully sold partial spot position: {retry_qty}")
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
            
            # 固定ペアでは永続保持（FRがマイナスでもクローズしない）
            log_message(f"Keeping current position. FR: {new_fr:.4f}% (Fixed pair strategy - permanent hold)")
        else:
            log_message(f"Failed to get funding rate for {current_position['linear_symbol']}")
    
    except Exception as e:
        log_message(f"Error checking position: {str(e)}")

def check_position_balance(config):
    """スポットと先物のポジションバランスをチェックして調整
    （初回ポジション開設時と組み替え時のみ実行）
    """
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
        
        if futures_qty <= 0:
            log_message(f"No futures position found for {current_position['linear_symbol']}")
            return
        
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
        
        # 差分が5%以上ある場合は調整
        if spot_qty < futures_qty * 0.95:
            missing_qty = futures_qty - spot_qty
            log_message(f"Position imbalance detected: Futures {futures_qty}, Spot {spot_qty}. Missing: {missing_qty}")
            
            # 現在の価格を取得して最小注文価値を確認
            spot_ticker = session.get_tickers(category="spot", symbol=current_position['spot_symbol'])
            spot_price = float(spot_ticker['result']['list'][0]['lastPrice'])
            
            # 現物の銘柄情報を取得
            spot_info = get_instrument_info(session, "spot", current_position['spot_symbol'])
            
            if spot_info:
                spot_min_order_qty = Decimal(spot_info['lotSizeFilter']['minOrderQty'])
                spot_qty_step = Decimal(spot_info['lotSizeFilter'].get('basePrecision', '0.000001'))
                
                # 最小注文価値（多くの取引所では5-10 USDT程度）
                min_order_value = 10.0  # 10 USDT を最小注文価値と仮定
                
                # 注文数量を調整
                adjusted_qty = Decimal(str(missing_qty))
                adjusted_qty = (adjusted_qty / spot_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * spot_qty_step
                
                # 注文価値を計算
                order_value = float(adjusted_qty) * spot_price
                
                # 注文価値が最小価値未満なら、最小価値になるよう数量を調整
                if order_value < min_order_value:
                    new_qty = Decimal(str(min_order_value / spot_price))
                    adjusted_qty = max(spot_min_order_qty, (new_qty / spot_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * spot_qty_step)
                    log_message(f"Adjusted order quantity to meet minimum value: from {missing_qty} to {adjusted_qty}")
                
                # 最小注文量を超えていれば追加注文
                if adjusted_qty >= spot_min_order_qty:
                    log_message(f"Balancing position: Adding spot position {adjusted_qty} to match futures")
                    spot_order = session.place_order(
                        category="spot",
                        symbol=current_position['spot_symbol'],
                        side="Buy",
                        orderType="Market",
                        qty=str(adjusted_qty)
                    )
                    
                    if spot_order['retCode'] == 0:
                        log_message(f"Successfully balanced position with additional spot order")
                    else:
                        log_message(f"Failed to balance position: {spot_order['retMsg']}")
                        
                        # エラーメッセージから最小注文価値を推測して再調整
                        if "Order value exceeded lower limit" in spot_order['retMsg']:
                            try:
                                import re
                                match = re.search(r'lower limit is (\d+\.?\d*)', spot_order['retMsg'])
                                if match:
                                    min_limit = float(match.group(1))
                                    new_qty = Decimal(str(min_limit / spot_price))
                                    adjusted_qty = max(spot_min_order_qty, (new_qty / spot_qty_step).quantize(Decimal('1'), rounding=ROUND_DOWN) * spot_qty_step)
                                    
                                    log_message(f"Retrying with adjusted quantity to meet minimum value: {adjusted_qty}")
                                    retry_order = session.place_order(
                                        category="spot",
                                        symbol=current_position['spot_symbol'],
                                        side="Buy",
                                        orderType="Market",
                                        qty=str(adjusted_qty)
                                    )
                                    
                                    if retry_order['retCode'] == 0:
                                        log_message(f"Successfully balanced position on retry")
                                    else:
                                        log_message(f"Failed to balance position on retry: {retry_order['retMsg']}")
                            except Exception as e:
                                log_message(f"Error adjusting order quantity: {str(e)}")
                else:
                    log_message(f"Missing quantity too small for adjustment: {adjusted_qty} < {spot_min_order_qty}")
        elif futures_qty < spot_qty * 0.95:
            # 現物が先物より多い場合はクローズ時に適切に処理されるので何もしない
            log_message(f"Spot position exceeds futures position: Futures {futures_qty}, Spot {spot_qty}")
    
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
    
    log_message("Starting arbitrage loop (Fixed ZKJUSDT strategy)")
    
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
            
            # 実際には固定ペア（ZKJUSDT）で実行
            log_message(f"Attempting arbitrage: Fixed pair ZKJUSDT strategy")
            
            if execute_arbitrage_fixed(config):
                # 初回ポジション作成後にポジションバランスをチェック
                log_message("Initial position opened, checking balance")
                check_position_balance(config)
            else:
                log_message("Failed to execute arbitrage for ZKJUSDT")
                
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
    global is_arbitrage_running, arbitrage_thread
    
    if not is_arbitrage_running.is_set():
        is_arbitrage_running.set()
        arbitrage_thread = threading.Thread(target=arbitrage_loop, args=(config,))
        arbitrage_thread.daemon = True
        arbitrage_thread.start()
        log_message("System started (Fixed ZKJUSDT strategy)")
        return True
    return False

def stop_arbitrage(force_close_positions=False):
    """アービトラージを停止"""
    global is_arbitrage_running, arbitrage_thread
    
    if is_arbitrage_running.is_set():
        is_arbitrage_running.clear()
        if arbitrage_thread and arbitrage_thread.is_alive():
            arbitrage_thread.join(timeout=30)  # 最大30秒待機
        
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
    with open(config_file, 'w') as configfile:
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
