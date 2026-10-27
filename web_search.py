# web_search.py
"""
对外接口：
  run_web_search(query, max_results=None, aborted=None)
  run_read_article(identifier)
  format_search_results(results, failed_sources)
"""
import re
import json
import time
import threading
from urllib.parse import urlparse, quote

import requests
import html

from internet_settings import InternetSettings

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "DesktopPet-Shizuku/1.0 (personal desktop assistant)"
)

_MAX_TOTAL_RESULTS = 12
_CONNECT_TIMEOUT = 5

class SearchSourceError(Exception):
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


# ==================== 端点记忆 ====================

class EndpointMemory:
    _success_routes = {}
    _lock = threading.Lock()

    @classmethod
    def order(cls, source_id, candidates):
        cleaned = []
        for u in candidates:
            u = str(u or "").strip().rstrip("/")
            if u and u not in cleaned:
                cleaned.append(u)
        if not cleaned:
            return []
        with cls._lock:
            remembered = cls._success_routes.get(source_id)
        if remembered in cleaned:
            cleaned.remove(remembered)
            return [remembered] + cleaned
        return cleaned

    @classmethod
    def record_success(cls, source_id, url):
        with cls._lock:
            cls._success_routes[source_id] = url

    @classmethod
    def clear(cls):
        with cls._lock:
            cls._success_routes.clear()


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
        s = requests.Session()
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


# ==================== MediaWiki 适配器 ====================

class MediaWikiAdapter(BaseSourceAdapter):
    source_type = "mediawiki"

    def _candidate_endpoints(self):
        candidates = [self.config.get("api_base", "")]
        candidates.extend(self.config.get("mirror_api_bases", []) or [])
        return EndpointMemory.order(self.source_id, candidates)

    def _request(self, params):
        session = self._session()
        try:
            for endpoint in self._candidate_endpoints():
                try:
                    resp = session.get(endpoint, params=params, timeout=self._timeout())
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and "error" not in data:
                            EndpointMemory.record_success(self.source_id, endpoint)
                            return data
                except Exception as e:
                    print(f"[WebSearch] MediaWiki 端点失败 {endpoint}: {e}")
            raise SearchSourceError("所有端点均不可用")
        finally:
            session.close()

    def _page_url(self, title, endpoint):
        if not endpoint:
            return ""
        try:
            base = re.sub(r"/w/api\.php$", "", endpoint)
            base = re.sub(r"/api\.php$", "", base)
            return f"{base}/wiki/{quote(str(title).replace(' ', '_'))}"
        except Exception:
            return ""

    def search(self, query, max_results):
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max_results,
            "format": "json",
            "formatversion": "2",
        }
        data = self._request(params)
        session = self._session()  # 仅用于记录成功端点的展示 URL
        session.close()
        results = []
        success_route = None
        with EndpointMemory._lock:
            success_route = EndpointMemory._success_routes.get(self.source_id)
        for item in (data.get("query", {}).get("search", []) or []):
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title": title,
                "url": self._page_url(title, success_route or self.config.get("api_base", "")),
                "snippet": _strip_html(item.get("snippet", "")),
                "fetch_info": {
                    "kind": "mediawiki",
                    "pageid": item.get("pageid"),
                    "title": title,
                },
            })
        return results

    def fetch_article(self, info, max_chars):
        params = {
            "action": "query",
            "prop": "extracts",
            "exintro": "1",
            "explaintext": "1",
            "format": "json",
            "formatversion": "2",
        }
        pageid = info.get("pageid")
        if pageid:
            params["pageids"] = pageid
        else:
            params["titles"] = info.get("title", "")
            params["redirects"] = "1"
        data = self._request(params)
        pages = data.get("query", {}).get("pages", []) or []
        if not pages:
            return None
        return _truncate(pages[0].get("extract", ""), max_chars)


# ==================== GitHub 适配器 ====================

class GitHubAdapter(BaseSourceAdapter):
    source_type = "github_api"
    API_BASE = "https://api.github.com"

    def _search_via_api(self, query, max_results):
        session = self._session()
        session.headers.update({"Accept": "application/vnd.github+json"})
        try:
            resp = session.get(
                f"{self.API_BASE}/search/repositories",
                params={"q": query, "per_page": max_results},
                timeout=self._timeout(),
            )
            if resp.status_code != 200:
                print(f"[WebSearch] GitHub API 返回 {resp.status_code}")
                return None
            results = []
            for item in (resp.json().get("items", []) or [])[:max_results]:
                full_name = item.get("full_name", "")
                desc = item.get("description") or ""
                parts = [desc] if desc else []
                if item.get("language"):
                    parts.append(f"语言: {item['language']}")
                parts.append(f"stars: {item.get('stargazers_count', 0)}")
                results.append({
                    "title": full_name,
                    "url": item.get("html_url", ""),
                    "snippet": " | ".join(parts),
                    "fetch_info": {"kind": "github_repo", "full_name": full_name},
                })
            return results
        except Exception as e:
            print(f"[WebSearch] GitHub API 搜索异常: {e}")
            return None
        finally:
            session.close()

    def _fallback_site_search(self, query, max_results):
        fallback = BingSiteSearchAdapter({
            "id": self.source_id,
            "display_name": self.display_name,
            "target_domain": self.config.get("target_domain", "github.com"),
        })
        return fallback.search(query, max_results)

    def search(self, query, max_results):
        results = self._search_via_api(query, max_results)
        if results is not None:
            return results
        print("[WebSearch] GitHub 官方 API 不可用，回退 site: 搜索")
        return self._fallback_site_search(query, max_results)

    def fetch_article(self, info, max_chars):
        kind = info.get("kind", "")
        if kind == "generic_url":
            return BingSiteSearchAdapter(self.config).fetch_article(info, max_chars)
        full_name = info.get("full_name", "")
        if not full_name:
            return None
        session = self._session()
        session.headers.update({"Accept": "application/vnd.github.raw"})
        try:
            resp = session.get(
                f"{self.API_BASE}/repos/{full_name}/readme",
                timeout=self._timeout(),
            )
            if resp.status_code != 200:
                return None
            return _truncate(resp.text, max_chars)
        except Exception as e:
            print(f"[WebSearch] GitHub README 读取失败: {e}")
            return None
        finally:
            session.close()


# ==================== Bing site: 兜底适配器 ====================

class BingSiteSearchAdapter(BaseSourceAdapter):
    source_type = "site_search"
    SEARCH_URL = "https://cn.bing.com/search"

    def search(self, query, max_results):
        domain = str(self.config.get("target_domain", "")).strip()
        full_query = f"site:{domain} {query}" if domain else query
        session = self._session()
        session.headers.update({"Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"q": full_query, "count": max_results, "ensearch": "0"},
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
        for li in soup.select("li.b_algo"):
            h2 = li.find("h2")
            if not h2:
                continue
            a = h2.find("a")
            if not a or not a.get("href"):
                continue
            snippet_el = li.select_one("div.b_caption p") or li.find("p")
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
        url = info.get("url", "")
        if not url:
            return None
        session = self._session()
        try:
            resp = session.get(url, timeout=self._timeout())
            if resp.status_code != 200:
                return None
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            text = _extract_page_text(resp.text, max_chars)
            if not text or len(text.strip()) < 50:
                return None
            return text
        except Exception as e:
            print(f"[WebSearch] 网页抓取失败 {url}: {e}")
            return None
        finally:
            session.close()

class BingNewsAdapter(BaseSourceAdapter):
    source_type = "news_search"
    SEARCH_URL = "https://cn.bing.com/news/search"

    def search(self, query, max_results):
        session = self._session()
        session.headers.update({"Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            resp = session.get(
                self.SEARCH_URL,
                params={"q": query, "count": max_results, "ensearch": "0"},
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
        
        # 策略1: 尝试标准新闻卡片结构
        cards = soup.select("div.news-card") or soup.select("div.caption")
        for card in cards:
            title = (card.get("data-title") or "").strip()
            url = (card.get("data-url") or "").strip()
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

        # 策略2: 回退到普通搜索结果 (li.b_algo)
        if not results:
            for li in soup.select("li.b_algo"):
                h2 = li.find("h2")
                if not h2:
                    continue
                a = h2.find("a")
                if not a or not a.get("href"):
                    continue
                snippet_el = li.select_one("div.b_caption p") or li.find("p")
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
    text = "\n".join(paragraphs) if paragraphs else main.get_text("\n", strip=True)
    return _truncate(text, max_chars)


# ==================== 搜索引擎 ====================

_ADAPTER_CLASSES = {
    "mediawiki": MediaWikiAdapter,
    "github_api": GitHubAdapter,
    "site_search": BingSiteSearchAdapter,
    "news_search": BingNewsAdapter, 
}


def _build_adapter(source_config):
    cls = _ADAPTER_CLASSES.get(str(source_config.get("type", "")))
    if cls is None:
        return None
    return cls(source_config)


class WebSearchEngine:
    def __init__(self):
        self._cache = _TTLCache()
        self._registry = _ResultRegistry()
        self._ref_counter = 0
        self._lock = threading.Lock()

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
        sources = settings.enabled_sources()
        if not sources:
            return {
                "ok": False,
                "error_type": "no_enabled_source",
                "error": "白名单中没有启用任何搜索来源。",
                "next_action": "请告知用户当前未配置可用的搜索网站。",
            }
        try:
            per_source = int(max_results) if max_results else settings.max_results_per_source
        except Exception:
            per_source = settings.max_results_per_source
        per_source = max(1, min(per_source, 8))

        all_results = []
        failed_sources = []
        seen_urls = set()
        for source in sources:
            if aborted is not None and aborted():
                break
            if len(all_results) >= _MAX_TOTAL_RESULTS:
                break
            adapter = _build_adapter(source)
            if adapter is None:
                continue
            cache_key = (adapter.source_id, _normalize_query(query))
            cached = self._cache.get(cache_key)
            if cached is not None:
                results = cached
            else:
                try:
                    results = adapter.search(query, per_source)
                except SearchSourceError as e:
                    failed_sources.append(f"{adapter.display_name}（{e}）")
                    continue
                except Exception as e:
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
            detail = "；".join(failed_sources) if failed_sources else "未匹配到内容"
            return {
                "ok": False,
                "error_type": "no_results",
                "error": f"未获得可用搜索结果。{detail}",
                "next_action": "可尝试更换关键词重试一次；仍无结果则如实告知用户未搜到。",
            }
        return {"ok": True, "results": all_results, "failed_sources": failed_sources}

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
            adapter = self._adapter_for(info.get("source_id"))
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
            "next_action": "请使用 web_search 结果中的 ID（如 wiki_zh_3），或完整的 http/https 链接。",
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


# ==================== 独立自测 ====================

if __name__ == "__main__":
    import sys as _sys
    q = " ".join(_sys.argv[1:]) or "Live2D"
    settings = InternetSettings.get_instance()
    print(f"代理: {settings.proxy or '（直连）'}")
    print(f"启用来源: {settings.enabled_source_names()}")
    print("=" * 50)
    res = run_web_search(q)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
    if res.get("ok") and res.get("results"):
        first_ref = res["results"][0]["ref_id"]
        print("=" * 50)
        print(f"读取正文: {first_ref}")
        art = run_read_article(first_ref)
        if art.get("ok"):
            print(f"标题: {art['title']}\n来源: {art['source_name']}")
            print(art["text"][:500])
        else:
            print(json.dumps(art, ensure_ascii=False))
