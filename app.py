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
from email.utils import formatdate, parsedate_to_datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
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
    remote = request.client.host if request.client else ""
    try:
        return any(ipaddress.ip_address(remote) in net for net in TRUSTED_PROXIES)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    remote = request.client.host if request.client else ""
    if _is_trusted_proxy(request):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return remote or "-"


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


def parse_markdown(text: str, md_instance: MarkdownIt | None = None) -> tuple[str, dict]:
    instance = md_instance or _md_base
    tokens = instance.parse(text)
    front_matter_vars = {}
    for token in tokens:
        if token.type == "front_matter":
            try:
                parsed = yaml.safe_load(token.content)
                if isinstance(parsed, dict):
                    front_matter_vars = parsed
            except yaml.YAMLError:
                pass
            break
    body_html = instance.renderer.render(tokens, instance.options, {})
    return body_html, front_matter_vars


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
                _, front_matter = parse_markdown(md_file.read_text(encoding="utf-8"))
                vars_ = {**defaults, **front_matter}
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
            _, front_matter = parse_markdown(md_file.read_text(encoding="utf-8"))
            vars_ = {**defaults, **front_matter}
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


def make_client_ip_match_any(request: Request):
    """Return a client_ip_match_any(patterns) function bound to the current request."""
    def client_ip_match_any(patterns) -> bool:
        client_ip = get_client_ip(request)
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
    if request is not None:
        env.globals["client_ip_match_any"] = make_client_ip_match_any(request)
    try:
        tmpl = env.get_template(f"{status_code}.j2")
        return HTMLResponse(tmpl.render(status_code=status_code), status_code=status_code)
    except Exception:
        return Response(str(status_code), status_code=status_code, media_type="text/plain")


def render_md_file(md_path: Path, defaults_docs_dir: Path, md_rel: str, vhost_dir: Path | None, lookup_docs: list[Path], request: Request) -> Response:
    mtime = md_path.stat().st_mtime
    # テンプレートのmtimeも含めて有効なmtimeを確定する（304判定前に必要）
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

    etag = f'"{int(mtime)}"'
    last_modified = formatdate(mtime, usegmt=True)
    cache_headers = {
        "Last-Modified": last_modified,
        "ETag": etag,
        "Cache-Control": "no-cache",
    }

    # 304 check: ETag takes priority over Last-Modified
    if_none_match = request.headers.get("if-none-match", "")
    if if_none_match:
        if any(e.strip() == etag for e in if_none_match.split(",")):
            return Response(status_code=304, headers=cache_headers)
    elif ims := request.headers.get("if-modified-since", ""):
        try:
            if int(mtime) <= int(parsedate_to_datetime(ims).timestamp()):
                return Response(status_code=304, headers=cache_headers)
        except Exception:
            pass

    container_classes = {**defaults, **raw_fm}.get("container_classes", [])
    body_html, front_matter = parse_markdown(text, make_md(container_classes))
    vars_ = {**defaults, **front_matter}
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

    env.globals["index_of"] = make_index_of(lookup_docs, defaults_docs_dir)
    env.globals["client_ip_match_any"] = make_client_ip_match_any(request)
    try:
        tmpl = env.get_template(template_name)
    except Exception:
        return HTMLResponse(body_html, headers=cache_headers)
    return HTMLResponse(tmpl.render(body=body_html, **vars_), headers=cache_headers)


LLM_CRAWLERS = ("ClaudeBot", "GPTBot", "PerplexityBot", "meta-externalagent", "MJ12bot")


def is_llm_crawler(request: Request) -> bool:
    ua = request.headers.get("user-agent", "")
    return any(bot in ua for bot in LLM_CRAWLERS)


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


def send_inquiry_email(data: dict, settings: dict, vhost_dir: Path | None) -> None:
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

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.get("from", smtp_cfg.get("username", ""))
    msg["To"] = inquiry_cfg.get("to", "")

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
        honeypot = settings.get("inquiry", {}).get("honeypot")
        if honeypot and data.get(honeypot):
            return JSONResponse({"ok": True})
        try:
            send_inquiry_email(data, settings, vhost_dir)
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
                return render_md_file(md_path, defaults_docs_dir, md_rel, vhost_dir, lookup_docs, request)

    # Static files and directory indexes
    for docs_dir in lookup_docs:
        target = docs_dir / path
        if target.is_file():
            return FileResponse(str(target))
        if target.is_dir():
            index_md = target / "index.md"
            index_html = target / "index.html"
            if is_llm_crawler(request) and index_md.is_file():
                return FileResponse(str(index_md), media_type="text/markdown")
            if index_html.is_file():
                return FileResponse(str(index_html))
            if index_md.is_file():
                md_rel = str(Path(path) / "index.md")
                return render_md_file(index_md, defaults_docs_dir, md_rel, vhost_dir, lookup_docs, request)
            break  # directory found but no index; don't fall back to next docs_dir

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
    uvicorn.run(
        "app:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        access_log=not args.file_logging,
    )
