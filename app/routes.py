from flask import Blueprint, render_template, request, jsonify, redirect, url_for, current_app
import pytz
from datetime import datetime, timedelta
from app.utils import logs as global_logs
from app.utils import (
    get_session, log_message, find_arbitrage_opportunities, execute_arbitrage,
    is_api_configured, get_next_funding_time, cancel_all_orders_and_close_positions, load_config,
    arbitrage_loop
)
import threading
import time

main = Blueprint('main', __name__)

is_arbitrage_running = threading.Event()
arbitrage_thread = None

@main.route('/')
def index():
    config = current_app.config
    if is_api_configured(config):
        return redirect(url_for('main.dashboard'))
    return render_template('onboarding.html')

@main.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@main.route('/settings')
def settings():
    return render_template('settings.html')

@main.route('/update_api_keys', methods=['POST'])
def update_api_keys():
    data = request.json
    config = load_config()
    config['API']['key'] = data['api_key']
    config['API']['secret'] = data['api_secret']
    config['API']['testnet'] = str(data['testnet'])
    
    with open('config/config.ini', 'w') as configfile:
        config.write(configfile)
    
    current_app.config.update(config)
    
    try:
        session = get_session(config)
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = f"{float(usdt_balance):.2f}"  # 小数点以下2桁に制限
        return jsonify({'success': True, 'usdt_balance': usdt_balance})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@main.route('/get_config')
def get_config():
    config = current_app.config
    return jsonify({
        'api_key': config['API']['key'],
        'api_secret': config['API']['secret'],
        'testnet': config['API']['testnet'],
    })

@main.route('/get_usdt_balance')
def get_usdt_balance():
    config = current_app.config
    try:
        session = get_session(config)
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = f"{float(usdt_balance):.2f}"  # 小数点以下2桁に制限
        return jsonify({'success': True, 'usdt_balance': usdt_balance})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@main.route('/toggle_arbitrage/<action>', methods=['POST'])
def toggle_arbitrage(action):
    global is_arbitrage_running, arbitrage_thread
    config = current_app.config
    print(f"Toggle arbitrage called with action: {action}")  # デバッグログ
    
    if action == 'start' and not is_arbitrage_running.is_set():
        print("Starting arbitrage")  # デバッグログ
        is_arbitrage_running.set()
        arbitrage_thread = threading.Thread(target=arbitrage_loop, args=(config, is_arbitrage_running))
        arbitrage_thread.start()
        log_message("システムを開始しました")
        return jsonify({'success': True, 'is_running': True, 'message': 'システムを開始しました'})
    elif action == 'stop' and is_arbitrage_running.is_set():
        print("Stopping arbitrage")  # デバッグログ
        is_arbitrage_running.clear()
        if arbitrage_thread:
            arbitrage_thread.join()
        cancel_all_orders_and_close_positions(config)
        log_message("システムを停止しました")
        return jsonify({'success': True, 'is_running': False, 'message': 'システムを停止しました'})
    else:
        print(f"Invalid action: {action}")  # デバッグログ
        return jsonify({'success': False, 'message': '無効なアクション'})

@main.route('/arbitrage_status')
def arbitrage_status():
    global is_arbitrage_running, global_logs
    simplified_logs = [log for log in global_logs if log is not None]
    return jsonify({
        'is_running': is_arbitrage_running.is_set(),
        'status': '実行中' if is_arbitrage_running.is_set() else '停止中',
        'logs': simplified_logs
    })