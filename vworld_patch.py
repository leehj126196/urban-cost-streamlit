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
    """VWorld WFS 1.1.0 EPSG:4326 axis order is lat,lon for BBOX."""
    params = dict(params or {})
    bbox_key = "BBOX" if "BBOX" in params else "bbox" if "bbox" in params else None
    srs = str(params.get("SRSNAME", params.get("srsname", ""))).upper()

    if bbox_key and srs == "EPSG:4326":
        try:
            vals = [float(v) for v in str(params[bbox_key]).split(",")[:4]]
            if len(vals) == 4:
                minx, miny, maxx, maxy = vals
                # VWorld WFS 1.1.0: ymin,xmin,ymax,xmax
                params[bbox_key] = f"{miny},{minx},{maxy},{maxx}"
        except Exception:
            pass

    # VWorld WFS examples use OUTPUT=json for GeoJSON response.
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
