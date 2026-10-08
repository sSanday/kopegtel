"""nms.exceptions — Custom exception types for structured error handling."""

import logging
from functools import wraps

logger = logging.getLogger(__name__)


class NMSException(Exception):
    """Base exception for NMS errors."""

    pass


class DatabaseError(NMSException):
    """Database operation failed."""

    pass


class AuthenticationError(NMSException):
    """Authentication failed."""

    pass


class ValidationError(NMSException):
    """Input validation failed."""

    pass


class NetworkError(NMSException):
    """Network operation failed."""

    pass


class ConfigError(NMSException):
    """Configuration error."""

    pass


def safe_db_operation(fallback_value=None, log_level=logging.WARNING):
    """Decorator for safe database operations with proper logging."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.log(
                    log_level,
                    f"DB operation {func.__name__} failed: {type(e).__name__}: {e}",
                    exc_info=log_level <= logging.DEBUG,
                )
                return fallback_value

        return wrapper

    return decorator


def safe_network_operation(fallback_value=None, log_level=logging.WARNING):
    """Decorator for safe network operations with proper logging."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.log(
                    log_level,
                    f"Network operation {func.__name__} failed: {type(e).__name__}: {e}",
                    exc_info=log_level <= logging.DEBUG,
                )
                return fallback_value

        return wrapper

    return decorator


def log_exception(level=logging.ERROR, prefix=""):
    """Decorator to log exceptions with context."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                msg = f"{prefix}{func.__name__}" if prefix else func.__name__
                logger.log(
                    level,
                    f"Exception in {msg}: {type(e).__name__}: {e}",
                    exc_info=level <= logging.DEBUG,
                )
                raise

        return wrapper

    return decorator
