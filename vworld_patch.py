"""Runtime compatibility patch for VWorld requests used by the Streamlit app."""

from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DIRECT = "https://api.vworld.kr/req/data"
_WFS = "https://api.vworld.kr/req/wfs"
_PROXY = "https://map.vworld.kr/proxy.do"

_ORIGINAL_REQUESTS_GET = requests.get
_ORIGINAL_SESSION_GET = requests.sessions.Session.get

_retry = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=0.7,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _normalize_wfs_params(params):
    """Normalize VWorld WFS responses to JSON without changing caller BBOX."""
    params = dict(params or {})

    # VWorld WFS examples use OUTPUT=json for GeoJSON responses.
    if "OUTPUT" in params:
        params["OUTPUT"] = "json"
    elif "output" in params:
        params["output"] = "json"
    else:
        params["OUTPUT"] = "json"

    return params


def _patched_session_get(self, url, params=None, **kwargs):
    params = dict(params or {})
    if url == _WFS:
        params = _normalize_wfs_params(params)
    return _ORIGINAL_SESSION_GET(self, url, params=params, **kwargs)


def _safe_requests_get(url, params=None, **kwargs):
    """Keep retry support for code paths that use requests.get directly."""
    timeout = kwargs.pop("timeout", 45)
    params = dict(params or {})

    if url == _WFS:
        params = _normalize_wfs_params(params)

    if url == _DIRECT:
        if "geomfilter" in params and "geomFilter" not in params:
            params["geomFilter"] = params.pop("geomfilter")

        response = _ORIGINAL_SESSION_GET(_session, url, params=params, timeout=timeout, **kwargs)
        if response.status_code < 500:
            return response

        # Last-resort compatibility path for VWorld's own proxy endpoint.
        inner = url + "?" + urlencode(params)
        return _ORIGINAL_SESSION_GET(
            _session,
            _PROXY,
            params={"url": inner},
            timeout=timeout,
            **kwargs,
        )

    return _ORIGINAL_SESSION_GET(_session, url, params=params, timeout=timeout, **kwargs)


# Patch both the module-level helper and Session.get because app.py uses a Session.
requests.sessions.Session.get = _patched_session_get
requests.get = _safe_requests_get
