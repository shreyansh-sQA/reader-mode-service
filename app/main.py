from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import bleach
import httpx
from bs4 import BeautifulSoup, Comment
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import AnyHttpUrl, BaseModel
from readability import Document


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
USER_AGENT = (
    "ReaderModeService/1.0 "
    "(+https://github.com/shreyansh-sQA/reader-mode-service)"
)

ALLOWED_TAGS = {
    "a",
    "article",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "picture",
    "pre",
    "q",
    "section",
    "small",
    "source",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel"],
    "blockquote": ["cite"],
    "img": ["alt", "height", "src", "srcset", "title", "width"],
    "q": ["cite"],
    "source": ["src", "srcset", "type"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan", "scope"],
}

REMOVE_SELECTORS = (
    "aside",
    "button",
    "canvas",
    "embed",
    "footer",
    "form",
    "iframe",
    "input",
    "link",
    "nav",
    "noscript",
    "object",
    "script",
    "select",
    "style",
    "svg",
    "textarea",
    "video",
)

JUNK_CLASS_RE = re.compile(
    r"(ad-|advert|banner|cookie|footer|modal|newsletter|outbrain|promo|"
    r"related|share|sidebar|sponsor|subscribe)",
    re.IGNORECASE,
)


class ReaderRequest(BaseModel):
    url: AnyHttpUrl


@dataclass
class FetchedPage:
    url: str
    html: str


app = FastAPI(
    title="Reader Mode Service",
    description="Fetch a URL and return a clean reading-mode HTML document.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.post("/reader", response_class=HTMLResponse)
async def reader(payload: ReaderRequest) -> HTMLResponse:
    """Return a clean, readable HTML version of a fetched article page."""
    return await render_reader_url(str(payload.url))


@app.get("/reader", response_class=HTMLResponse)
async def reader_link(url: Optional[AnyHttpUrl] = Query(default=None)) -> HTMLResponse:
    """Browser-friendly reader endpoint for opening a URL directly."""
    if url is None:
        return HTMLResponse(render_form())

    return await render_reader_url(str(url))


async def render_reader_url(url: str) -> HTMLResponse:
    try:
        fetched = await fetch_page(url)
        document = build_reader_html(fetched.html, fetched.url)
    except ReaderServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return HTMLResponse(document)


class ReaderServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def fetch_page(url: str) -> FetchedPage:
    """Fetch a public HTTP(S) URL with bounded redirects and response size."""
    current_url = url

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await ensure_public_http_url(current_url)

            try:
                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ReaderServiceError(502, "Redirect response missing Location header.")
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status_code >= 400:
                        raise ReaderServiceError(
                            502,
                            f"Upstream returned HTTP {response.status_code}.",
                        )

                    content_type = response.headers.get("content-type", "")
                    if content_type and "html" not in content_type.lower():
                        raise ReaderServiceError(415, "URL did not return an HTML document.")

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ReaderServiceError(413, "HTML document is too large.")

                    encoding = response.encoding or "utf-8"
                    return FetchedPage(
                        url=str(response.url),
                        html=bytes(body).decode(encoding, errors="replace"),
                    )
            except httpx.RequestError as exc:
                raise ReaderServiceError(502, f"Could not fetch URL: {exc}") from exc

    raise ReaderServiceError(508, "Too many redirects.")


async def ensure_public_http_url(url: str) -> None:
    await asyncio.to_thread(_ensure_public_http_url_sync, url)


def _ensure_public_http_url_sync(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ReaderServiceError(400, "Only http and https URLs are supported.")

    if not parsed.hostname:
        raise ReaderServiceError(400, "URL must include a hostname.")

    hostname = parsed.hostname.strip().rstrip(".")
    if hostname.lower() == "localhost":
        raise ReaderServiceError(400, "Localhost URLs are not allowed.")

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ReaderServiceError(400, "URL includes an invalid port.") from exc

    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ReaderServiceError(400, "Could not resolve URL hostname.") from exc

    resolved_ips = {result[4][0] for result in addresses}
    if not resolved_ips:
        raise ReaderServiceError(400, "Could not resolve URL hostname.")

    for ip in resolved_ips:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ReaderServiceError(400, "Hostname resolved to an invalid IP address.") from exc

        if not _is_public_ip(address):
            raise ReaderServiceError(400, "Private and local network URLs are not allowed.")


def _is_public_ip(address: ipaddress._BaseAddress) -> bool:
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def build_reader_html(source_html: str, base_url: str) -> str:
    document = Document(source_html, url=base_url)
    title = document.short_title() or "Reader View"
    summary = document.summary(html_partial=True)

    soup = BeautifulSoup(summary, "html.parser")
    remove_junk_nodes(soup)
    remove_duplicate_title(soup, title)
    normalize_links_and_images(soup, base_url)

    article = soup.decode(formatter="html")
    cleaner = bleach.Cleaner(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    clean_article = cleaner.clean(article)

    return render_document(title=title, article_html=clean_article, source_url=base_url)


def remove_junk_nodes(soup: BeautifulSoup) -> None:
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for node in soup.select(",".join(REMOVE_SELECTORS)):
        node.decompose()

    for node in soup.find_all(True):
        classes = " ".join(node.get("class", []))
        node_id = node.get("id", "")
        marker = f"{classes} {node_id}"
        if marker and JUNK_CLASS_RE.search(marker):
            node.decompose()


def remove_duplicate_title(soup: BeautifulSoup, title: str) -> None:
    first_heading = soup.find(["h1", "h2"])
    if first_heading and normalized_text(first_heading.get_text()) == normalized_text(title):
        first_heading.decompose()


def normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def normalize_links_and_images(soup: BeautifulSoup, base_url: str) -> None:
    for link in soup.find_all("a", href=True):
        href = safe_urljoin(base_url, link["href"], allowed_schemes={"http", "https", "mailto"})
        if href:
            link["href"] = href
            link["rel"] = "noopener noreferrer"
        else:
            del link["href"]

    for image in soup.find_all("img"):
        src = first_present_attribute(image, ("src", "data-src", "data-original", "data-lazy-src"))
        if src:
            absolute_src = safe_urljoin(base_url, src, allowed_schemes={"http", "https"})
            if absolute_src:
                image["src"] = absolute_src
            elif image.has_attr("src"):
                del image["src"]

        if image.get("srcset"):
            image["srcset"] = absolutize_srcset(image["srcset"], base_url)

        image.attrs = {
            key: value
            for key, value in image.attrs.items()
            if key in {"alt", "height", "src", "srcset", "title", "width"}
        }

    for source in soup.find_all("source"):
        if source.get("src"):
            absolute_src = safe_urljoin(base_url, source["src"], allowed_schemes={"http", "https"})
            if absolute_src:
                source["src"] = absolute_src
            else:
                del source["src"]
        if source.get("srcset"):
            source["srcset"] = absolutize_srcset(source["srcset"], base_url)


def first_present_attribute(node, attribute_names: Iterable[str]) -> str | None:
    for name in attribute_names:
        value = node.get(name)
        if value:
            return str(value)
    return None


def absolutize_srcset(srcset: str, base_url: str) -> str:
    candidates = []
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        absolute_url = safe_urljoin(base_url, parts[0], allowed_schemes={"http", "https"})
        if absolute_url:
            parts[0] = absolute_url
            candidates.append(" ".join(parts))
    return ", ".join(candidates)


def safe_urljoin(base_url: str, value: str, allowed_schemes: set[str]) -> str | None:
    absolute_url = urljoin(base_url, value.strip())
    if urlparse(absolute_url).scheme in allowed_schemes:
        return absolute_url
    return None


def render_document(title: str, article_html: str, source_url: str) -> str:
    escaped_title = html.escape(title)
    escaped_source = html.escape(source_url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --page: #f8f7f2;
      --ink: #1f2933;
      --muted: #65707a;
      --rule: #d8d3c7;
      --link: #0b6b75;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --page: #16191d;
        --ink: #e8e5dc;
        --muted: #a8b0b8;
        --rule: #343941;
        --link: #74d0dc;
      }}
    }}
    body {{
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", Times, serif;
      font-size: 19px;
      line-height: 1.7;
    }}
    main {{
      box-sizing: border-box;
      max-width: 760px;
      margin: 0 auto;
      padding: 48px 22px 72px;
    }}
    header {{
      border-bottom: 1px solid var(--rule);
      margin-bottom: 34px;
      padding-bottom: 22px;
    }}
    h1, h2, h3, h4, h5, h6 {{
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.25;
    }}
    h1 {{
      font-size: clamp(2rem, 7vw, 3.35rem);
      margin: 0 0 14px;
    }}
    h2 {{
      font-size: 1.55rem;
      margin-top: 2.2em;
    }}
    h3 {{
      font-size: 1.25rem;
      margin-top: 1.8em;
    }}
    p, ul, ol, blockquote, table, figure, pre {{
      margin: 0 0 1.25em;
    }}
    a {{
      color: var(--link);
      text-decoration-thickness: 0.08em;
      text-underline-offset: 0.15em;
    }}
    img, picture {{
      display: block;
      height: auto;
      max-width: 100%;
    }}
    figure {{
      margin-left: 0;
      margin-right: 0;
    }}
    figcaption, .source {{
      color: var(--muted);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 0.88rem;
      line-height: 1.5;
    }}
    blockquote {{
      border-left: 4px solid var(--rule);
      color: var(--muted);
      margin-left: 0;
      padding-left: 1em;
    }}
    pre, code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 0.92em;
    }}
    pre {{
      overflow-x: auto;
      padding: 1em;
      border: 1px solid var(--rule);
    }}
    table {{
      border-collapse: collapse;
      display: block;
      overflow-x: auto;
      width: 100%;
    }}
    th, td {{
      border-bottom: 1px solid var(--rule);
      padding: 0.45em 0.65em;
      text-align: left;
      vertical-align: top;
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{escaped_title}</h1>
      <div class="source">Source: <a href="{escaped_source}">{escaped_source}</a></div>
    </header>
    <article>
      {article_html}
    </article>
  </main>
</body>
</html>
"""


def render_form() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reader Mode</title>
  <style>
    :root {
      color-scheme: light dark;
      --page: #f8f7f2;
      --ink: #1f2933;
      --muted: #65707a;
      --rule: #d8d3c7;
      --accent: #0b6b75;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --page: #16191d;
        --ink: #e8e5dc;
        --muted: #a8b0b8;
        --rule: #343941;
        --accent: #74d0dc;
      }
    }
    body {
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      box-sizing: border-box;
      max-width: 720px;
      margin: 0 auto;
      padding: 56px 22px;
    }
    h1 {
      font-size: clamp(2rem, 8vw, 3.25rem);
      line-height: 1.1;
      margin: 0 0 14px;
    }
    p {
      color: var(--muted);
      font-size: 1.05rem;
      line-height: 1.6;
      margin: 0 0 28px;
    }
    form {
      display: flex;
      gap: 10px;
    }
    input, button {
      border: 1px solid var(--rule);
      border-radius: 6px;
      box-sizing: border-box;
      font: inherit;
      min-height: 48px;
      padding: 0 14px;
    }
    input {
      background: transparent;
      color: var(--ink);
      flex: 1;
      min-width: 0;
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: var(--page);
      cursor: pointer;
      font-weight: 700;
    }
    @media (max-width: 620px) {
      form {
        flex-direction: column;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>Reader Mode</h1>
    <p>Paste an article URL and open a clean reading view.</p>
    <form method="get" action="/reader">
      <input name="url" type="url" placeholder="https://example.com/article" required autofocus>
      <button type="submit">Open</button>
    </form>
  </main>
</body>
</html>
"""
