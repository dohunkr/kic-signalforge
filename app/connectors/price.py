"""주가 커넥터 - 상장사 종가를 가져온다.

심볼(티커) 해석 경로
  기업명 → DART 기업코드(stock_code) → Yahoo Finance 검색 → 실제 종목 심볼
  (Yahoo 는 같은 6자리 숫자에 .KS/.KQ 로 서로 다른 종목이 매핑될 수 있으므로
   반드시 검색 API 로 equities 를 찾아 그 심볼을 사용한다)

시세는 공개 시세 API(Yahoo Finance chart endpoint)를 사용한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from ..config import PriceSettings
from ..errors import ConnectorFailure, ERR_NOT_FOUND
from ..models import Metric, PricePoint, ProviderName, Provenance, SourceDoc
from ..observability import get_logger
from .base import Connector, ConnectorRequest, ConnectorResult
from .http_client import HttpClient

log = get_logger(__name__)

#: 한국 주식이 거래되는 야판 교환 코드
KR_EQUITY_EXCHANGES = {"KSC", "KOE", "KSC2", "KOE2"}


class PriceConnector(Connector):
    """상장사 종가 시계열 수집기."""

    provider = ProviderName.PRICE
    kind = "price_series"
    label = "공시 시세"

    def __init__(
        self,
        settings: PriceSettings,
        http: HttpClient,
        *,
        symbol_resolver=None,
    ) -> None:
        super().__init__(max_retries=2, timeout=settings.timeout_seconds)
        self._settings = settings
        self._http = http
        self._symbol_resolver = symbol_resolver
        self._symbol_cache: Dict[str, Optional[str]] = {}

    @property
    def configured(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "live"

    # --- 심볼 해석 -------------------------------------------------------
    def resolve_symbol(self, stock_code: str) -> Optional[str]:
        """6자리 종목코드 → Yahoo 심볼(예: 005930.KS)."""
        stock_code = (stock_code or "").strip()
        if not stock_code:
            return None
        if stock_code in self._symbol_cache:
            return self._symbol_cache[stock_code]

        url = f"{self._settings.base_url.rstrip('/')}/v1/finance/search"
        try:
            _, payload = self._http.get_json(
                url,
                provider=self.provider.value,
                endpoint="finance/search",
                params={"q": stock_code, "quotesCount": 8, "newsCount": 0},
            )
        except ConnectorFailure:
            self._symbol_cache[stock_code] = None
            return None

        quotes = payload.get("quotes") if isinstance(payload, dict) else None
        symbol: Optional[str] = None
        if isinstance(quotes, list):
            for quote in quotes:
                if not isinstance(quote, dict):
                    continue
                if str(quote.get("quoteType", "")).upper() != "EQUITY":
                    continue
                if str(quote.get("exchange", "")).upper() not in KR_EQUITY_EXCHANGES:
                    continue
                candidate = quote.get("symbol")
                if not candidate:
                    continue
                name = str(quote.get("shortname") or quote.get("longname") or "")
                # 여러 심볼이 붙은 가짜 이름(예: '005930.KQ,0P0000B2XZ')은 제외한다.
                if "." in name or "," in name:
                    continue
                symbol = str(candidate)
                break
        self._symbol_cache[stock_code] = symbol
        return symbol

    # --- 시계열 ----------------------------------------------------------
    def fetch_series(
        self, symbol: str, *, range_key: str = "1y", interval: str = "1d"
    ) -> Tuple[List[PricePoint], Dict[str, Any]]:
        """종가 시계열과 메타를 반환한다."""
        url = f"{self._settings.base_url.rstrip('/')}/v8/finance/chart/{symbol}"
        _, payload = self._http.get_json(
            url,
            provider=self.provider.value,
            endpoint="finance/chart",
            params={"range": range_key, "interval": interval},
        )
        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            raise ConnectorFailure(
                ERR_NOT_FOUND,
                f"'{symbol}' 의 시세 데이터를 찾지 못했습니다.",
                provider=self.provider.value,
            )
        result = results[0]
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote_blocks = (result.get("indicators") or {}).get("quote") or [{}]
        closes = quote_blocks[0].get("close") or []
        volumes = quote_blocks[0].get("volume") or []

        points: List[PricePoint] = []
        for index, stamp in enumerate(timestamps):
            close = closes[index] if index < len(closes) else None
            if close is None:
                continue
            volume = volumes[index] if index < len(volumes) else None
            points.append(
                PricePoint(
                    date=datetime.fromtimestamp(stamp, tz=timezone.utc)
                    .strftime("%Y-%m-%d"),
                    close=round(float(close), 2),
                    volume=float(volume) if volume is not None else None,
                )
            )
        if not points:
            raise ConnectorFailure(
                ERR_NOT_FOUND,
                f"'{symbol}' 의 종가 시계열이 비어 있습니다.",
                provider=self.provider.value,
            )
        return points, meta

    # --- 커넥터 인터페이스 -------------------------------------------------
    def fetch(self, request: ConnectorRequest) -> ConnectorResult:
        """종목코드 또는 심볼로 시계열을 수집한다."""
        result = ConnectorResult()
        symbol = request.extra.get("symbol") if request.extra else None
        if not symbol and self._symbol_resolver is not None:
            stock_code = request.extra.get("stock_code") if request.extra else None
            if stock_code:
                symbol = self.resolve_symbol(str(stock_code))
        if not symbol:
            result.notes.append("종목 코드를 확인하지 못해 시세를 조회하지 않았습니다.")
            return result

        try:
            points, meta = self.fetch_series(symbol)
        except ConnectorFailure as exc:
            result.errors.append(
                ConnectorError(
                    provider=self.provider,
                    code=exc.code,
                    message=exc.message,
                    detail=exc.detail,
                )
            )
            return result

        result.sources.append(
            SourceDoc(
                source_id=f"price:{symbol}",
                provider=self.provider,
                title=f"[시세] {symbol} 종가 ({points[0].date}~{points[-1].date})",
                url=f"https://finance.yahoo.com/quote/{symbol}",
                provenance=Provenance.LIVE,
                retrieved_at=datetime.now(timezone.utc),
                published_at=datetime.strptime(points[-1].date, "%Y-%m-%d").date(),
                period_label=points[-1].date,
                unit=meta.get("currency", "KRW"),
                excerpt=(
                    f"종가 {points[-1].close:,.0f} {meta.get('currency', 'KRW')} "
                    f"({len(points)}거래일)"
                ),
                raw_text="\n".join(f"{p.date} | {p.close}" for p in points),
                metrics=[
                    Metric(
                        label=f"{symbol} 종가",
                        value=p.close,
                        raw_value=f"{p.close:g}",
                        unit=meta.get("currency", "KRW"),
                        period=p.date,
                        source_id=f"price:{symbol}",
                        context=f"{p.date} 종가 {p.close:,.0f}",
                    )
                    for p in points[-120:]
                ],
                publisher="Yahoo Finance",
                doc_id=symbol,
                extra={
                    "symbol": symbol,
                    "currency": meta.get("currency", "KRW"),
                    "exchange": meta.get("fullExchangeName"),
                    "current_price": meta.get("regularMarketPrice"),
                    "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                    "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
                    "points": len(points),
                },
            )
        )
        return result
