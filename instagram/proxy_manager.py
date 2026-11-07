from typing import Optional
from urllib.parse import urlparse, urlunparse
from config import settings


class ProxyManager:
    """
    Manages consistent proxy routing for Instagrapi sessions via .env settings:
    PROXY_ENABLED, PROXY_URL, PROXY_USERNAME, PROXY_PASSWORD.
    """

    def __init__(self) -> None:
        self.enabled: bool = settings.PROXY_ENABLED
        self.proxy_url: str = settings.PROXY_URL.strip()
        self.username: str = settings.PROXY_USERNAME.strip()
        self.password: str = settings.PROXY_PASSWORD.strip()

    def get_proxy_dsn(self) -> Optional[str]:
        if not self.enabled or not self.proxy_url:
            return None

        parsed = urlparse(self.proxy_url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""

        if self.username and self.password:
            netloc = f"{self.username}:{self.password}@{host}{port}"
        else:
            netloc = parsed.netloc or f"{host}{port}"

        return urlunparse((scheme, netloc, parsed.path, "", "", ""))

    def get_masked_proxy(self) -> str:
        if not self.enabled or not self.proxy_url:
            return "Disabled"
        parsed = urlparse(self.proxy_url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"
