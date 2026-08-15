# 快看漫画 RESTful API - EdgeOne Pages Cloud Functions (Python/Flask)
#
# 路由：/config、/comic/<id>、/comic/<id>/cover、/photo/<id>/chapter/<n>[/<page>.jpg]、/search/<text>/<page>
# 注意：cloud-functions/index.py 与 [[default]].py 内容需保持一致（EdgeOne 双入口）
import json
import logging
import re
import struct
import time
from io import BytesIO
from threading import Lock
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, jsonify, request
from PIL import Image
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.debug = False
app.json.ensure_ascii = False

Image.MAX_IMAGE_PIXELS = 40_000_000  # 防解压炸弹


class WSGIPathFixMiddleware:
    """修复部分 Serverless 运行时（如 EdgeOne）将 URL 解码为 Unicode 后
    直接放入 PATH_INFO/QUERY_STRING 的问题。

    PEP 3333 要求 PATH_INFO 为 latin-1 范围内的 str（表示原始字节），
    Werkzeug 会 encode('latin1') 还原字节。若运行时放入的是真正的
    Unicode 字符串（如 /search/航海王/1），需要重新按 UTF-8 编码
    再按 latin-1 解码，恢复 WSGI 标准格式。
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        for key in ("PATH_INFO", "QUERY_STRING", "SCRIPT_NAME"):
            value = environ.get(key)
            if not value:
                continue
            try:
                value.encode("latin1")
            except UnicodeEncodeError:
                environ[key] = value.encode("utf-8").decode("latin1")
        return self.wsgi_app(environ, start_response)


app.wsgi_app = WSGIPathFixMiddleware(app.wsgi_app)


class ApiError(Exception):
    """业务错误，status 为返回给客户端的 HTTP 状态码"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@app.errorhandler(ApiError)
def handle_api_error(e):
    if e.status >= 500:
        logging.warning(f"上游错误 {e.status}: {e.message}")
    return jsonify({"code": e.status, "message": e.message}), e.status


@app.errorhandler(HTTPException)
def handle_http_error(e):
    """Flask/Werkzeug 自身的 HTTP 错误（如路由未命中 404、方法不允许 405），
    统一按约定 JSON 格式返回"""
    return jsonify({"code": e.code, "message": e.name}), e.code


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    logging.exception("未处理异常")
    return jsonify({"code": 500, "message": "服务器内部错误"}), 500


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def get_api_url():
    """获取对外可访问的 API 基础地址。

    EdgeOne Pages 会将 Host 改写为内部域名，原始访问域名通过 Eo-Pages-Host
    请求头透传（平台强制 HTTPS）。优先级：PUBLIC_URL 环境变量 > Eo-Pages-Host
    > X-Forwarded-* > Host。
    """
    import os

    public = os.environ.get("PUBLIC_URL")
    if public:
        return public.rstrip("/")

    eo_host = request.headers.get("Eo-Pages-Host", "").strip()
    if eo_host:
        return f"https://{eo_host}"

    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if host and proto:
        return f"{proto}://{host}"
    if not host:
        host = request.host
    if not proto:
        proto = request.scheme
    return f"{proto}://{host}"


# 图片地址白名单：仅允许快看漫画相关域名，防止本服务被当作开放代理（SSRF）
ALLOWED_IMAGE_HOSTS = ("kkmh.com", "v3mh.com", "kukacdn.com")


def is_allowed_image_url(url):
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = u.hostname or ""
    return any(host == d or host.endswith("." + d) for d in ALLOWED_IMAGE_HOSTS)


def clamp_int(raw, fallback, min_v, max_v, name):
    if raw is None or raw == "":
        return fallback
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, f"参数 {name} 非法")
    return max(min_v, min(max_v, n))


def is_truthy_arg(name):
    return request.args.get(name, "0").lower() in ("1", "true", "yes", "on")


class TTLCache:
    """TTL 内存缓存：serverless 暖实例间复用，冷启动后自动重建。
    注意：带用户 Cookie 的请求一律不读不写，防止付费内容泄露。"""

    def __init__(self, ttl_seconds, max_size=500):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._data = {}
        self._lock = Lock()

    def get(self, key):
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if entry[0] <= now:
                del self._data[key]
                return None
            return entry[1]

    def set(self, key, value):
        now = time.time()
        with self._lock:
            if len(self._data) >= self.max_size:
                for k in [k for k, v in self._data.items() if v[0] <= now]:
                    del self._data[k]
                if len(self._data) >= self.max_size:
                    self._data.clear()
            self._data[key] = (now + self.ttl, value)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)


topic_cache = TTLCache(10 * 60)  # 漫画详情（含章节列表）10 分钟
chapter_cache = TTLCache(30 * 60)  # 章节图片数据 30 分钟（签名 URL 有效期约 1 小时）


# ---------------------------------------------------------------------------
# 上游抓取
# ---------------------------------------------------------------------------

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "referer": "https://www.kuaikanmanhua.com/",
}
# 不声明 accept-encoding：requests 自动处理 gzip/deflate，
# 手动声明 br 而未安装 brotli 会导致响应乱码


def fetch_text(url, cookie=None, timeout=15, retries=1):
    """抓取文本，统一 UTF-8 解码。仅对网络异常重试，非 200 状态码属确定性错误不重试。"""
    headers = dict(PAGE_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    last_exc = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            continue
        if resp.status_code != 200:
            raise ApiError(502, f"上游请求失败，状态码: {resp.status_code}")
        resp.encoding = "utf-8"  # 统一 UTF-8，避免缺 charset 时中文乱码
        return resp.text
    raise ApiError(502, f"上游请求失败: {last_exc}")


def fetch_json(url, cookie=None, timeout=15, retries=1):
    data = None
    try:
        data = json.loads(fetch_text(url, cookie, timeout, retries))
    except ValueError:
        raise ApiError(502, "上游返回非 JSON 数据")
    if data.get("code") != 200:
        raise ApiError(502, f"上游业务错误: {data.get('message') or data.get('code')}")
    return data


# ---------------------------------------------------------------------------
# window.__NUXT__ 解析：快看 SSR 页面的数据是 IIFE 表达式
#   (function(a,b,...){ aE[0]="都市"; rn.user_id=123; return <字面量> })(实参1,...)
# 函数体内 return 前可能有对实参数组/对象的赋值改写语句，对象内的值多为形参引用。
# 以下为该语法子集的零依赖微型解析执行器：先解析实参构建作用域，
# 再顺序执行改写语句，最后求值 return 表达式。
# 支持字面量：对象/数组/字符串/数字/true/false/null/void 0/!0/!1/形参引用。
# ---------------------------------------------------------------------------


class NuxtParser:
    def __init__(self, text, scope=None):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.scope = scope if scope is not None else {}

    @classmethod
    def parse(cls, text):
        p = cls(text)
        p._skip_ws()
        p._expect("(")
        p._skip_ws()
        p._expect("function")
        p._skip_ws()
        p._expect("(")
        params = []
        while True:
            p._skip_ws()
            if p._peek() == ")":
                p.i += 1
                break
            params.append(p._read_ident())
            p._skip_ws()
            if p._peek() == ",":
                p.i += 1
        p._skip_ws()
        p._expect("{")

        # 字符串感知的平衡扫描，定位函数体结束 }（此时还不知道实参，无法求值）
        body_start = p.i
        depth = 1
        while depth > 0:
            if p.i >= p.n:
                raise ValueError("函数体未闭合")
            ch = p.s[p.i]
            if ch in ('"', "'"):
                p._parse_string()  # 跳过字符串内容
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            p.i += 1
        body = p.s[body_start : p.i - 1]

        p._skip_ws()
        if p._peek() == ")":
            p.i += 1  # 兼容 })(args) 形式
            p._skip_ws()
        p._expect("(")
        args = []
        while True:
            p._skip_ws()
            if p._peek() == ")":
                p.i += 1
                break
            args.append(p._parse_value())
            p._skip_ws()
            if p._peek() == ",":
                p.i += 1

        scope = dict(zip(params, args))
        return cls(body, scope)._execute_body()

    def _execute_body(self):
        """顺序执行改写语句（ident[idx]=值 / ident.key=值），最后求值 return"""
        while True:
            self._skip_ws()
            if self.s.startswith("return", self.i) and not re.match(
                r"[A-Za-z0-9_$]", self.s[self.i + 6 : self.i + 7]
            ):
                self.i += 6
                return self._parse_value()
            name = self._read_ident()
            self._skip_ws()
            if self._peek() == "[":
                self.i += 1
                key = int(self._parse_number())
                self._skip_ws()
                self._expect("]")
            elif self._peek() == ".":
                self.i += 1
                key = self._read_ident()
            else:
                raise ValueError(f"不支持的语句: {self.s[self.i:self.i+30]!r}")
            self._skip_ws()
            self._expect("=")
            value = self._parse_value()
            target = self.scope.get(name)
            if isinstance(target, list) and isinstance(key, int):
                while len(target) <= key:
                    target.append(None)
                target[key] = value
            elif isinstance(target, dict):
                target[key] = value
            else:
                raise ValueError(f"无法改写的目标: {name}")
            self._skip_ws()
            self._expect(";")

    def _peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def _skip_ws(self):
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _expect(self, token):
        if not self.s.startswith(token, self.i):
            raise ValueError(f"预期 {token!r}，实际: {self.s[self.i:self.i+20]!r}")
        self.i += len(token)

    def _read_ident(self):
        m = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", self.s[self.i :])
        if not m:
            raise ValueError(f"预期标识符: {self.s[self.i:self.i+20]!r}")
        self.i += m.end()
        return m.group(0)

    def _parse_value(self):
        self._skip_ws()
        ch = self._peek()
        if ch == "{":
            return self._parse_object()
        if ch == "[":
            return self._parse_array()
        if ch in ('"', "'"):
            return self._parse_string()
        if ch == "-" or ch.isdigit():
            return self._parse_number()
        if ch == "!":
            self.i += 1
            return not self._parse_number()  # !0 -> True, !1 -> False
        word = self._read_ident()
        if word == "true":
            return True
        if word == "false":
            return False
        if word in ("null", "undefined"):
            return None
        if word == "void":
            self._skip_ws()
            self._expect("0")
            return None
        if word == "Array":
            # Array(N) 稀疏数组构造，槽位由函数体赋值语句填充
            self._skip_ws()
            self._expect("(")
            size = int(self._parse_number())
            self._skip_ws()
            self._expect(")")
            return [None] * size
        if word not in self.scope:
            raise ValueError(f"未定义的形参: {word}")
        return self.scope[word]

    def _parse_object(self):
        obj = {}
        self.i += 1  # {
        while True:
            self._skip_ws()
            if self._peek() == "}":
                self.i += 1
                return obj
            if self._peek() in ('"', "'"):
                key = self._parse_string()
            else:
                key = self._read_ident()
            self._skip_ws()
            self._expect(":")
            obj[key] = self._parse_value()
            self._skip_ws()
            if self._peek() == ",":
                self.i += 1

    def _parse_array(self):
        arr = []
        self.i += 1  # [
        while True:
            self._skip_ws()
            if self._peek() == "]":
                self.i += 1
                return arr
            arr.append(self._parse_value())
            self._skip_ws()
            if self._peek() == ",":
                self.i += 1

    def _parse_string(self):
        quote_ch = self.s[self.i]
        self.i += 1
        out = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == "\\":
                nxt = self.s[self.i + 1]
                if nxt == "u":
                    code = int(self.s[self.i + 2 : self.i + 6], 16)
                    self.i += 6
                    # 代理对：高代理后跟 \uDC00-\uDFFF 则合并
                    if 0xD800 <= code <= 0xDBFF and self.s[self.i : self.i + 2] == "\\u":
                        low = int(self.s[self.i + 2 : self.i + 6], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            self.i += 6
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    out.append(chr(code))
                    continue
                if nxt == "x":
                    out.append(chr(int(self.s[self.i + 2 : self.i + 4], 16)))
                    self.i += 4
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(nxt, nxt))
                self.i += 2
                continue
            if ch == quote_ch:
                self.i += 1
                return "".join(out)
            out.append(ch)
            self.i += 1
        raise ValueError("字符串未闭合")

    def _parse_number(self):
        m = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", self.s[self.i :])
        if not m:
            raise ValueError(f"预期数字: {self.s[self.i:self.i+20]!r}")
        self.i += m.end()
        text = m.group(0)
        if "." in text or "e" in text or "E" in text:
            return float(text)
        return int(text)


def extract_nuxt_state(html):
    """从 SSR HTML 中提取并求值 window.__NUXT__，返回 dict 的 data[0]"""
    idx = html.find("window.__NUXT__=")
    if idx == -1:
        raise ApiError(502, "页面结构可能已变更（缺少 __NUXT__）")
    start = html.index("=", idx) + 1
    try:
        state = NuxtParser.parse(html[start:])
    except (ValueError, IndexError) as e:
        raise ApiError(502, f"页面数据解析失败: {e}")
    try:
        return state["data"][0]
    except (KeyError, IndexError, TypeError):
        raise ApiError(502, "页面结构可能已变更（data 缺失）")


def get_topic_bundle(topic_id, cookie=None):
    """获取漫画详情与章节列表（同一 SSR 页面内嵌，一次抓取）。

    返回 {"info": topicInfo, "comics": [...]}。comics 保持官方顺序并重编号 1..N，
    章节 id 非单调递增，不能按 id 排序。
    """
    html = fetch_text(f"https://www.kuaikanmanhua.com/web/topic/{topic_id}/", cookie)
    data = extract_nuxt_state(html)

    info = data.get("topicInfo") or {}
    comics_raw = data.get("comics") or []
    if not isinstance(comics_raw, list):
        raise ApiError(502, "页面结构可能已变更（comics 缺失）")

    comics = []
    seen = set()
    for c in comics_raw:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        comics.append(
            {
                "comic_id": int(cid),
                "title": c.get("title") or f"第{len(comics) + 1}话",
                "locked": bool(c.get("locked")),
            }
        )

    if not info.get("title") and not comics:
        raise ApiError(404, "Comic not found")
    return {"info": info, "comics": comics}


def get_chapter_data(comic_id, cookie=None):
    """获取章节图片地址列表（签名 URL 有效期约 1 小时）"""
    data = fetch_json(f"https://www.kuaikanmanhua.com/v2/pweb/comic/inner/{comic_id}?source=", cookie)
    info = (data.get("data") or {}).get("comic_info") or {}
    if not info.get("id"):
        raise ApiError(404, "Chapter not found")
    if info.get("locked"):
        raise ApiError(403, "章节已锁定（付费内容需登录并购读）")

    images = []
    for img in info.get("comic_images") or []:
        if not isinstance(img, dict):
            continue
        url = img.get("url") or img.get("url1280")
        if url:
            images.append(url)
    if not images:
        raise ApiError(502, "未获取到章节图片")
    return {"title": info.get("title") or "", "images": images}


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def search_comics(keyword):
    """搜索漫画。SSR 搜索页一次返回全部结果，无可用分页参数。"""
    html = fetch_text(f"https://www.kuaikanmanhua.com/sou/{quote(keyword, safe='')}", timeout=12)
    data = extract_nuxt_state(html)
    try:
        hits = ((data.get("originData") or {}).get("topics") or {}).get("hit") or []
    except AttributeError:
        raise ApiError(502, "页面结构可能已变更（搜索结果缺失）")

    results = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        tid, title = h.get("id"), h.get("title")
        if not tid or not title:
            continue
        results.append(
            {
                "comic_id": int(tid),
                "title": title,
                "pages": clamp_int(str(h.get("comic_count") or 0), 0, 0, 100000, "pages"),
            }
        )
    return results


# ---------------------------------------------------------------------------
# 图片处理管线
# ---------------------------------------------------------------------------

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20MB 下载上限
LVGL_MAX_DIM = 2047  # LVGL 头部宽高各占 11 bit

IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.kuaikanmanhua.com/",
}


def fetch_image(image_url, retries=1):
    """下载上游图片。地址来自上游接口数据，仍做白名单校验做纵深防御。"""
    if not is_allowed_image_url(image_url):
        raise ApiError(502, "上游图片地址非法")
    last_exc = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(image_url, headers=IMAGE_HEADERS, timeout=20)
        except requests.RequestException as e:
            last_exc = e
            continue
        if resp.status_code != 200:
            raise ApiError(502, f"图片下载失败: {resp.status_code}")
        declared = int(resp.headers.get("content-length") or 0)
        if declared > MAX_DOWNLOAD_BYTES:
            raise ApiError(413, "图片体积过大")
        data = resp.content
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ApiError(413, "图片体积过大")
        try:
            image = Image.open(BytesIO(data))
            image.load()
        except Exception:
            raise ApiError(502, "图片解码失败")
        return image
    raise ApiError(502, f"图片下载失败: {last_exc}")


def normalize_rgb(image):
    """透明通道铺白底转 RGB"""
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode in ("RGBA", "LA"):
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def convert_to_lvgl8(image):
    """转换为 LVGL indexed-8 二进制：4 字节头 + 256 项 BGRA 调色板 + 每像素 1 字节索引"""
    image = normalize_rgb(image).quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    w, h = image.size
    if w > LVGL_MAX_DIM or h > LVGL_MAX_DIM:
        raise ApiError(413, "图片尺寸超出 LVGL 编码上限")

    raw_palette = image.getpalette()
    out = BytesIO()
    out.write(struct.pack("<I", 10 | (w << 10) | (h << 21)))
    for i in range(256):
        idx = i * 3
        if raw_palette and idx + 2 < len(raw_palette):
            r, g, b = raw_palette[idx], raw_palette[idx + 1], raw_palette[idx + 2]
        else:
            r = g = b = 0
        out.write(bytes([b, g, r, 0xFF]))
    out.write(image.tobytes())
    return out.getvalue()


def process_image(image, width, quality, if_png, if_lvgl):
    """等比缩放（仅缩小）后输出 LVGL / PNG / JPEG，返回 (bytes, content_type)"""
    if if_lvgl:
        w = min(width, image.width)
        h = round(image.height * w / image.width)
        if h > LVGL_MAX_DIM:  # LVGL 头部宽高仅 11 bit，保证缩放后高度也不超限
            h = LVGL_MAX_DIM
            w = max(1, int(image.width * h / image.height))
        image = normalize_rgb(image).resize((w, h), Image.Resampling.LANCZOS)
        return convert_to_lvgl8(image), "application/octet-stream"

    if image.width > width:
        image = image.resize(
            (width, round(image.height * width / image.width)), Image.Resampling.LANCZOS
        )

    out = BytesIO()
    if if_png:
        colors = max(16, min(256, int(16 + quality * 2.4)))
        image = normalize_rgb(image).quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        image.save(out, "PNG", optimize=True, compress_level=9)
        return out.getvalue(), "image/png"

    normalize_rgb(image).save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def image_response(data, content_type):
    return Response(
        data,
        mimetype=content_type,
        headers={"Cache-Control": "public, max-age=86400", "Content-Type": content_type},
    )


def page_image_params(default_width):
    """解析图片参数：width/quality/ifPNG/ifLVGL"""
    width = clamp_int(request.args.get("width") or request.args.get("w"), default_width, 1, 2047, "width")
    quality = clamp_int(request.args.get("quality") or request.args.get("q"), 50, 1, 100, "quality")
    return width, quality, is_truthy_arg("ifPNG"), is_truthy_arg("ifLVGL")


def get_topic_bundle_cached(topic_id, cookie):
    """详情缓存：带 Cookie 的请求不读不写"""
    if cookie:
        return get_topic_bundle(topic_id, cookie)
    bundle = topic_cache.get(topic_id)
    if bundle is None:
        bundle = get_topic_bundle(topic_id, None)
        topic_cache.set(topic_id, bundle)
    return bundle


def resolve_chapter(topic_id, chapter_number, cookie):
    """解析第 chapter_number 章（1 起）的图片数据，返回 (章节信息, 章节数据)"""
    bundle = get_topic_bundle_cached(topic_id, cookie)
    if chapter_number > len(bundle["comics"]):
        raise ApiError(404, f"未找到第 {chapter_number} 章")
    target = bundle["comics"][chapter_number - 1]

    data = None if cookie else chapter_cache.get(target["comic_id"])
    if data is None:
        data = get_chapter_data(target["comic_id"], cookie)
        if not cookie:
            chapter_cache.set(target["comic_id"], data)
    return target, data


@app.get("/")
def read_root():
    return "it works!"


@app.get("/config")
@app.get("/config/")
def config():
    api_url = get_api_url()
    return jsonify(
        {
            "KuaikanComic": {
                "name": "快看漫画",
                "apiUrl": api_url,
                "detailPath": "/comic/<id>",
                "photoPath": "/photo/<id>/chapter/<chapter>",
                "searchPath": "/search/<text>/<page>",
                "type": "kuaikan",
            }
        }
    )


@app.get("/comic/<topic_id>")
@app.get("/comic/<topic_id>/")
def comic_info(topic_id):
    if not topic_id.isdigit():
        raise ApiError(400, "无效的漫画ID")
    cookie = request.headers.get("Cookie") or None
    bundle = get_topic_bundle_cached(topic_id, cookie)
    info = bundle["info"]
    if not bundle["comics"]:
        raise ApiError(404, "Comic not found")

    tags = [t.get("name") if isinstance(t, dict) else t for t in info.get("tags") or []]
    return jsonify(
        {
            "item_id": int(topic_id),
            "name": info.get("title") or "未知标题",
            "page_count": 0,
            "views": info.get("popularity_info") or info.get("likes_count") or "0",
            "cover": f"{get_api_url()}/comic/{topic_id}/cover",
            "tags": [t for t in tags if t],
            "total_chapters": len(bundle["comics"]),
        }
    )


@app.get("/comic/<topic_id>/cover")
def comic_cover(topic_id):
    """漫画封面图片，支持 width/quality/ifPNG 参数"""
    if not topic_id.isdigit():
        raise ApiError(400, "无效的漫画ID")
    cookie = request.headers.get("Cookie") or None
    bundle = get_topic_bundle_cached(topic_id, cookie)
    cover_url = bundle["info"].get("cover_image_url") or bundle["info"].get("vertical_image_url")
    if not cover_url:
        raise ApiError(404, "No cover found")

    width, quality, if_png, _ = page_image_params(200)
    image = fetch_image(cover_url)
    data, content_type = process_image(image, width, quality, if_png=if_png, if_lvgl=False)
    return image_response(data, content_type)


@app.get("/photo/<topic_id>/chapter/<int:chapter_number>")
def chapter_images(topic_id, chapter_number):
    if not topic_id.isdigit():
        raise ApiError(400, "无效的漫画ID")
    if chapter_number < 1:
        raise ApiError(400, "章节号必须从 1 开始")

    cookie = request.headers.get("Cookie") or None
    target, data = resolve_chapter(topic_id, chapter_number, cookie)

    api_url = get_api_url()
    images = [
        {"url": f"{api_url}/photo/{topic_id}/chapter/{chapter_number}/{page}.jpg"}
        for page in range(1, len(data["images"]) + 1)
    ]
    return jsonify({"title": data["title"] or target["title"], "images": images})


@app.get("/photo/<topic_id>/chapter/<int:chapter_number>/<path:page>")
def chapter_page_image(topic_id, chapter_number, page):
    """章节单页图片，格式 /photo/<id>/chapter/<chapter>/<page>.jpg"""
    if not topic_id.isdigit():
        raise ApiError(400, "无效的漫画ID")
    if chapter_number < 1:
        raise ApiError(400, "章节号必须从 1 开始")
    m = re.match(r"^(\d+)", page or "")
    if not m:
        raise ApiError(400, "无效的页码")
    page_num = int(m.group(1))

    cookie = request.headers.get("Cookie") or None
    target, data = resolve_chapter(topic_id, chapter_number, cookie)

    def pick_image_url(chapter_data):
        if page_num < 1 or page_num > len(chapter_data["images"]):
            raise ApiError(404, "Page not found")
        return chapter_data["images"][page_num - 1]

    width, quality, if_png, if_lvgl = page_image_params(600)
    try:
        image = fetch_image(pick_image_url(data))
    except ApiError as e:
        # 签名 URL 过期（缓存数据超过约 1 小时）时刷新章节数据重试一次
        if e.status != 502 or cookie:
            raise
        chapter_cache.delete(target["comic_id"])
        _, data = resolve_chapter(topic_id, chapter_number, cookie)
        image = fetch_image(pick_image_url(data))

    img_data, content_type = process_image(image, width, quality, if_png=if_png, if_lvgl=if_lvgl)
    return image_response(img_data, content_type)


@app.get("/search/<value>")
@app.get("/search/<value>/")
@app.get("/search/<value>/<page>")
def search(value, page="1"):
    # Flask 已对路径参数做过一次 URL 解码，直接使用
    if not page.isdigit() or int(page) < 1:
        raise ApiError(400, "页码必须大于0")
    page = int(page)
    if not value:
        raise ApiError(400, "缺少搜索关键词")

    # 上游 SSR 搜索页一次返回全部结果，无分页参数：仅第 1 页有数据
    results = search_comics(value) if page == 1 else []
    api_url = get_api_url()

    return jsonify(
        {
            "page": page,
            "has_more": False,
            "results": [
                {
                    "comic_id": r["comic_id"],
                    "title": r["title"],
                    "cover_url": f"{api_url}/comic/{r['comic_id']}/cover",
                    "pages": r["pages"],
                }
                for r in results
            ],
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
