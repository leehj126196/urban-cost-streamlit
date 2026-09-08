import time
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_ORIGINAL_GET = requests.get
_DIRECT = "https://api.vworld.kr/req/data"
_PROXY = "https://map.vworld.kr/proxy.do"

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


def _safe_get(url, params=None, **kwargs):
    """VWorld req/data 5xx를 자동 재시도하고 공식 proxy.do 경로로 fallback합니다."""
    timeout = kwargs.pop("timeout", 45)
    params = dict(params or {})

    if url == _DIRECT:
        # 현재 VWorld 샘플 표기와 맞춤
        if "geomfilter" in params and "geomFilter" not in params:
            params["geomFilter"] = params.pop("geomfilter")
        if str(params.get("request", "")).lower() == "getfeature":
            params["request"] = "getfeature"

        response = _session.get(url, params=params, timeout=timeout, **kwargs)
        if response.status_code < 500:
            return response

        inner = url + "?" + urlencode(params)
        proxy_response = _session.get(_PROXY, params={"url": inner}, timeout=timeout, **kwargs)
        return proxy_response

    return _session.get(url, params=params, timeout=timeout, **kwargs)


requests.get = _safe_get
