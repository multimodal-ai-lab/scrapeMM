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
