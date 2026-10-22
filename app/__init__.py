from flask import Flask, jsonify

from .backends.base import DawBackend
from .errors import DawError
from .services import RecorderService
from .web import bp


def create_app(backend: DawBackend) -> Flask:
    app = Flask(__name__)
    app.extensions["recorder"] = RecorderService(backend)
    app.register_blueprint(bp)

    @app.errorhandler(DawError)
    def handle_daw_error(error: DawError):
        return jsonify(error=str(error)), error.status

    return app
