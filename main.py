import os
from flask import Flask
from app import routes
from app.utils import load_config, setup_logging

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.urandom(24)

    # Load configuration
    config = load_config()
    app.config.update(config)

    # Setup logging
    setup_logging(app.config['SYSTEM']['log_level'])

    # Register blueprints
    app.register_blueprint(routes.main)

    return app

if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', app.config['SYSTEM'].get('port', 8000)))
    app.run(debug=app.config['SYSTEM']['debug_mode'], port=port)