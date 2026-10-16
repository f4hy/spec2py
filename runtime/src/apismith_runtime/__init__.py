"""Runtime for apismith-generated API clients.

Generated packages import from here; end users mostly touch the error classes,
``NOT_GIVEN``, the auth helpers and ``Hooks``.
"""

from ._auth import ApiKeyAuth, BasicAuth, BearerAuth, MultiAuth, OAuth2ClientCredentials
from ._client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    APIClient,
    APIResource,
    AsyncAPIClient,
    AsyncAPIResource,
    Hooks,
    RequestOptions,
    RequestSpec,
)
from ._exceptions import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from ._models import (
    BaseModel,
    Tolerant,
    build_body,
    jsonable,
    parse_value,
    rebuild_models,
    validate_param,
)
from ._pagination import (
    AsyncPage,
    AsyncPaginator,
    CursorPagination,
    LinkHeaderPagination,
    NextPage,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PageState,
    PaginationStrategy,
)
from ._qs import path_template
from ._response import (
    APIResponse,
    async_to_raw_response_wrapper,
    to_raw_response_wrapper,
)
from ._retry import RetryPolicy
from ._streaming import (
    AsyncStream,
    AsyncStreamManager,
    ServerSentEvent,
    SSEDecoder,
    Stream,
    StreamManager,
)
from ._types import (
    NOT_GIVEN,
    Body,
    FileTypes,
    Headers,
    NotGiven,
    Query,
    Timeout,
    TimeoutTypes,
    is_given,
)

try:
    from ._version import __version__
except ImportError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "NOT_GIVEN",
    "APIClient",
    "APIConnectionError",
    "APIError",
    "APIResource",
    "APIResponse",
    "APIResponseValidationError",
    "APIStatusError",
    "APITimeoutError",
    "ApiKeyAuth",
    "AsyncAPIClient",
    "AsyncAPIResource",
    "AsyncPage",
    "AsyncPaginator",
    "AsyncStream",
    "AsyncStreamManager",
    "AuthenticationError",
    "BadRequestError",
    "BaseModel",
    "BasicAuth",
    "BearerAuth",
    "Body",
    "ConflictError",
    "CursorPagination",
    "FileTypes",
    "Headers",
    "Hooks",
    "InternalServerError",
    "LinkHeaderPagination",
    "MultiAuth",
    "NextPage",
    "NotFoundError",
    "NotGiven",
    "OAuth2ClientCredentials",
    "OffsetPagination",
    "Page",
    "PageNumberPagination",
    "PageState",
    "PaginationStrategy",
    "PermissionDeniedError",
    "Query",
    "RateLimitError",
    "RequestOptions",
    "RequestSpec",
    "RetryPolicy",
    "SSEDecoder",
    "ServerSentEvent",
    "Stream",
    "StreamManager",
    "Timeout",
    "TimeoutTypes",
    "Tolerant",
    "UnprocessableEntityError",
    "__version__",
    "async_to_raw_response_wrapper",
    "build_body",
    "is_given",
    "jsonable",
    "parse_value",
    "path_template",
    "rebuild_models",
    "to_raw_response_wrapper",
    "validate_param",
]
