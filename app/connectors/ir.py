"""기업 공식 IR(공개 웹페이지) 커넥터.

키가 필요 없다. 사용자가 직접 입력한 공개 IR URL 만 조회한다.
- https 만 허용(평문/비표준 스킴 차단).
- 응답 본문 크기 상한을 두어 비정상 응답으로 메모리를 소모하지 않는다.
- 출처 URL/발행일/본문 구절을 그대로 보존한다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..errors import ConnectorFailure, ERR_INVALID_REQUEST
from ..models import ConnectorError, ProviderName, Provenance, SourceDoc
from ..observability import get_logger
from ..pipeline.parser import (
    extract_metrics_from_text,
    extract_period,
    extract_published_at,
    html_to_text,
    normalize_whitespace,
    split_sentences,
)
from .base import Connector, ConnectorRequest, ConnectorResult
from .http_client import HttpClient

log = get_logger(__name__)

MAX_BYTES = 5 * 1024 * 1024

#: 발행일로 신뢰할 수 있는 메타 태그만 사용한다(일반 'date' 는 푸터 날짜를 잡을 수 있음).
_DATE_META_KEYS = [
    "article:published_time",
    "og:published_time",
    "article:modified_time",
    "publishdate",
    "publication_date",
    "og:updated_time",
]

#: 본문에서 발행일을 찾을 때 붙어 있어야 하는 키워드
_DATE_KEYWORDS = ["게시", "등록", "발행", "작성", "작성일", "입력", "제공", "dated", "published"]


class IrConnector(Connector):
    """기업 공식 IR 페이지 수집기."""

    label = "기업 공식 IR"
    provider = ProviderName.IR
    kind = "webpage"

    def __init__(self, http: HttpClient) -> None:
        super().__init__(max_retries=1, timeout=20.0)
        self._http = http

    @property
    def configured(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "live"

    def fetch(self, request: ConnectorRequest) -> ConnectorResult:
        result = ConnectorResult()
        urls = list(request.urls)
        if not urls:
            result.notes.append("제공된 IR URL 이 없어 수집을 건너뛰었습니다.")
            return result
        for url in urls[: request.max_items]:
            try:
                source = self._fetch_one(url)
            except ConnectorFailure as exc:
                result.errors.append(
                    ConnectorError(
                        provider=self.provider,
                        code=exc.code,
                        message=exc.message,
                        retryable=exc.retryable,
                        detail=exc.detail,
                    )
                )
                continue
            if source is not None:
                result.sources.append(source)
        return result

    def _fetch_one(self, url: str) -> Optional[SourceDoc]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConnectorFailure(
                ERR_INVALID_REQUEST,
                "IR URL 은 https 로 시작해야 합니다.",
                provider=self.provider.value,
                detail=url,
            )
        status, raw = self._http.get_text(
            url,
            provider=self.provider.value,
            endpoint="ir_page",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        meta = _extract_meta(raw)
        title = meta.get("title") or _extract_title(raw) or f"IR 자료 {parsed.netloc}"
        text = html_to_text(raw)
        sentences = split_sentences(text)
        excerpt = ""
        for sentence in sentences:
            if len(sentence) >= 40:
                excerpt = sentence[:300]
                break
        if not excerpt:
            excerpt = normalize_whitespace(text)[:300]

        published = _extract_publish_date(meta, text)
        period = extract_period(text)

        source_id = f"ir:{parsed.netloc}:{abs(hash(url)) % 10**8}"
        metrics = extract_metrics_from_text(text, source_id=source_id, limit=20)
        if metrics and not period:
            period = metrics[0].period

        return SourceDoc(
            source_id=source_id,
            provider=self.provider,
            title=title[:300],
            url=url,
            provenance=Provenance.LIVE,
            retrieved_at=_now(),
            published_at=published,
            period_label=period,
            unit=metrics[0].unit if metrics else None,
            excerpt=excerpt,
            raw_text=text,
            metrics=metrics,
            publisher=parsed.netloc,
            doc_id=url,
            extra={
                "http_status": status,
                "meta": {k: v for k, v in meta.items() if k in ("title", "site_name")},
            },
        )


def _extract_meta(raw_html: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(raw_html, "html.parser")
        if soup.title and soup.title.string:
            meta["title"] = soup.title.string.strip()
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            meta["title"] = og["content"].strip()
        site = soup.find("meta", property="og:site_name")
        if site and site.get("content"):
            meta["site_name"] = site["content"].strip()
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
            if not key:
                continue
            content = (tag.get("content") or "").strip()
            if not content:
                continue
            if key in _DATE_META_KEYS:
                meta.setdefault("published", content)
            elif key in ("title", "og:title"):
                meta.setdefault("title", content)
            elif key == "og:site_name":
                meta.setdefault("site_name", content)
    except Exception:  # noqa: BLE001
        pass
    return meta


def _extract_title(raw_html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip() or None
    return None


def _extract_publish_date(meta: Dict[str, str], text: str) -> Optional[date]:
    """발행일을 추출한다(신뢰 가능한 메타 태그 → 키워드 근처 본문 순)."""
    raw = meta.get("published")
    if raw:
        parsed = extract_published_at(raw)
        if parsed:
            return parsed

    # 본문에서는 '게시일/등록일' 같은 키워드 근처만 본다(푸터 날짜 오인 방지).
    head = text[:8000]
    for keyword in _DATE_KEYWORDS:
        for match in re.finditer(re.escape(keyword), head):
            window = head[match.start() : match.start() + 120]
            parsed = extract_published_at(window)
            if parsed:
                return parsed
    return None


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
