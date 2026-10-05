"""Product-image cache: download once, reuse forever. Keyed by URL hash."""
import hashlib
import os
from urllib.parse import urlparse


def cached_image_path(image_url: str, cache_dir: str) -> str | None:
    if not image_url:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    h = hashlib.md5(image_url.encode()).hexdigest()[:16]
    ext = os.path.splitext(urlparse(image_url).path)[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    return os.path.join(cache_dir, h + ext)


def fetch_image(session, image_url: str, cache_dir: str) -> str | None:
    """Download image_url into the cache; return local path (or None on failure)."""
    path = cached_image_path(image_url, cache_dir)
    if path is None:
        return None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        r = session.get(image_url, timeout=30)
        r.raise_for_status()
        if len(r.content) < 1024:  # not a real image
            return None
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None
