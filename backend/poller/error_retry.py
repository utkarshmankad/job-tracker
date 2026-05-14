"""Retry logic for transient Gmail API errors."""

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from googleapiclient.errors import HttpError

log = structlog.get_logger()


class AuthError(Exception):
    pass


class RateLimitError(Exception):
    pass


def gmail_retry(max_attempts: int = 3):
    """Decorator: retry on 429/5xx, raise AuthError immediately on 401/403."""

    def decorator(fn):
        @retry(
            retry=retry_if_exception_type(RateLimitError),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            reraise=True,
        )
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except HttpError as e:
                if e.resp.status in (401, 403):
                    log.error("gmail_auth_error", status=e.resp.status)
                    raise AuthError(f"Gmail auth failed: {e.resp.status}") from e
                elif e.resp.status == 429:
                    log.warning("gmail_rate_limit")
                    raise RateLimitError("Rate limited") from e
                else:
                    log.error("gmail_http_error", status=e.resp.status)
                    raise

        return wrapper

    return decorator
