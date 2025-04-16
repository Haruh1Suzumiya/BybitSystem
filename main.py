import os
from flask import Flask
from app.routes import main_bp
from app import core

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.urandom(24)

    # Load configuration
    config = core.load_config()
    app.config.update(config)

    # Setup logging
    core.setup_logging(app.config['SYSTEM']['log_level'])

    # Register blueprints
    app.register_blueprint(main_bp)

    return app

if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', app.config['SYSTEM'].get('port', 8000)))
    app.run(debug=app.config['SYSTEM']['debug_mode'] == 'true', host='0.0.0.0', port=port)