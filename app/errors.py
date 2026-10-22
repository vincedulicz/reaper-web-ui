"""Errors that map 1:1 to an HTTP status (see the handler in app/__init__.py)."""


class DawError(Exception):
    status = 500


class BadInput(DawError):
    status = 400


class TrackNotFound(DawError):
    status = 404


class InvalidState(DawError):
    status = 409


class DawUnavailable(DawError):
    status = 503
