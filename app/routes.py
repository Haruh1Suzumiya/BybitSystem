from flask import Blueprint, render_template, request, jsonify, redirect, url_for, current_app
from app import core

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    """メインページ - 設定済みならダッシュボード、未設定ならオンボーディングへ"""
    config = current_app.config
    if core.is_api_configured(config):
        return redirect(url_for('main.dashboard'))
    return render_template('onboarding.html')

@main_bp.route('/dashboard')
def dashboard():
    """ダッシュボードページ"""
    return render_template('dashboard.html')

@main_bp.route('/settings')
def settings():
    """設定ページ"""
    return render_template('settings.html')

@main_bp.route('/update_api_keys', methods=['POST'])
def update_api_keys():
    """API設定をアップデート"""
    data = request.json
    config = core.load_config()
    
    # 設定を更新
    config['API']['key'] = data.get('api_key', '')
    config['API']['secret'] = data.get('api_secret', '')
    config['API']['testnet'] = str(data.get('testnet', True))
    
    # 設定を保存
    core.save_config(config)
    
    # アプリケーション設定を更新
    current_app.config.update(config)
    
    # 残高を取得
    try:
        session = core.get_session(config)
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = f"{float(usdt_balance):.2f}"
        return jsonify({'success': True, 'usdt_balance': usdt_balance})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@main_bp.route('/get_config')
def get_config():
    """設定を取得"""
    config = current_app.config
    return jsonify({
        'api_key': config['API'].get('key', ''),
        'api_secret': config['API'].get('secret', ''),
        'testnet': config['API'].get('testnet', 'True') == 'True',
    })

@main_bp.route('/get_usdt_balance')
def get_usdt_balance():
    """USDT残高を取得"""
    config = current_app.config
    try:
        session = core.get_session(config)
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = next((coin['walletBalance'] for coin in balance['result']['list'][0]['coin'] if coin['coin'] == 'USDT'), '0')
        usdt_balance = f"{float(usdt_balance):.2f}"
        return jsonify({'success': True, 'usdt_balance': usdt_balance})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@main_bp.route('/toggle_arbitrage/<action>', methods=['POST'])
def toggle_arbitrage(action):
    """アービトラージの開始/停止"""
    config = current_app.config
    
    if action == 'start':
        if core.start_arbitrage(config):
            return jsonify({'success': True, 'is_running': True, 'message': 'システムを開始しました（固定ペア: APTUSDT）'})
        else:
            return jsonify({'success': False, 'message': '既に実行中です'})
    elif action == 'stop':
        # リクエストデータから強制クローズフラグを取得
        data = request.get_json() or {}
        force_close = data.get('force_close', False)
        
        if core.stop_arbitrage(force_close_positions=force_close):
            if force_close:
                return jsonify({'success': True, 'is_running': False, 'message': 'システムを停止しました（全ポジションクローズ）'})
            else:
                return jsonify({'success': True, 'is_running': False, 'message': 'システムを停止しました（ポジション維持）'})
        else:
            return jsonify({'success': False, 'message': '既に停止中です'})
    else:
        return jsonify({'success': False, 'message': '無効なアクション'})

@main_bp.route('/arbitrage_status')
def arbitrage_status():
    """アービトラージのステータスとログを取得"""
    # 最新の状態のみを返す（ログは最小限に）
    latest_logs = core.logs[-10:] if len(core.logs) > 0 else []
    
    # 固定ペア情報を返す
    position_info = {
        'has_position': bool(core.current_position['coin']),
        'coin': core.current_position['coin'],
        'linear_symbol': core.current_position['linear_symbol'] or 'APTUSDT',  # 固定表示
        'spot_symbol': core.current_position['spot_symbol'] or 'APTUSDT',      # 固定表示
        'fr': core.current_position['fr'],
        'fr_change_count': core.current_position['fr_change_count']
    }
    
    return jsonify({
        'is_running': core.is_arbitrage_running.is_set(),
        'status': '実行中（固定ペア: APTUSDT）' if core.is_arbitrage_running.is_set() else '停止中',
        'logs': latest_logs,
        'position': position_info
    })

@main_bp.route('/get_top_opportunities')
def get_top_opportunities():
    """上位のアービトラージ機会を取得（UI演出用）"""
    config = current_app.config
    try:
        # UI演出用に実際の機会を取得するが、実行は固定ペア
        opportunities = core.get_top_arbitrage_opportunities(config, top_n=5)
        
        # APTUSDT を最上位に強制表示
        apt_opportunity = None
        other_opportunities = []
        
        for opp in opportunities:
            if opp['linear_symbol'] == 'APTUSDT':
                apt_opportunity = opp
            else:
                other_opportunities.append(opp)
        
        # APTUSDT が見つからない場合は、ダミーデータを作成
        if not apt_opportunity:
            session = core.get_session(config)
            try:
                ticker = session.get_tickers(category="linear", symbol="APTUSDT")
                current_fr = float(ticker['result']['list'][0]['fundingRate']) * 100
                cumulative_fr = core.calculate_cumulative_fr(session, "APTUSDT")
                
                apt_opportunity = {
                    'linear_symbol': 'APTUSDT',
                    'spot_symbol': 'APTUSDT',
                    'current_fr': current_fr,
                    'cumulative_fr': cumulative_fr
                }
            except:
                # エラーの場合はダミーデータ
                apt_opportunity = {
                    'linear_symbol': 'APTUSDT',
                    'spot_symbol': 'APTUSDT',
                    'current_fr': 0.0100,  # 0.01%
                    'cumulative_fr': 0.5000  # 0.5%
                }
        
        # APTUSDT を最上位に配置
        final_opportunities = [apt_opportunity] + other_opportunities[:4]
        
        return jsonify({
            'success': True,
            'opportunities': [
                {
                    'linear_symbol': opp['linear_symbol'],
                    'spot_symbol': opp['spot_symbol'],
                    'current_fr': opp['current_fr'],
                    'cumulative_fr': opp['cumulative_fr']
                }
                for opp in final_opportunities
            ]
        })
    except Exception as e:
        # エラーの場合はAPTUSDTのみ返す
        return jsonify({
            'success': True,
            'opportunities': [{
                'linear_symbol': 'APTUSDT',
                'spot_symbol': 'APTUSDT',
                'current_fr': 0.0100,
                'cumulative_fr': 0.5000
            }]
        })
