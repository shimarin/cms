import json
import os
import time
import logging
import logging.handlers
import ipaddress
import datetime
import html
import secrets
import smtplib
import mimetypes
from io import BytesIO
from email.utils import formatdate, parsedate_to_datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from PIL import Image, ImageOps, UnidentifiedImageError
from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from jinja2 import Environment, FileSystemLoader, ChoiceLoader, select_autoescape
from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.container import container_plugin

TRUSTED_PROXIES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(cidr) for cidr in [
        "127.0.0.0/8",     # IPv4 loopback
        "10.0.0.0/8",      # IPv4 private
        "172.16.0.0/12",   # IPv4 private
        "192.168.0.0/16",  # IPv4 private
        "169.254.0.0/16",  # IPv4 link-local
        "::1/128",         # IPv6 loopback
        "fc00::/7",        # IPv6 ULA
        "fe80::/10",       # IPv6 link-local
    ]
]


def _is_trusted_proxy(request: Request) -> bool:
    # Unix domain socket connections have no client address; always trust them
    # since only local processes can connect via a socket file.
    if not request.client:
        return True
    remote = request.client.host
    try:
        return any(ipaddress.ip_address(remote) in net for net in TRUSTED_PROXIES)
    except ValueError:
        return False


def _get_client_ip_and_vary(request: Request) -> tuple[str, tuple[str, ...]]:
    """Return (client_ip, vary_fields) where vary_fields lists every request header
    whose presence/value could have affected the IP decision.

    - CF-Connecting-IP used (trusted proxy):
        only CF-Connecting-IP matters (XFF is ignored when CF is present).
    - X-Forwarded-For used (trusted proxy, no CF):
        both CF-Connecting-IP and X-Forwarded-For matter (adding CF would change
        the result).
    - Trusted proxy but neither header present:
        both could change the result if added.
    - Untrusted source (direct connection):
        no proxy header is consulted, so no Vary fields are required.
    """
    remote = request.client.host if request.client else ""
    if _is_trusted_proxy(request):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip, ("CF-Connecting-IP",)
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip(), ("CF-Connecting-IP", "X-Forwarded-For")
        return remote or "-", ("CF-Connecting-IP", "X-Forwarded-For")
    return remote or "-", ()


def get_client_ip(request: Request) -> str:
    return _get_client_ip_and_vary(request)[0]


def get_site_url(request: Request) -> str:
    trusted = _is_trusted_proxy(request)
    host = request.headers.get("host", "").split(",", 1)[0].strip()
    if trusted:
        host = request.headers.get("x-forwarded-host", host).split(",", 1)[0].strip()
    scheme = request.url.scheme
    if trusted:
        proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        if proto in {"http", "https"}:
            scheme = proto
        else:
            cf_visitor = request.headers.get("cf-visitor", "")
            if '"scheme":"https"' in cf_visitor:
                scheme = "https"
    return f"{scheme}://{host}"


BASE_DIR = Path(__file__).parent
VHOSTS_DIR = BASE_DIR / "vhosts"
TOP_DOCS_DIR = BASE_DIR / "docs"
TOP_TEMPLATES_DIR = BASE_DIR / "templates"
TOP_LOGS_DIR = BASE_DIR / "logs"

VHOST_OVERRIDE: str | None = os.environ.get("CMS_VHOST_OVERRIDE") or None
FILE_LOGGING_ENABLED = os.environ.get("CMS_FILE_LOGGING") == "1"
try:
    LOG_ROTATION_MIN_BYTES = int(os.environ.get("CMS_LOG_ROTATION_MIN_BYTES", 1024 * 1024))
except ValueError:
    LOG_ROTATION_MIN_BYTES = 1024 * 1024


_LANG_ALIASES = {
    "c++": "cpp", "cxx": "cpp", "c#": "csharp",
    "shell": "bash", "sh": "bash", "zsh": "bash",
    "js": "javascript", "ts": "typescript",
}

_PYGMENTS_FORMATTER = HtmlFormatter(nowrap=True)
PYGMENTS_CSS = _PYGMENTS_FORMATTER.get_style_defs(".highlight")


def _render_fence(self, tokens, idx, options, env):
    token = tokens[idx]
    info = token.info.strip().split(None, 1)[0] if token.info else ""
    lang = _LANG_ALIASES.get(info, info)
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    highlighted = highlight(token.content, lexer, _PYGMENTS_FORMATTER)
    lang_class = f" language-{html.escape(info)}" if info else ""
    return f'<pre class="highlight"><code class="{lang_class.strip()}">{highlighted}</code></pre>\n'


def _render_link_open(self, tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if href.endswith(".md") and not href.startswith(("http://", "https://", "//", "mailto:", "tel:", "#")):
        token.attrSet("href", href[:-3] + ".html")
    return self.renderToken(tokens, idx, options, env)


def _make_container_rule(class_set: set[str]):
    """全コンテナクラスを1つのブロックルールでスタック管理する。"""
    def rule(state, startLine, endLine, silent):
        start = state.bMarks[startLine] + state.tShift[startLine]
        maximum = state.eMarks[startLine]

        pos = start
        while pos < maximum and state.src[pos] == ':':
            pos += 1
        marker_count = pos - start
        if marker_count < 3:
            return False

        params = state.src[pos:maximum].strip()
        class_name = params.split(None, 1)[0] if params else ""
        if not class_name or class_name not in class_set:
            return False
        if silent:
            return True

        # 対応する閉じマーカーを深さ追跡で探す
        depth = 1
        nextLine = startLine + 1
        auto_closed = False
        while nextLine < endLine:
            ls = state.bMarks[nextLine] + state.tShift[nextLine]
            lm = state.eMarks[nextLine]
            if state.src[ls:ls+1] == ':':
                cp = ls
                while cp < lm and state.src[cp] == ':':
                    cp += 1
                lmc = cp - ls
                if lmc >= 3:
                    rest = state.src[cp:lm].strip()
                    lcls = rest.split(None, 1)[0] if rest else ""
                    if lcls and lcls in class_set:
                        depth += 1
                    elif not lcls and lmc >= marker_count:
                        depth -= 1
                        if depth == 0:
                            auto_closed = True
                            break
            nextLine += 1

        old_parent, old_line_max = state.parentType, state.lineMax
        state.parentType = "container"
        state.lineMax = nextLine

        token = state.push(f"container_{class_name}_open", "div", 1)
        token.markup = state.src[start:pos]
        token.block = True
        token.info = params
        token.map = [startLine, nextLine]
        token.attrSet("class", class_name)

        state.md.block.tokenize(state, startLine + 1, nextLine)

        token = state.push(f"container_{class_name}_close", "div", -1)
        token.markup = state.src[start:pos]
        token.block = True

        state.parentType = old_parent
        state.lineMax = old_line_max
        state.line = nextLine + (1 if auto_closed else 0)
        return True

    return rule


def make_md(container_classes: list[str] = ()) -> MarkdownIt:
    instance = (
        MarkdownIt("commonmark", {"html": True})
        .use(front_matter_plugin)
        .use(footnote_plugin)
        .enable("table")
    )
    if container_classes:
        instance.block.ruler.before(
            "fence", "containers",
            _make_container_rule(set(container_classes)),
            {"alt": ["paragraph", "reference", "blockquote", "list"]},
        )
    instance.add_render_rule("fence", _render_fence)
    instance.add_render_rule("link_open", _render_link_open)
    return instance


# container_classesなし（index_ofのfront matter抽出・通常ページ）用ベースインスタンス
_md_base = make_md()


def extract_front_matter_raw(text: str) -> dict:
    """rawテキストからfront matterだけYAMLパースして返す（mdトークナイザ不使用）。"""
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    try:
        parsed = yaml.safe_load(rest[:end])
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


def _extract_h1_plaintext(tokens) -> tuple[str | None, int | None]:
    """Find the first H1 in tokens. Return (plain_text_title, heading_open_index).

    The plain text is built from text/code_inline children of the heading's inline
    token, so inline Markdown decorations (links, emphasis, etc.) are stripped.
    Returns (None, None) when no usable H1 is found.
    """
    for i, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h1":
            continue
        if i + 1 >= len(tokens):
            return None, None
        inline_token = tokens[i + 1]
        if inline_token.type != "inline" or not inline_token.children:
            return None, None
        title = "".join(
            child.content for child in inline_token.children
            if child.type in ("text", "code_inline")
        ).strip()
        return (title or None), i
    return None, None


def parse_markdown_document(
    text: str,
    md_instance: MarkdownIt | None = None,
    defaults: dict | None = None,
    source_mtime: float | None = None,
) -> tuple[str, dict]:
    """Parse Markdown text into (body_html, metadata).

    metadata is the merge of `defaults` and the document's YAML front matter,
    optionally augmented by body-derived values:

    - h1_as_title: when truthy and front matter has no `title`, the first H1 is
      extracted as plain text, used as `title`, and removed from body_html.
    - mtime_as_date: when truthy and front matter has no `date`, `source_mtime`
      (a POSIX timestamp) is converted to a tz-aware datetime using `timezone`.

    Front matter always wins over body-derived values, which always win over defaults.
    """
    instance = md_instance or _md_base
    defaults = defaults or {}
    tokens = instance.parse(text)

    front_matter: dict = {}
    for token in tokens:
        if token.type == "front_matter":
            try:
                parsed = yaml.safe_load(token.content)
                if isinstance(parsed, dict):
                    front_matter = parsed
            except yaml.YAMLError:
                pass
            break

    flags = {**defaults, **front_matter}

    if flags.get("h1_as_title") and not front_matter.get("title"):
        title, idx = _extract_h1_plaintext(tokens)
        if title and idx is not None:
            front_matter["title"] = title
            del tokens[idx:idx + 3]

    if (
        flags.get("mtime_as_date")
        and front_matter.get("date") is None
        and source_mtime is not None
    ):
        tz_name = flags.get("timezone")
        try:
            tz = ZoneInfo(tz_name) if tz_name else _system_tz()
        except (ZoneInfoNotFoundError, KeyError):
            tz = _system_tz()
        front_matter["date"] = datetime.datetime.fromtimestamp(source_mtime, tz=tz)

    body_html = instance.renderer.render(tokens, instance.options, {})
    return body_html, {**defaults, **front_matter}


def load_defaults(docs_dir: Path, md_rel: str) -> dict:
    """Merge defaults.json from docs_dir down to the directory containing md_rel."""
    parts = Path(md_rel).parent.parts
    dirs = [docs_dir] + [docs_dir.joinpath(*parts[:i+1]) for i in range(len(parts))]
    merged = {}
    for d in dirs:
        f = d / "defaults.json"
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged.update(data)
            except (json.JSONDecodeError, OSError):
                pass
    return merged


def check_moved_redirect(docs_dir: Path, path: str) -> str | None:
    """Check defaults.json files for a 'moved' entry matching path.

    Returns the resolved redirect target URL, or None if no match.
    """
    # Walk directories from root down to the parent of the requested path
    parts = Path(path).parent.parts if path else ()
    dirs = [docs_dir] + [docs_dir.joinpath(*parts[:i+1]) for i in range(len(parts))]

    for d in dirs:
        f = d / "defaults.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        moved = data.get("moved")
        if not isinstance(moved, dict):
            continue

        # Compute the relative path from this defaults.json's directory to the request
        try:
            rel = Path(path).relative_to(d.relative_to(docs_dir)).as_posix() if d != docs_dir else path
        except ValueError:
            continue

        # Normalize: strip leading slash from keys
        for from_key, to_val in moved.items():
            from_normalized = from_key.lstrip("/")
            # Skip entries with ../ traversal
            if "../" in from_normalized or from_normalized.startswith(".."):
                logging.getLogger("error").warning("moved entry with '../' skipped: %s in %s", from_key, f)
                continue
            if from_normalized == rel:
                # Resolve target URL
                if not isinstance(to_val, str):
                    continue
                if to_val.startswith("https://") or to_val.startswith("http://"):
                    return to_val
                if to_val.startswith("/"):
                    return to_val
                # Relative to defaults.json directory
                dir_url = "/" + d.relative_to(docs_dir).as_posix() if d != docs_dir else ""
                if dir_url and not dir_url.endswith("/"):
                    dir_url += "/"
                elif not dir_url:
                    dir_url = "/"
                if to_val == "":
                    return dir_url
                return dir_url + to_val

    return None


def _system_tz() -> datetime.tzinfo:
    return datetime.datetime.now().astimezone().tzinfo


def resolve_date(date_val, tz_name: str | None) -> datetime.datetime | None:
    """Return timezone-aware datetime from a YAML date/datetime value, or None if not present."""
    if date_val is None:
        return None
    try:
        tz: datetime.tzinfo = ZoneInfo(tz_name) if tz_name else _system_tz()
    except (ZoneInfoNotFoundError, KeyError):
        tz = _system_tz()
    if isinstance(date_val, datetime.datetime):
        return date_val if date_val.tzinfo else date_val.replace(tzinfo=tz)
    if isinstance(date_val, datetime.date):
        return datetime.datetime(date_val.year, date_val.month, date_val.day, tzinfo=tz)
    return None


def make_index_of(lookup_docs: list[Path], defaults_docs_dir: Path):
    """Return an index_of(url_path) function for use in Jinja2 templates."""
    def index_of(url_path: str, include_index: bool = False) -> list[dict]:
        results = []
        rel = url_path.strip("/")
        for docs_dir in lookup_docs:
            scan_dir = docs_dir / rel if rel else docs_dir
            if not scan_dir.is_dir():
                continue
            for md_file in sorted(scan_dir.rglob("*.md")):
                if not include_index and md_file.name == "index.md":
                    continue
                md_rel = str(md_file.relative_to(docs_dir))
                defaults = load_defaults(defaults_docs_dir, md_rel)
                _, vars_ = parse_markdown_document(
                    md_file.read_text(encoding="utf-8"),
                    defaults=defaults,
                    source_mtime=md_file.stat().st_mtime,
                )
                date = resolve_date(vars_.get("date"), vars_.get("timezone"))
                if date is None:
                    continue  # draft
                if md_file.name == "index.md":
                    parent_rel = str(md_file.parent.relative_to(docs_dir)).replace("\\", "/")
                    url = "/" + parent_rel + "/" if parent_rel != "." else "/"
                else:
                    url = "/" + md_rel[:-3] + ".html"
                results.append({**vars_, "url": url, "date": date})
            break  # use first docs_dir that has the directory
        results.sort(key=lambda x: x["date"], reverse=True)
        return results
    return index_of


def generate_feed(url_dir: str, defaults_docs_dir: Path, lookup_docs: list[Path], request: Request) -> Response:
    rel = url_dir.strip("/")
    if not any((docs_dir / rel if rel else docs_dir).is_dir() for docs_dir in lookup_docs):
        return Response("Not Found", status_code=404, media_type="text/plain")  # feed: no template
    articles = make_index_of(lookup_docs, defaults_docs_dir)(url_dir or "/")
    base_url = str(request.base_url).rstrip("/")

    dir_defaults = load_defaults(defaults_docs_dir, (url_dir.strip("/") + "/index.md") if url_dir.strip("/") else "index.md")
    channel_title = dir_defaults.get("title") or dir_defaults.get("site_name") or base_url
    channel_desc = dir_defaults.get("description", "")
    channel_link = base_url + ("/" + url_dir.strip("/") if url_dir.strip("/") else "")

    llm = is_llm_crawler(request)
    items = []
    for article in articles:
        url = article["url"]
        if llm and url.endswith(".html"):
            url = url[:-5] + ".md"
        link = base_url + url
        pub_date = formatdate(article["date"].timestamp(), usegmt=True)
        items.append(
            f"    <item>\n"
            f"      <title>{html.escape(article.get('title', ''))}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid>{link}</guid>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <description>{html.escape(article.get('description', ''))}</description>\n"
            f"    </item>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        f'    <title>{html.escape(channel_title)}</title>\n'
        f'    <link>{channel_link}</link>\n'
        f'    <description>{html.escape(channel_desc)}</description>\n'
        + "\n".join(items) + "\n"
        '  </channel>\n'
        '</rss>\n'
    )
    return Response(body, media_type="application/rss+xml", headers={"Vary": "User-Agent"})


def generate_sitemap(defaults_docs_dir: Path, lookup_docs: list[Path], request: Request) -> Response:
    base_url = str(request.base_url).rstrip("/")
    seen: set[str] = set()
    urls = []

    for docs_dir in lookup_docs:
        for md_file in sorted(docs_dir.rglob("*.md")):
            md_rel = str(md_file.relative_to(docs_dir))
            url_path = "/" + md_rel[:-3] + (".html" if md_file.name != "index.md" else "").replace("index.html", "")
            # normalize directory index URL
            if md_file.name == "index.md":
                url_path = "/" + str(md_file.parent.relative_to(docs_dir)).replace("\\", "/")
                url_path = url_path.rstrip(".")  # docs_dir itself → "/"
                if not url_path.endswith("/"):
                    url_path += "/"

            if url_path in seen:
                continue

            defaults = load_defaults(defaults_docs_dir, md_rel)
            _, vars_ = parse_markdown_document(
                md_file.read_text(encoding="utf-8"),
                defaults=defaults,
                source_mtime=md_file.stat().st_mtime,
            )
            date = resolve_date(vars_.get("date"), vars_.get("timezone"))
            if date is None:
                continue  # draft

            seen.add(url_path)
            if is_llm_crawler(request) and url_path.endswith(".html"):
                url_path = url_path[:-5] + ".md"
            lastmod = datetime.datetime.fromtimestamp(md_file.stat().st_mtime, tz=datetime.timezone.utc)
            urls.append(
                f"  <url>\n"
                f"    <loc>{html.escape(base_url + url_path)}</loc>\n"
                f"    <lastmod>{lastmod.strftime('%Y-%m-%d')}</lastmod>\n"
                f"  </url>"
            )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n"
        '</urlset>\n'
    )
    return Response(body, media_type="application/xml", headers={"Vary": "User-Agent"})


def make_client_ip_match_any(request: Request, vary_fields: set[str] | None = None):
    """Return a client_ip_match_any(patterns) function bound to the current request.

    If vary_fields is provided, the first call automatically records the request
    header that was actually used to determine the client IP (e.g. 'X-Forwarded-For'
    or 'CF-Connecting-IP').  When the IP comes from a direct TCP connection no field
    is added, because no caching proxy is involved in that case.
    """
    def client_ip_match_any(patterns) -> bool:
        client_ip, used_vary = _get_client_ip_and_vary(request)
        if vary_fields is not None and used_vary:
            vary_fields.update(used_vary)
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        for pattern in patterns or []:
            try:
                network = ipaddress.ip_network(str(pattern), strict=False)
            except ValueError:
                continue
            if addr in network:
                return True
        return False
    return client_ip_match_any


def render_error(status_code: int, vhost_dir: Path | None, request: Request | None = None) -> Response:
    env = make_jinja_env(vhost_dir)
    vary_fields: set[str] = set()
    if request is not None:
        env.globals["client_ip_match_any"] = make_client_ip_match_any(request, vary_fields)
    try:
        tmpl = env.get_template(f"{status_code}.j2")
        html_body = tmpl.render(status_code=status_code)
        headers = {}
        if vary_fields:
            headers["Vary"] = ", ".join(sorted(vary_fields))
        return HTMLResponse(html_body, status_code=status_code, headers=headers)
    except Exception:
        return Response(str(status_code), status_code=status_code, media_type="text/plain")


def _link_headers(rss: str | None, sitemap: str | None = None) -> dict:
    """Return a dict with a Link header built from rss/sitemap values, or empty dict if both falsy."""
    parts = []
    if rss:
        parts.append(f'<{rss}>; rel="alternate"; type="application/rss+xml"')
    if sitemap:
        parts.append(f'<{sitemap}>; rel="sitemap"')
    return {"Link": ", ".join(parts)} if parts else {}


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_RESIZE_QUERY_KEYS = {"w", "width", "h", "height", "fit"}
IMAGE_MAX_DIMENSION = 4096
IMAGE_RESAMPLE = Image.Resampling.LANCZOS


def _is_image_resize_request(path: str, request: Request) -> bool:
    if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
        return False
    return any(key in request.query_params for key in IMAGE_RESIZE_QUERY_KEYS)


def _parse_image_dimension(request: Request, short_key: str, long_key: str) -> tuple[int | None, str | None]:
    short_value = request.query_params.get(short_key)
    long_value = request.query_params.get(long_key)
    if short_value and long_value and short_value != long_value:
        return None, f"{short_key} and {long_key} must not conflict"
    value = short_value or long_value
    if value is None or value == "":
        return None, None
    try:
        parsed = int(value)
    except ValueError:
        return None, f"{short_key} must be an integer"
    if parsed < 1 or parsed > IMAGE_MAX_DIMENSION:
        return None, f"{short_key} must be between 1 and {IMAGE_MAX_DIMENSION}"
    return parsed, None


def _parse_image_resize_params(request: Request) -> tuple[dict | None, Response | None]:
    width, error = _parse_image_dimension(request, "w", "width")
    if error:
        return None, Response(error, status_code=400, media_type="text/plain")
    height, error = _parse_image_dimension(request, "h", "height")
    if error:
        return None, Response(error, status_code=400, media_type="text/plain")
    if width is None and height is None:
        return None, Response("w or h is required", status_code=400, media_type="text/plain")

    fit = request.query_params.get("fit")
    if fit is None:
        fit = "cover" if width is not None and height is not None else "inside"
    fit = fit.lower()
    if fit not in {"cover", "contain", "inside"}:
        return None, Response("fit must be cover, contain, or inside", status_code=400, media_type="text/plain")
    if fit == "cover" and (width is None or height is None):
        return None, Response("fit=cover requires both w and h", status_code=400, media_type="text/plain")

    normalized = f"w={width or ''};h={height or ''};fit={fit}"
    return {"width": width, "height": height, "fit": fit, "normalized": normalized}, None


def _image_cache_headers_and_304(request: Request, source_path: Path, normalized_params: str) -> tuple[dict, Response | None]:
    stat = source_path.stat()
    etag = f'"img-{stat.st_mtime_ns}-{stat.st_size}-{normalized_params}"'
    headers = {
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        "ETag": etag,
        "Cache-Control": "public, no-cache",
    }

    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match:
        candidates = [candidate.strip() for candidate in if_none_match.split(",")]
        if "*" in candidates or etag in candidates or f"W/{etag}" in candidates:
            return headers, Response(status_code=304, headers=headers)
    elif ims := request.headers.get("if-modified-since", ""):
        try:
            if int(stat.st_mtime) <= int(parsedate_to_datetime(ims).timestamp()):
                return headers, Response(status_code=304, headers=headers)
        except Exception:
            pass
    return headers, None


def _resize_dimensions(original: tuple[int, int], width: int | None, height: int | None, allow_upscale: bool) -> tuple[int, int]:
    orig_w, orig_h = original
    if width is None:
        scale = height / orig_h
    elif height is None:
        scale = width / orig_w
    else:
        scale = min(width / orig_w, height / orig_h)
    if not allow_upscale:
        scale = min(scale, 1)
    return (max(1, round(orig_w * scale)), max(1, round(orig_h * scale)))


def _render_resized_image(source_path: Path, params: dict) -> tuple[bytes, str] | Response:
    try:
        with Image.open(source_path) as image:
            if getattr(image, "is_animated", False):
                return Response("Animated images are not supported for resizing", status_code=415, media_type="text/plain")
            source_format = image.format or source_path.suffix.lstrip(".").upper()
            transformed = ImageOps.exif_transpose(image)
            width = params["width"]
            height = params["height"]
            fit = params["fit"]

            if fit == "cover":
                transformed = ImageOps.fit(transformed, (width, height), method=IMAGE_RESAMPLE)
            else:
                size = _resize_dimensions(transformed.size, width, height, allow_upscale=(fit == "contain"))
                transformed = transformed.resize(size, IMAGE_RESAMPLE)

            output = BytesIO()
            save_kwargs = {}
            if source_format in {"JPEG", "JPG"}:
                source_format = "JPEG"
                if transformed.mode not in {"RGB", "L"}:
                    transformed = transformed.convert("RGB")
                save_kwargs.update({"quality": 85, "optimize": True})
            elif source_format == "PNG":
                save_kwargs["optimize"] = True
            elif source_format == "WEBP":
                save_kwargs.update({"quality": 85, "method": 6})
            transformed.save(output, format=source_format, **save_kwargs)
    except UnidentifiedImageError:
        return Response("Unsupported image file", status_code=415, media_type="text/plain")
    except OSError:
        return Response("Failed to process image", status_code=415, media_type="text/plain")

    media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    return output.getvalue(), media_type


def render_resized_image(source_path: Path, request: Request) -> Response:
    params, error_response = _parse_image_resize_params(request)
    if error_response is not None:
        return error_response

    cache_headers, not_modified = _image_cache_headers_and_304(request, source_path, params["normalized"])
    if not_modified is not None:
        return not_modified

    rendered = _render_resized_image(source_path, params)
    if isinstance(rendered, Response):
        return rendered
    body, media_type = rendered
    headers = {**cache_headers, "Content-Length": str(len(body))}
    return Response(body, media_type=media_type, headers=headers)


def render_md_file(md_path: Path, defaults_docs_dir: Path, md_rel: str, vhost_dir: Path | None, lookup_docs: list[Path], request: Request) -> Response:
    file_mtime = md_path.stat().st_mtime
    mtime = file_mtime
    text = md_path.read_text(encoding="utf-8")
    defaults = load_defaults(defaults_docs_dir, md_rel)
    raw_fm = extract_front_matter_raw(text)
    template_name = {**defaults, **raw_fm}.get("template", "default.j2")
    env = make_jinja_env(vhost_dir)
    try:
        _, tmpl_filename, _ = env.loader.get_source(env, template_name)
        if tmpl_filename:
            mtime = max(mtime, Path(tmpl_filename).stat().st_mtime)
    except Exception:
        pass

    link_headers = _link_headers({**defaults, **raw_fm}.get("rss"), {**defaults, **raw_fm}.get("sitemap"))

    container_classes = {**defaults, **raw_fm}.get("container_classes", [])
    body_html, vars_ = parse_markdown_document(
        text,
        make_md(container_classes),
        defaults=defaults,
        source_mtime=file_mtime,
    )
    vars_.pop("template", None)

    url_path = request.url.path
    page_dir = url_path if url_path.endswith("/") else str(Path(url_path).parent) + "/"
    page_vars = {
        "url_path": url_path,
        "page_dir": page_dir,
        "site_url": get_site_url(request),
    }
    # defaults/front matter の明示値を優先
    page_vars.update(vars_)
    vars_ = page_vars

    # index_of を一度でも呼んだページは内容が動的に変動しうるためキャッシュ対象外にする
    index_of_used = False
    base_index_of = make_index_of(lookup_docs, defaults_docs_dir)
    def index_of_tracked(*args, **kwargs):
        nonlocal index_of_used
        index_of_used = True
        return base_index_of(*args, **kwargs)
    env.globals["index_of"] = index_of_tracked
    vary_fields: set[str] = set()
    env.globals["client_ip_match_any"] = make_client_ip_match_any(request, vary_fields)

    def _finalize_headers(extra: dict | None = None) -> dict:
        headers = {**link_headers}
        if extra:
            headers.update(extra)
        if vary_fields:
            headers["Vary"] = ", ".join(sorted(vary_fields))
        return headers

    def _cacheable_headers_and_304() -> tuple[dict, Response | None]:
        etag = f'"{int(mtime)}"'
        headers = _finalize_headers({
            "Last-Modified": formatdate(mtime, usegmt=True),
            "ETag": etag,
            "Cache-Control": "no-cache",
        })
        # 304 check: ETag takes priority over Last-Modified
        if_none_match = request.headers.get("if-none-match", "")
        if if_none_match:
            if any(e.strip() == etag for e in if_none_match.split(",")):
                return headers, Response(status_code=304, headers=headers)
        elif ims := request.headers.get("if-modified-since", ""):
            try:
                if int(mtime) <= int(parsedate_to_datetime(ims).timestamp()):
                    return headers, Response(status_code=304, headers=headers)
            except Exception:
                pass
        return headers, None

    try:
        tmpl = env.get_template(template_name)
    except Exception:
        # テンプレートが取得できない場合は本文のみを返す。index_of は未呼出なのでキャッシュ可能
        cache_headers, not_modified = _cacheable_headers_and_304()
        if not_modified is not None:
            return not_modified
        return HTMLResponse(body_html, headers=cache_headers)
    html_body = tmpl.render(body=body_html, **vars_)

    if index_of_used:
        # ディレクトリ配下の更新を反映するため、検証子を付けず常に再取得させる
        cache_headers = _finalize_headers({"Cache-Control": "no-store"})
        return HTMLResponse(html_body, headers=cache_headers)

    cache_headers, not_modified = _cacheable_headers_and_304()
    if not_modified is not None:
        return not_modified
    return HTMLResponse(html_body, headers=cache_headers)


LLM_CRAWLERS = ("ClaudeBot", "GPTBot", "ChatGPT-User", "PerplexityBot", "meta-externalagent", "Bytespider", "OAI-SearchBot", "Amazonbot")
EAGER_CRAWLERS = ("MJ12bot", "trendictionbot", "Baiduspider", "Sogou web spider", "SemrushBot", "Faraday", "python-requests", "python-httpx", "Go-http-client", "Mediatoolkitbot", "Barkrowler", "ICC-Crawler", "AhrefsBot", "PetalBot", "YandexBot", "Presto", "DuckDuckBot", "Iframely", "YaBrowser")


def is_llm_crawler(request: Request) -> bool:
    ua = request.headers.get("user-agent", "")
    return any(bot in ua for bot in LLM_CRAWLERS + EAGER_CRAWLERS)


def match_vhost(hostname: str) -> tuple[Path | None, str | None]:
    """Return (vhost_dir, redirect_host). vhost_dir is None if no named vhost matches."""
    exact = VHOSTS_DIR / hostname
    if exact.is_dir():
        return exact, None
    if hostname.startswith("www."):
        bare = hostname[4:]
        candidate = VHOSTS_DIR / bare
        if candidate.is_dir():
            return candidate, bare
    else:
        www = f"www.{hostname}"
        candidate = VHOSTS_DIR / www
        if candidate.is_dir():
            return candidate, www
    return None, None


def resolve_vhost(request: Request) -> tuple[Path | None, str | None]:
    """Return (vhost_dir, redirect_host), respecting VHOST_OVERRIDE."""
    if VHOST_OVERRIDE is not None:
        exact = VHOSTS_DIR / VHOST_OVERRIDE
        return (exact if exact.is_dir() else None), None
    hostname = request.headers.get("host", "").split(":")[0]
    return match_vhost(hostname)


def make_jinja_env(vhost_dir: Path | None) -> Environment:
    loaders = []
    if vhost_dir is not None:
        vhost_templates = vhost_dir / "templates"
        if vhost_templates.is_dir():
            loaders.append(FileSystemLoader(str(vhost_templates)))
    loaders.append(FileSystemLoader(str(TOP_TEMPLATES_DIR)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html"]))
    env.globals["pygments_css"] = PYGMENTS_CSS
    return env


def _get_logs_dir(vhost_dir: Path | None) -> Path:
    d = (vhost_dir / "logs") if vhost_dir else TOP_LOGS_DIR
    d.mkdir(exist_ok=True)
    return d


class _SizeThresholdTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """TimedRotatingFileHandler that skips rollover when file is smaller than min_bytes."""

    def __init__(self, *args, min_bytes: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_bytes = min_bytes

    def shouldRollover(self, record) -> bool:
        if not super().shouldRollover(record):
            return False
        try:
            size_ok = Path(self.baseFilename).stat().st_size >= self.min_bytes
        except FileNotFoundError:
            size_ok = False
        if not size_ok:
            # Advance rolloverAt so we don't re-evaluate on every subsequent write
            self.rolloverAt = self.computeRollover(int(time.time()))
            return False
        return True


def get_access_logger(vhost_dir: Path | None) -> logging.Logger:
    key = vhost_dir.name if vhost_dir else "_top"
    logger = logging.getLogger(f"access.{key}")
    if not logger.handlers:
        handler = _SizeThresholdTimedRotatingFileHandler(
            _get_logs_dir(vhost_dir) / "access_log",
            when="midnight", backupCount=30, encoding="utf-8",
            min_bytes=LOG_ROTATION_MIN_BYTES,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def apache_combined_log(request: Request, status: int, size: int, vhost_dir: Path | None) -> None:
    remote = get_client_ip(request)
    t = time.strftime("%d/%b/%Y:%H:%M:%S %z")
    path = request.url.path
    qs = f"?{request.url.query}" if request.url.query else ""
    proto = request.scope.get("http_version", "1.1")
    referer = request.headers.get("referer", "-")
    ua = request.headers.get("user-agent", "-")
    line = (
        f'{remote} - - [{t}] "{request.method} {path}{qs} HTTP/{proto}" '
        f'{status} {size} "{referer}" "{ua}"'
    )
    get_access_logger(vhost_dir).info(line) if FILE_LOGGING_ENABLED else None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

_xsrf_tokens: dict[str, float] = {}
XSRF_TTL = 3600


def _purge_xsrf_tokens() -> None:
    cutoff = time.monotonic() - XSRF_TTL
    expired = [t for t, ts in _xsrf_tokens.items() if ts < cutoff]
    for t in expired:
        del _xsrf_tokens[t]


def _consume_xsrf_token(token: str) -> bool:
    _purge_xsrf_tokens()
    if token in _xsrf_tokens:
        del _xsrf_tokens[token]
        return True
    return False


def load_api_settings(vhost_dir: Path | None) -> dict:
    path = (vhost_dir / "docs" / "api" / "settings.json") if vhost_dir else (TOP_DOCS_DIR / "api" / "settings.json")
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def send_inquiry_email(data: dict, settings: dict, vhost_dir: Path | None, extra_headers: dict | None = None, subject_prefix: str = "") -> None:
    smtp_cfg = settings.get("smtp", {})
    inquiry_cfg = settings.get("inquiry", {})

    template_name = inquiry_cfg.get("template")
    if template_name:
        env = make_jinja_env(vhost_dir)
        try:
            rendered = env.get_template(template_name).render(**data)
        except Exception:
            rendered = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        rendered = json.dumps(data, ensure_ascii=False, indent=2)

    first_line, _, rest = rendered.partition("\n")
    if first_line.startswith("Subject:"):
        subject = first_line[len("Subject:"):].strip()
        body = rest.lstrip("\n")
    else:
        subject = inquiry_cfg.get("default_subject", "お問い合わせ")
        body = rendered

    if subject_prefix:
        subject = f"{subject_prefix} {subject}"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.get("from", smtp_cfg.get("username", ""))
    msg["To"] = inquiry_cfg.get("to", "")

    if extra_headers:
        for k, v in extra_headers.items():
            msg[k] = v

    host = smtp_cfg.get("host", "localhost")
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")
    use_tls = smtp_cfg.get("use_tls", True)

    with smtplib.SMTP(host, port) as s:
        if use_tls:
            s.starttls()
        if username:
            s.login(username, password)
        s.send_message(msg)


async def api_inquiry(request: Request) -> Response:
    vhost_dir, _ = resolve_vhost(request)
    settings = load_api_settings(vhost_dir)

    if not settings.get("inquiry") or not settings.get("smtp"):
        return JSONResponse({"error": "not configured"}, status_code=404)

    if request.method == "GET":
        token = secrets.token_urlsafe(32)
        _xsrf_tokens[token] = time.monotonic()
        return JSONResponse({"token": token})

    if request.method == "POST":
        token = request.headers.get("x-xsrf-token", "")
        if not _consume_xsrf_token(token):
            return JSONResponse({"error": "invalid or expired token"}, status_code=403)
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        inquiry_cfg = settings.get("inquiry", {})
        honeypot = inquiry_cfg.get("honeypot")
        is_bot = bool(honeypot and data.get(honeypot))
        honeypot_action = inquiry_cfg.get("honeypot_action", "drop")

        if not is_bot:
            email_blacklist = inquiry_cfg.get("email_blacklist", [])
            if email_blacklist:
                candidates = [str(v).strip() for v in data.values() if isinstance(v, str) and "@" in v]
                for addr in candidates:
                    addr_lower = addr.lower()
                    for entry in email_blacklist:
                        entry_lower = str(entry).strip().lower()
                        if entry_lower.startswith("@"):
                            if addr_lower.endswith(entry_lower):
                                is_bot = True
                                break
                        else:
                            if addr_lower == entry_lower:
                                is_bot = True
                                break
                    if is_bot:
                        break

        if is_bot and honeypot_action == "drop":
            return JSONResponse({"ok": True})

        try:
            extra_headers = {}
            subject_prefix = ""
            if is_bot:
                extra_headers["Importance"] = "low"
                extra_headers["X-Priority"] = "5"
                subject_prefix = inquiry_cfg.get("honeypot_subject_prefix", "[Bot]")
            send_inquiry_email(data, settings, vhost_dir, extra_headers=extra_headers, subject_prefix=subject_prefix)
        except Exception as e:
            logging.getLogger("error").error("inquiry email failed: %s", e)
            return JSONResponse({"error": "failed to send"}, status_code=500)
        return JSONResponse({"ok": True})

    return Response("Method Not Allowed", status_code=405)


async def handle_request(request: Request) -> Response:
    vhost_dir, redirect_host = resolve_vhost(request)

    if redirect_host is not None:
        url = request.url.replace(netloc=redirect_host)
        return RedirectResponse(str(url), status_code=301)

    path = request.url.path.lstrip("/")

    if path.startswith("api/") or path == "api":
        return JSONResponse({"error": "not found"}, status_code=404)

    if Path(path).name == "defaults.json":
        return Response("Forbidden", status_code=403, media_type="text/plain")

    # defaults.json is scoped to the vhost's docs if a vhost exists, else top-level docs
    defaults_docs_dir = (vhost_dir / "docs") if vhost_dir else TOP_DOCS_DIR

    # file lookup: vhost docs first, then top-level fallback
    lookup_docs = [vhost_dir / "docs", TOP_DOCS_DIR] if vhost_dir else [TOP_DOCS_DIR]

    # RSS feed
    if path == "sitemap.xml":
        return generate_sitemap(defaults_docs_dir, lookup_docs, request)
    if path.endswith("feed.xml"):
        url_dir = path[:-len("feed.xml")].rstrip("/")
        return generate_feed(url_dir, defaults_docs_dir, lookup_docs, request)

    # Markdown → HTML (.html → .md)
    if path.endswith(".html"):
        md_rel = path[:-5] + ".md"
        for docs_dir in lookup_docs:
            md_path = docs_dir / md_rel
            if md_path.is_file():
                if is_llm_crawler(request):
                    md_url = request.url.replace(path="/" + md_rel)
                    return RedirectResponse(str(md_url), status_code=302, headers={"Vary": "User-Agent"})
                return render_md_file(md_path, defaults_docs_dir, md_rel, vhost_dir, lookup_docs, request)

    # Static files and directory indexes
    for docs_dir in lookup_docs:
        target = docs_dir / path
        if target.is_file():
            if _is_image_resize_request(path, request):
                return render_resized_image(target, request)
            if path.endswith(".md"):
                md_rel = path
                defaults = load_defaults(defaults_docs_dir, md_rel)
                raw_fm = extract_front_matter_raw(target.read_text(encoding="utf-8"))
                merged = {**defaults, **raw_fm}
                return FileResponse(str(target), headers=_link_headers(merged.get("rss"), merged.get("sitemap")))
            return FileResponse(str(target))
        if target.is_dir():
            if not request.url.path.endswith("/"):
                redirect_url = request.url.replace(path=request.url.path + "/")
                return RedirectResponse(str(redirect_url), status_code=301)
            index_md = target / "index.md"
            index_html = target / "index.html"
            if is_llm_crawler(request) and index_md.is_file():
                md_rel = str(Path(path) / "index.md") if path else "index.md"
                defaults = load_defaults(defaults_docs_dir, md_rel)
                raw_fm = extract_front_matter_raw(index_md.read_text(encoding="utf-8"))
                merged = {**defaults, **raw_fm}
                return FileResponse(str(index_md), media_type="text/markdown", headers=_link_headers(merged.get("rss"), merged.get("sitemap")))
            if index_html.is_file():
                return FileResponse(str(index_html))
            if index_md.is_file():
                md_rel = str(Path(path) / "index.md")
                return render_md_file(index_md, defaults_docs_dir, md_rel, vhost_dir, lookup_docs, request)
            break  # directory found but no index; don't fall back to next docs_dir

    # Check for moved redirects before returning 404
    for docs_dir in lookup_docs:
        redirect_to = check_moved_redirect(docs_dir, path)
        if redirect_to is not None:
            return RedirectResponse(redirect_to, status_code=301)

    if path.endswith(".html"):
        return render_error(404, vhost_dir, request)
    return Response("Not Found", status_code=404, media_type="text/plain")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)

        vhost_dir, _ = resolve_vhost(request)

        size = int(response.headers.get("content-length", 0))
        apache_combined_log(request, response.status_code, size, vhost_dir)
        return response


app = Starlette(
    routes=[
        Route("/api/inquiry", api_inquiry, methods=["GET", "POST"]),
        Route("/{path:path}", handle_request),
        Route("/", handle_request),
    ],
    middleware=[Middleware(LoggingMiddleware)],
)

if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--unix-socket", metavar="PATH", help="Listen on a Unix domain socket instead of host:port")
    parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload (for production)")
    parser.add_argument("--file-logging", action="store_true", help="Write access log to logs/ files and suppress uvicorn access log (for production/systemd)")
    parser.add_argument("--log-rotation-min-bytes", type=int, default=1024 * 1024, metavar="BYTES",
                        help="Skip log rotation if access_log is smaller than this size (default: 1048576 = 1MB)")
    parser.add_argument("--vhost-override", metavar="HOSTNAME", help="Force a specific vhost regardless of Host header (dev only)")
    args = parser.parse_args()
    if args.vhost_override:
        os.environ["CMS_VHOST_OVERRIDE"] = args.vhost_override
    if args.file_logging:
        os.environ["CMS_FILE_LOGGING"] = "1"
    os.environ["CMS_LOG_ROTATION_MIN_BYTES"] = str(args.log_rotation_min_bytes)
    run_kwargs: dict = dict(
        reload=not args.no_reload,
        access_log=not args.file_logging,
    )
    if args.unix_socket:
        run_kwargs["uds"] = args.unix_socket
    else:
        run_kwargs["host"] = args.host
        run_kwargs["port"] = args.port
    uvicorn.run("app:app", **run_kwargs)
