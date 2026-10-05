"""Polite HTTP session: retries, backoff, rate limiting. Shared by all sources."""
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session(delay: float = 1.5, timeout: int = 30, max_retries: int = 3) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Accept-Language": "en-CA,en;q=0.9",
    })
    retry = Retry(total=max_retries, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.request_timeout = timeout
    s.politeness_delay = delay
    _orig_get = s.get

    def polite_get(url, **kw):
        time.sleep(getattr(s, "politeness_delay", 0))
        kw.setdefault("timeout", timeout)
        return _orig_get(url, **kw)

    s.get = polite_get
    return s
