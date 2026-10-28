# web_search.py
"""
对外接口：
  run_web_search(query, max_results=None, aborted=None)
  run_read_article(identifier)
  format_search_results(results, failed_sources)
"""
import re
import time
import threading
from urllib.parse import urlparse
import base64

import requests
import html
import random

from internet_settings import InternetSettings
from ssrfguard import Policy
from ssrfguard.requests import Session as GuardedSession

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_MAX_TOTAL_RESULTS = 12
_CONNECT_TIMEOUT = 5


# ==================== 异常类型 ====================

class SearchSourceError(Exception):
    pass


class TavilyRateLimitError(SearchSourceError):
    pass


class TavilyUnavailableError(SearchSourceError):
    pass


# ==================== 通用工具 ====================

def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[内容过长已截断]"


def _normalize_query(query):
    return re.sub(r"\s+", " ", str(query or "").strip().lower())


def _host_of(url):
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def _clean_bing_url(url: str) -> str:
    if not url:
        return url
    if "bing.com/ck/a" in url:
        from urllib.parse import urlparse, parse_qs
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            u_param = qs.get("u")
            if u_param:
                b64_str = u_param[0]
                if b64_str.startswith("a1"):
                    b64_str = b64_str[2:]
                b64_str += "=" * (-len(b64_str) % 4)
                real_url = base64.urlsafe_b64decode(b64_str).decode("utf-8")
                if real_url.startswith("http"):
                    return real_url
        except Exception:
            pass
    return url

# ==================== TTL 缓存 ====================

class _TTLCache:
    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            value, expires_at = item
            if time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key, value, ttl):
        with self._lock:
            self._store[key] = (value, time.time() + max(1, ttl))
            if len(self._store) > 128:
                now = time.time()
                for k in [k for k, (_, exp) in self._store.items() if now > exp]:
                    self._store.pop(k, None)


# ==================== 结果引用注册表 ====================

class _ResultRegistry:
    def __init__(self, max_entries=200):
        self._entries = {}
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def register(self, ref_id, info):
        with self._lock:
            self._entries[ref_id] = info
            while len(self._entries) > self._max_entries:
                first_key = next(iter(self._entries))
                del self._entries[first_key]

    def resolve(self, ref_id):
        with self._lock:
            return self._entries.get(ref_id)


# ==================== 适配器基类 ====================

class BaseSourceAdapter:
    source_type = "base"

    def __init__(self, source_config):
        self.config = source_config or {}
        self.source_id = str(self.config.get("id", ""))
        self.display_name = str(self.config.get("display_name") or self.source_id)

    def _session(self):
        settings = InternetSettings.get_instance()
        s = GuardedSession(policy=Policy())
        s.headers.update({"User-Agent": _USER_AGENT})
        proxies = settings.proxies
        if proxies:
            s.proxies.update(proxies)
        return s

    def _timeout(self):
        return (_CONNECT_TIMEOUT, InternetSettings.get_instance().request_timeout)

    def search(self, query, max_results):
        raise NotImplementedError

    def fetch_article(self, info, max_chars):
        raise NotImplementedError


# ==================== Tavily 适配器 ====================

class TavilyAdapter(BaseSourceAdapter):
    source_type = "tavily"
    SEARCH_URL = "https://api.tavily.com/search"
    EXTRACT_URL = "https://api.tavily.com/extract"

    def __init__(self, source_config=None):
        super().__init__(source_config or {"id": "tavily", "display_name": "Tavily"})

    def _headers(self):
        settings = InternetSettings.get_instance()
        headers = {"Content-Type": "application/json"}
        api_key = settings.tavily_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["X-Tavily-Access-Mode"] = "keyless"
        return headers

    def search(self, query, max_results):
        session = self._session()
        payload = {
            "query": query,
            "max_results": int(max_results) if max_results else 5,
        }
        try:
            resp = session.post(
                self.SEARCH_URL,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout(),
            )
        except requests.exceptions.RequestException as e:
            raise TavilyUnavailableError(f"Tavily 网络异常: {e}")
        finally:
            session.close()

        if resp.status_code == 429:
            raise TavilyRateLimitError("Tavily 触发限流 (HTTP 429)")
        if resp.status_code in (401, 403):
            raise TavilyUnavailableError(f"Tavily 鉴权失败 (HTTP {resp.status_code})")
        if resp.status_code >= 500:
            raise TavilyUnavailableError(f"Tavily 服务端错误 (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise TavilyUnavailableError(f"Tavily 返回异常状态码 {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            raise TavilyUnavailableError("Tavily 返回非 JSON 内容")

        results = []
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", "")).strip()
            content = str(item.get("content", "")).strip()
            if not url and not title:
                continue
            results.append({
                "title": title,
                "url": url,
                "snippet": _strip_html(content),
                "fetch_info": {"kind": "tavily_url", "url": url},
            })
            if len(results) >= max_results:
                break
        return results

    def fetch_article(self, info, max_chars):
        url = str(info.get("url", "")).strip()
        if not url:
            return None

        session = self._session()
        try:
            resp = session.post(
                self.EXTRACT_URL,
                headers=self._headers(),
                json={"urls": [url]},
                timeout=self._timeout(),
            )
        except requests.exceptions.RequestException as e:
            print(f"[WebSearch] Tavily extract 网络异常: {e}")
            return None
        finally:
            session.close()

        if resp.status_code == 429:
            print("[WebSearch] Tavily extract 触发限流 (HTTP 429)，正文不可得")
            return None
        if resp.status_code != 200:
            print(f"[WebSearch] Tavily extract 返回 {resp.status_code}")
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        results = data.get("results") or []
        if not results or not isinstance(results[0], dict):
            return None

        raw = results[0].get("raw_content") or ""
        if not str(raw).strip():
            return None
        return _truncate(str(raw), max_chars)


# ==================== Bing site: 通用 / 兜底适配器 ====================

class BingSiteSearchAdapter(BaseSourceAdapter):
    source_type = "site_search"
    SEARCH_URL = "https://cn.bing.com/search"

    def search(self, query, max_results):
        domain = str(self.config.get("target_domain", "")).strip()
        use_site = self.config.get("use_site_operator", True)
        full_query = f"site:{domain} {query}" if (domain and use_site) else query
        session = self._session()
        session.headers.update({
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        })
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"q": full_query, "count": max_results, "mkt": "zh-CN"},
                timeout=self._timeout(),
            )
            if resp.status_code != 200:
                raise SearchSourceError(f"HTTP {resp.status_code}")
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return self._parse_results(resp.text, max_results)
        except SearchSourceError:
            raise
        except Exception as e:
            raise SearchSourceError(f"请求失败: {e}")
        finally:
            session.close()

    def _parse_results(self, html, max_results):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise SearchSourceError("缺少依赖 beautifulsoup4")
        soup = BeautifulSoup(html, "html.parser")
        results = []

        for ans in soup.select("div.b_ans, div.b_vidAns, div.b_entityTP"):
            h2 = ans.find("h2")
            if not h2:
                continue
            a = h2.find("a")
            if not a or not a.get("href"):
                continue
            snippet_el = ans.select_one("div.b_caption p") or ans.find("p")
            real_href = _clean_bing_url(a["href"])
            if not real_href.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                real_href = urljoin("https://www.bing.com", real_href)

            results.append({
                "title": h2.get_text(strip=True),
                "url": real_href,
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else ans.get_text(" ", strip=True)[:300],
                "fetch_info": {"kind": "generic_url", "url": real_href},
            })
            if len(results) >= max_results:
                return results

        for li in soup.select("li.b_algo"):
            h2 = li.find("h2")
            if not h2:
                continue
            a = h2.find("a")
            if not a or not a.get("href"):
                continue
            snippet_el = li.select_one("div.b_caption p") or li.find("p")

            raw_href = a["href"]
            real_href = _clean_bing_url(raw_href)

            if not real_href.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                real_href = urljoin("https://cn.bing.com", real_href)

            results.append({
                "title": a.get_text(strip=True),
                "url": real_href,
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                "fetch_info": {"kind": "generic_url", "url": real_href},
            })
            if len(results) >= max_results:
                break
        return results

    def fetch_article(self, info, max_chars):
        url = info.get("url", "")
        if not url:
            return None
        session = self._session()
        try:
            resp = session.get(url, timeout=self._timeout(), allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                print(f"[WebSearch] 目标返回重定向 ({resp.status_code})，未经验证的目标不跟随")
                return None
            if resp.status_code != 200:
                return None
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            text = _extract_page_text(resp.text, max_chars)
            if not text or len(text.strip()) < 50:
                return None
            return text
        except Exception as e:
            if "ssrf" in type(e).__name__.lower() or "blocked" in str(e).lower():
                print(f"[WebSearch] SSRF 拦截: {e}")
            else:
                print(f"[WebSearch] 网页抓取失败 {url}: {e}")
            return None
        finally:
            session.close()


# ==================== Bing 新闻适配器 ====================

class BingNewsAdapter(BaseSourceAdapter):
    source_type = "news_search"
    SEARCH_URL = "https://cn.bing.com/news/search"

    def search(self, query, max_results):
        session = self._session()
        session.headers.update({
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        })
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"q": query, "count": max_results, "mkt": "zh-CN"},
                timeout=self._timeout(),
            )
            if resp.status_code != 200:
                raise SearchSourceError(f"HTTP {resp.status_code}")
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return self._parse_news(resp.text, max_results)
        except SearchSourceError:
            raise
        except Exception as e:
            raise SearchSourceError(f"请求失败: {e}")
        finally:
            session.close()

    def _parse_news(self, html, max_results):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise SearchSourceError("缺少依赖 beautifulsoup4")
        soup = BeautifulSoup(html, "html.parser")
        results = []

        cards = soup.select("div.news-card") or soup.select("div.caption")
        for card in cards:
            title = (card.get("data-title") or "").strip()
            url = (card.get("data-url") or "").strip()
            url = _clean_bing_url(url)
            if not title or not url:
                a = card.select_one("a.title") or card.find("a")
                if a:
                    title = title or a.get_text(strip=True)
                    url = url or (a.get("href") or "")
            if not title or not url:
                continue
            snippet_el = card.select_one("div.snippet") or card.find("p")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            publisher_el = card.select_one("cite") or card.select_one("div.source") or card.select_one("span.source")
            publisher = publisher_el.get_text(" ", strip=True) if publisher_el else ""
            if publisher:
                if publisher.startswith("www."):
                    publisher = publisher[4:]
                snippet = f"来源站点：{publisher} | {snippet}" if snippet else f"来源站点：{publisher}"
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "fetch_info": {"kind": "generic_url", "url": url},
            })
            if len(results) >= max_results:
                break

        if not results:
            for li in soup.select("li.b_algo"):
                h2 = li.find("h2")
                if not h2:
                    continue
                a = h2.find("a")
                if not a or not a.get("href"):
                    continue
                snippet_el = li.select_one("div.b_caption p") or li.find("p")
                real_href = _clean_bing_url(a["href"])
                results.append({
                    "title": a.get_text(strip=True),
                    "url": a["href"],
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                    "fetch_info": {"kind": "generic_url", "url": a["href"]},
                })
                if len(results) >= max_results:
                    break

        return results

    def fetch_article(self, info, max_chars):
        return BingSiteSearchAdapter(self.config).fetch_article(info, max_chars)


# ==================== 百度直搜适配器 ====================

class BaiduSearchAdapter(BaseSourceAdapter):
    source_type = "baidu_search"
    SEARCH_URL = "https://www.baidu.com/s"

    def search(self, query, max_results):
        session = self._session()
        session.headers.update({
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.baidu.com/",
        })
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"wd": query, "rn": max_results},
                timeout=self._timeout(),
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                raise SearchSourceError(f"重定向({resp.status_code})")
            if resp.status_code != 200:
                raise SearchSourceError(f"HTTP {resp.status_code}")
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return self._parse_results(resp.text, max_results)
        except SearchSourceError:
            raise
        except Exception as e:
            raise SearchSourceError(f"请求失败: {e}")
        finally:
            session.close()

    def _parse_results(self, html, max_results):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise SearchSourceError("缺少依赖 beautifulsoup4")

        # 反爬/验证页特征
        if ("百度安全验证" in html
                or "wappass.baidu.com" in html
                or "security.baidu.com" in html):
            raise SearchSourceError("触发百度安全验证")

        soup = BeautifulSoup(html, "html.parser")
        results = []
        for container in soup.select("div.result, div.c-container, div.result-op"):
            h3 = container.find("h3")
            if not h3:
                continue
            a = h3.find("a")
            if not a or not a.get("href"):
                continue
            href = str(a["href"]).strip()
            if not href.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                href = urljoin("https://www.baidu.com", href)
            snippet_el = container.select_one(
                "div.c-abstract, span.content-right_1THTn, div.c-span-last"
            )
            title = h3.get_text(strip=True)
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not title:
                continue
            results.append({
                "title": title,
                "url": href,
                "snippet": snippet,
                "fetch_info": {"kind": "generic_url", "url": href},
            })
            if len(results) >= max_results:
                break

        if not results:
            raise SearchSourceError("未解析到结果")
        return results

    def fetch_article(self, info, max_chars):
        return BingSiteSearchAdapter(self.config).fetch_article(info, max_chars)


# ==================== 搜狗直搜适配器 ====================

class SogouSearchAdapter(BaseSourceAdapter):
    source_type = "sogou_search"
    SEARCH_URL = "https://www.sogou.com/web"

    def search(self, query, max_results):
        session = self._session()
        ua = _USER_AGENT
        if "Edg/" in ua:
            ua = ua.split(" Edg/")[0].rstrip() + " Safari/537.36"
        session.headers.update({
            "User-Agent": ua,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.sogou.com/",
        })
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"query": query, "num": max_results},
                timeout=self._timeout(),
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                raise SearchSourceError(f"重定向({resp.status_code})")
            if resp.status_code != 200:
                raise SearchSourceError(f"HTTP {resp.status_code}")
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return self._parse_results(resp.text, max_results)
        except SearchSourceError:
            raise
        except Exception as e:
            raise SearchSourceError(f"请求失败: {e}")
        finally:
            session.close()

    def _parse_results(self, html, max_results):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise SearchSourceError("缺少依赖 beautifulsoup4")

        if ("请输入验证码" in html
                or "antispider" in html
                or "checkcode" in html):
            raise SearchSourceError("触发搜狗反爬验证")

        soup = BeautifulSoup(html, "html.parser")
        results = []
        for container in soup.select("div.vrwrap, div.rb, li.result"):
            h3 = container.find("h3")
            if not h3:
                continue
            a = h3.find("a")
            if not a or not a.get("href"):
                continue
            href = str(a["href"]).strip()
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            snippet_el = container.select_one(
                "div.str-text-info, div.ft, div.text-layout"
            )
            title = h3.get_text(strip=True)
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not title:
                continue
            results.append({
                "title": title,
                "url": href,
                "snippet": snippet,
                "fetch_info": {"kind": "generic_url", "url": href},
            })
            if len(results) >= max_results:
                break

        if not results:
            raise SearchSourceError("未解析到结果")
        return results

    def fetch_article(self, info, max_chars):
        return BingSiteSearchAdapter(self.config).fetch_article(info, max_chars)

def _extract_page_text(html, max_chars):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe",
                     "nav", "header", "footer", "form", "aside"]):
        tag.decompose()
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("div", class_=re.compile(r"article|content|post|entry", re.I))
        or soup.body
        or soup
    )
    paragraphs = []
    total = 0
    for node in main.find_all(["h1", "h2", "h3", "p", "li"]):
        text = node.get_text(" ", strip=True)
        if len(text) < 8:
            continue
        paragraphs.append(text)
        total += len(text)
        if total > max_chars * 2:
            break

    if len(paragraphs) < 3:
        text = main.get_text("\n", strip=True)
    else:
        text = "\n".join(paragraphs)

    return _truncate(text, max_chars)


# ==================== 适配器注册 ====================

_ADAPTER_CLASSES = {
    "site_search": BingSiteSearchAdapter,
    "news_search": BingNewsAdapter,
    "baidu_search": BaiduSearchAdapter,
    "sogou_search": SogouSearchAdapter,
}

def _build_adapter(source_config):
    cls = _ADAPTER_CLASSES.get(str(source_config.get("type", "")))
    if cls is None:
        return None
    return cls(source_config)


# ==================== 搜索引擎 ====================

class WebSearchEngine:
    def __init__(self):
        self._cache = _TTLCache()
        self._registry = _ResultRegistry()
        self._ref_counter = 0
        self._lock = threading.Lock()
        self._last_source_request_ts = 0.0

    def _next_ref_id(self, source_id):
        with self._lock:
            self._ref_counter += 1
            return f"{source_id}_{self._ref_counter}"

    # ---------- 搜索 ----------
    def search(self, query, max_results=None, aborted=None):
        settings = InternetSettings.get_instance()
        query = str(query or "").strip()
        if not query:
            return {"ok": False, "error_type": "missing_arg", "error": "query 为空"}

        if not re.search(r'[a-zA-Z]', query) and re.search(r'[\u4e00-\u9fa5]', query):
            query = query.replace(" ", "")

        try:
            per_source = int(max_results) if max_results else settings.max_results_per_source
        except Exception:
            per_source = settings.max_results_per_source
        per_source = max(1, min(per_source, 8))

        tavily_fail_reason = None

        # ===== Step 3: Tavily 优先 =====
        if settings.tavily_enabled:
            raw_tavily, tavily_fail_reason = self._search_via_tavily(
                query, per_source, aborted
            )
            if raw_tavily:
                return self._format_tavily_results(raw_tavily)

            if tavily_fail_reason == "aborted":
                return {
                    "ok": False,
                    "error_type": "aborted",
                    "error": "搜索已被中止。",
                }

            if not settings.tavily_fallback_to_general:
                return {
                    "ok": False,
                    "error_type": f"tavily_{tavily_fail_reason or 'failed'}",
                    "error": (
                        f"Tavily 搜索失败（{tavily_fail_reason or '未知原因'}），"
                        f"且未启用通用搜索兜底。"
                    ),
                    "next_action": "请稍后重试，或在设置中开启通用搜索兜底。",
                }

            print(f"[WebSearch] Tavily 不可用（{tavily_fail_reason}），降级到通用搜索")

        # ===== 通用搜索兜底 =====
        sources = settings.enabled_sources()
        if not sources:
            detail_parts = []
            if tavily_fail_reason:
                detail_parts.append(f"Tavily（{tavily_fail_reason}）")
            detail = "；".join(detail_parts) if detail_parts else "白名单中没有启用任何搜索来源。"
            return {
                "ok": False,
                "error_type": "no_enabled_source",
                "error": f"未获得可用搜索结果。{detail}",
                "next_action": "请告知用户当前未配置可用的搜索网站。",
            }

        all_results = []
        failed_sources = []
        seen_urls = set()
        seen_types = set()

        for source in sources:
            if aborted is not None and aborted():
                break
            if len(all_results) >= _MAX_TOTAL_RESULTS:
                break
            adapter = _build_adapter(source)
            if adapter is None:
                continue
            if adapter.source_type == "site_search" and not adapter.config.get("use_site_operator", True):
                if "site_search_global" in seen_types:
                    continue
                seen_types.add("site_search_global")
            cache_key = (adapter.source_id, _normalize_query(query))
            cached = self._cache.get(cache_key)
            if cached is not None:
                results = cached
            else:
                if self._last_source_request_ts > 0:
                    elapsed = time.time() - self._last_source_request_ts
                    wait_target = random.uniform(3.0, 5.0)
                    if elapsed < wait_target:
                        sleep_sec = wait_target - elapsed
                        if aborted is not None and aborted():
                            break
                        time.sleep(sleep_sec)
                    try:
                        results = adapter.search(query, per_source)
                        self._last_source_request_ts = time.time()
                    except SearchSourceError as e:
                        self._last_source_request_ts = time.time()
                        failed_sources.append(f"{adapter.display_name}（{e}）")
                        continue
                    except Exception as e:
                        self._last_source_request_ts = time.time()
                        print(f"[WebSearch] {adapter.display_name} 搜索异常: {e}")
                        failed_sources.append(f"{adapter.display_name}（搜索异常）")
                        continue
                    if results:
                        self._cache.set(cache_key, results, settings.cache_ttl)
            for r in results:
                if len(all_results) >= _MAX_TOTAL_RESULTS:
                    break
                url = str(r.get("url", "")).strip().rstrip("/")
                if url:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                ref_id = self._next_ref_id(adapter.source_id)
                self._registry.register(ref_id, {
                    "source_id": adapter.source_id,
                    "fetch_info": r.get("fetch_info") or {},
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                })
                all_results.append({
                    "ref_id": ref_id,
                    "source_name": adapter.display_name,
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": _strip_html(r.get("snippet", "")),
                })

        if not all_results:
            detail_parts = []
            if tavily_fail_reason:
                detail_parts.append(f"Tavily（{tavily_fail_reason}）")
            if failed_sources:
                detail_parts.extend(failed_sources)
            detail = "；".join(detail_parts) if detail_parts else "未匹配到内容"
            return {
                "ok": False,
                "error_type": "no_results",
                "error": f"未获得可用搜索结果。{detail}",
                "next_action": "可尝试更换关键词重试一次；仍无结果则如实告知用户未搜到。",
            }

        if tavily_fail_reason:
            failed_sources.append(f"Tavily（{tavily_fail_reason}）")

        return {"ok": True, "results": all_results, "failed_sources": failed_sources}

    # ---------- Tavily 封装 ----------
    def _search_via_tavily(self, query, per_source, aborted):
        settings = InternetSettings.get_instance()

        cache_key = ("tavily", _normalize_query(query))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached, None

        if aborted is not None and aborted():
            return None, "aborted"

        adapter = TavilyAdapter()
        try:
            raw_results = adapter.search(query, per_source)
        except TavilyRateLimitError as e:
            print(f"[WebSearch] Tavily 限流: {e}")
            return None, "rate_limited"
        except TavilyUnavailableError as e:
            print(f"[WebSearch] Tavily 不可用: {e}")
            return None, "unavailable"
        except Exception as e:
            print(f"[WebSearch] Tavily 搜索异常: {e}")
            return None, "exception"

        if aborted is not None and aborted():
            return None, "aborted"

        if not raw_results:
            return None, "no_results"

        self._cache.set(cache_key, raw_results, settings.cache_ttl)
        return raw_results, None

    def _format_tavily_results(self, raw_results):
        all_results = []
        for r in raw_results:
            if len(all_results) >= _MAX_TOTAL_RESULTS:
                break
            ref_id = self._next_ref_id("tavily")
            self._registry.register(ref_id, {
                "source_id": "tavily",
                "fetch_info": r.get("fetch_info") or {"kind": "tavily_url", "url": r.get("url", "")},
                "title": r.get("title", ""),
                "url": r.get("url", ""),
            })
            all_results.append({
                "ref_id": ref_id,
                "source_name": "Tavily",
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": _strip_html(r.get("snippet", "")),
            })
        return {"ok": True, "results": all_results, "failed_sources": []}

    # ---------- 正文读取 ----------
    def read_article(self, identifier):
        settings = InternetSettings.get_instance()
        identifier = str(identifier or "").strip()
        if not identifier:
            return {"ok": False, "error_type": "missing_arg", "error": "identifier 为空"}
        max_chars = settings.max_article_chars

        # 1) 内部引用 ID
        info = self._registry.resolve(identifier)
        if info is not None:
            source_id = info.get("source_id")

            # ===== Step 3: Tavily 分支，跳过白名单与 _adapter_for =====
            if source_id == "tavily":
                adapter = TavilyAdapter()
                try:
                    text = adapter.fetch_article(info.get("fetch_info") or {}, max_chars)
                except Exception as e:
                    print(f"[WebSearch] Tavily 正文读取异常: {e}")
                    text = None
                if not text:
                    return {
                        "ok": False,
                        "error_type": "fetch_failed",
                        "error": "Tavily 正文抓取失败（可能触发限流或页面不可提取）。",
                        "next_action": "依据搜索摘要作答，不要重复重试。",
                    }
                return {
                    "ok": True,
                    "title": info.get("title", ""),
                    "url": info.get("url", ""),
                    "source_name": "Tavily",
                    "text": text,
                }

            # ===== 通用 sources 分支 =====
            adapter = self._adapter_for(source_id)
            if adapter is None:
                return {
                    "ok": False,
                    "error_type": "source_unavailable",
                    "error": "该引用 ID 对应的来源当前未启用。",
                    "next_action": "请重新调用 web_search 获取新的结果列表。",
                }
            target_domain = str(adapter.config.get("target_domain", "")).strip().lower()
            result_host = _host_of(info.get("url", ""))
            is_cross_domain = bool(
                target_domain and result_host and target_domain not in result_host
            )
            if adapter.config.get("no_body") and not is_cross_domain:
                return {
                    "ok": False,
                    "error_type": "body_not_supported",
                    "error": f"{adapter.display_name} 为动态渲染页面，不支持读取正文。",
                    "next_action": "仅依据搜索摘要作答，不要尝试读取正文。",
                }
            try:
                if is_cross_domain and (info.get("fetch_info") or {}).get("kind") == "generic_url":
                    text = BingSiteSearchAdapter(
                        {"id": adapter.source_id, "display_name": adapter.display_name}
                    ).fetch_article({"url": info.get("url", "")}, max_chars)
                else:
                    text = adapter.fetch_article(info.get("fetch_info") or {}, max_chars)
            except Exception as e:
                print(f"[WebSearch] 正文读取异常: {e}")
                text = None
            if not text:
                return {
                    "ok": False,
                    "error_type": "fetch_failed",
                    "error": "正文抓取失败（页面可能为动态渲染或暂时不可用）。",
                    "next_action": "依据搜索摘要作答，不要重复重试。",
                }
            return {
                "ok": True,
                "title": info.get("title", ""),
                "url": info.get("url", ""),
                "source_name": adapter.display_name,
                "text": text,
            }

        # 2) 直接 URL
        if identifier.startswith("http://") or identifier.startswith("https://"):
            if not self._url_allowed(identifier):
                allowed = ", ".join(sorted(self._allowed_hosts())) or "（无）"
                return {
                    "ok": False,
                    "error_type": "url_not_allowed",
                    "error": "该 URL 不在白名单域名内，禁止读取。",
                    "next_action": f"只允许读取白名单域名：{allowed}",
                }
            adapter = BingSiteSearchAdapter({"id": "direct_url", "display_name": "网页"})
            try:
                text = adapter.fetch_article({"url": identifier}, max_chars)
            except Exception:
                text = None
            if not text:
                return {
                    "ok": False,
                    "error_type": "fetch_failed",
                    "error": "正文抓取失败。",
                    "next_action": "依据已有信息作答，不要重复重试。",
                }
            return {
                "ok": True, "title": "", "url": identifier,
                "source_name": "网页", "text": text,
            }

        # 3) 无法识别
        return {
            "ok": False,
            "error_type": "unknown_identifier",
            "error": f"无法识别的标识符：{identifier}",
            "next_action": "请使用 web_search 结果中的 ID（如 tavily_1），或完整的 http/https 链接。",
        }

    # ---------- 内部 ----------
    def _adapter_for(self, source_id):
        settings = InternetSettings.get_instance()
        source = settings.get_source(source_id)
        if not source or not source.get("enabled"):
            return None
        return _build_adapter(source)

    def _allowed_hosts(self):
        settings = InternetSettings.get_instance()
        hosts = set()
        for s in settings.enabled_sources():
            domain = str(s.get("target_domain", "")).strip().lower()
            if domain:
                hosts.add(domain)
            host = _host_of(s.get("api_base", ""))
            if host:
                hosts.add(host)
            for u in (s.get("mirror_api_bases", []) or []):
                host = _host_of(u)
                if host:
                    hosts.add(host)
        return hosts

    def _url_allowed(self, url):
        host = _host_of(url)
        if not host:
            return False
        for allowed in self._allowed_hosts():
            if host == allowed or host.endswith("." + allowed):
                return True
        return False


# ==================== 结果格式化 ====================

def format_search_results(results, failed_sources=None):
    if not results:
        return "未获得搜索结果。"
    settings = InternetSettings.get_instance()
    budget = settings.max_snippet_chars
    per_limit = max(120, budget // len(results))
    lines = []
    for i, r in enumerate(results, 1):
        snippet = (r.get("snippet") or "").strip()
        if len(snippet) > per_limit:
            snippet = snippet[:per_limit] + "…"
        if re.search(r"20[2-9]\d\s*年|\d{1,2}\s*月", snippet):
            snippet = f"{snippet}（含时间信息）"
        lines.append(
            f"[{i}] 来源: {r.get('source_name', '')} | "
            f"标题: {r.get('title', '')} | ID: {r.get('ref_id', '')}\n"
            f"摘要: {snippet}\n"
            f"链接: {r.get('url', '')}"
        )
    if failed_sources:
        lines.append(f"[注意] 以下来源本次未能返回结果：{'；'.join(failed_sources)}")
    lines.append("如需某条结果的正文，调用 read_web_article 并传入对应 ID。")
    lines.append(
        "[作答要求] 从以上摘要中选择与问题最相关的 1-3 条作为依据；"
        "优先使用具体数字、时间、限定条件；不要复述全部结果。"
    )
    return "\n".join(lines)


# ==================== 单例入口 ====================

_engine_instance = None
_engine_lock = threading.Lock()


def get_search_engine():
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = WebSearchEngine()
    return _engine_instance


def run_web_search(query, max_results=None, aborted=None):
    return get_search_engine().search(query, max_results, aborted=aborted)


def run_read_article(identifier):
    return get_search_engine().read_article(identifier)

