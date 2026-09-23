import ssl

import certifi

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-GPC": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Priority": "u=0, i"
}
ssl_context = ssl.create_default_context(cafile=certifi.where())
RELAXED_SSL_DOMAINS = {  # These domains do not support SSL verification
    "archive.today",
    "archive.is",
    "archive.ph",
    "archive.vn",
    "archive.li",
    "archive.fo",
    "archive.md",
}

# Domains that serve an nginx "decoy" page to clients whose TLS fingerprint is not a real
# browser's (aiohttp, on some IPs) instead of the real content. Downloads to these go
# through curl_cffi browser-TLS impersonation first, aiohttp only as a fallback.
BROWSER_TLS_DOMAINS = {
    "archive.today",
    "archive.is",
    "archive.ph",
    "archive.vn",
    "archive.li",
    "archive.fo",
    "archive.md",
}
