from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from config import Config
import os

db = SQLAlchemy()
migrate = Migrate()


def create_app(config_class=Config):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, 'app', 'templates'),
        static_folder=os.path.join(base_dir, 'app', 'static'),
    )
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)

    from app.routes import bp
    app.register_blueprint(bp)

    return app


app = create_app()


@app.cli.command('init-db')
def init_db():
    db.create_all()
    print('Database tables created.')


if __name__ == '__main__':
    app.run(debug=True)
