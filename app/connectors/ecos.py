"""한국은행 ECOS 커넥터.

확인된 사실(2026-10-21 실측):
  * Base URL : https://ecos.bok.or.kr/api
  * 인증키는 URL 경로 세그먼트로 전달. 공식 샘플 키 'sample' 이 동작하며 실제 데이터를 준다.
  * 통계 조회 URL 형식:
      /api/StatisticSearch/{key}/json/{lang}/{startRow}/{endRow}/{stat_code}/{freq}/{start_dt}/{end_dt}/{item_code}/
  * 에러는 {"RESULT":{"CODE":"ERROR-100|ERROR-101", "MESSAGE":...}} 형태.

아래 통계 코드/항목 코드는 StatisticItemList 응답으로 실제 존재를 확인한 값이다.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import EcosSettings
from ..errors import ConnectorFailure, ERR_INVALID_REQUEST, ERR_NOT_FOUND, map_ecos_code
from ..models import ConnectorError, Metric, ProviderName, Provenance, SourceDoc
from ..observability import get_logger
from .base import Connector, ConnectorRequest, ConnectorResult
from .http_client import HttpClient

log = get_logger(__name__)

#: 검증된 ECOS 지표 레지스트리
#: stat_code / item_code / cycle 은 StatisticItemList 로 실제 존재를 확인했다.
ECOS_INDICATORS: List[Dict[str, Any]] = [
    {
        "key": "base_rate",
        "stat_code": "722Y001",
        "item_code": "0101000",
        "cycle": "M",
        "label": "한국은행 기준금리",
        "label_en": "Bank of Korea Base Rate",
        "keywords": ["금리", "정책금리", "기준금리", "interest", "rate"],
    },
    {
        "key": "usd_krw",
        "stat_code": "731Y003",
        "item_code": "0000002",
        "cycle": "D",
        "label": "원/달러 환율(시가)",
        "label_en": "Won per United States Dollar (Open)",
        "keywords": ["환율", "달러", "usd", "exchange", "won"],
    },
    {
        "key": "usd_krw_close",
        "stat_code": "731Y003",
        "item_code": "0000003",
        "cycle": "D",
        "label": "원/달러 환율(15:30 종가)",
        "label_en": "Won per United States Dollar (Close 15:30)",
        "keywords": ["환율", "달러", "usd", "exchange"],
    },
    {
        "key": "jpy_krw",
        "stat_code": "731Y001",
        "item_code": "0000002",
        "cycle": "D",
        "label": "원/엔 환율(100엔)",
        "label_en": "Won per Japanese Yen (100Yen)",
        "keywords": ["엔", "yen", "jpy"],
    },
    {
        "key": "eur_krw",
        "stat_code": "731Y001",
        "item_code": "0000003",
        "cycle": "D",
        "label": "원/유로 환율",
        "label_en": "Won per Euro",
        "keywords": ["유로", "eur"],
    },
    {
        "key": "consumer_tendency",
        "stat_code": "511Y002",
        "item_code": "FMAB",
        "item_code2": "99988",
        "cycle": "M",
        "label": "소비자동향지수(현재 경기판단, 전국)",
        "label_en": "Consumer Tendency Index (Current Economic Situation, National)",
        "keywords": ["소비", "자동향", "consumer", "sentiment"],
    },
    {
        "key": "consumer_spending_plan",
        "stat_code": "511Y002",
        "item_code": "FMCB",
        "item_code2": "99988",
        "cycle": "M",
        "label": "소비자동향지수(소비지출계획, 전국)",
        "label_en": "Consumer Tendency Index (Spending Plan, National)",
        "keywords": ["소비", "지출", "consumer", "spending"],
    },
]

#: 주기별 날짜 표기 형식
_CYCLE_DATE_FORMAT = {
    "D": "%Y%m%d",
    "M": "%Y%m",
    "Q": "%Y%m",
    "A": "%Y",
}

#: 주기별 조회 단위(날짜 인덱스 계산용)
_CYCLE_UNIT = {"D": "day", "M": "month", "Q": "quarter", "A": "year"}

#: 샘플 키는 1회 호출당 최대 10건만 반환된다(ECOS ERROR-301 기준).
ROW_CAP_SAMPLE = 10
#: 실제 인증키 사용 시 요청 건수 상한
ROW_CAP_LIVE = 200

#: 지표별 목표 관측치 수와 최대 호출 횟수
_TARGET_OBSERVATIONS = 12
_MAX_CALLS_PER_INDICATOR = 4


class EcosConnector(Connector):
    """ECOS(한국은행 경제통계시스템) 커넥터."""

    label = "한국은행 ECOS"
    provider = ProviderName.ECOS
    kind = "macro_statistics"

    def __init__(
        self,
        settings: EcosSettings,
        http: HttpClient,
        *,
        indicators: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(max_retries=2, timeout=settings.timeout_seconds)
        self._settings = settings
        self._http = http
        self._indicators = list(indicators if indicators is not None else ECOS_INDICATORS)

    @property
    def configured(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "live" if self._settings.configured else "sample"

    # --- URL 생성 -------------------------------------------------------
    def _statistic_url(
        self,
        stat_code: str,
        cycle: str,
        start: str,
        end: str,
        item_code: str,
        item_code2: Optional[str] = None,
        rows: Optional[int] = None,
    ) -> str:
        base = self._settings.base_url.rstrip("/")
        key = self._settings.effective_key
        limit = rows if rows is not None else self._row_cap
        item_path = item_code if not item_code2 else f"{item_code}/{item_code2}"
        return (
            f"{base}/StatisticSearch/{key}/json/kr/1/{limit}/"
            f"{stat_code}/{cycle}/{start}/{end}/{item_path}/"
        )

    @property
    def _row_cap(self) -> int:
        """1회 호출 가능한 최대 조회 건수(샘플 키는 10건)."""
        return ROW_CAP_SAMPLE if self._settings.using_sample_key else ROW_CAP_LIVE

    @staticmethod
    def _format_date(value: date, cycle: str) -> str:
        """주기에 맞는 날짜 표기로 변환한다(ECOS는 주기별 형식을 요구한다)."""
        fmt = _CYCLE_DATE_FORMAT.get(cycle, "%Y%m%d")
        if cycle == "Q":
            return f"{value.year}Q{((value.month - 1) // 3) + 1}"
        return value.strftime(fmt)

    @staticmethod
    def _to_index(value: date, cycle: str) -> int:
        """날짜를 주기 단위 정수 인덱스로 변환한다."""
        unit = _CYCLE_UNIT.get(cycle, "day")
        if unit == "day":
            return (value - date(1970, 1, 1)).days
        if unit == "month":
            return value.year * 12 + (value.month - 1)
        if unit == "quarter":
            return value.year * 4 + ((value.month - 1) // 3)
        return value.year

    @staticmethod
    def _from_index(index: int, cycle: str) -> date:
        """주기 단위 인덱스를 날짜로 되돌린다."""
        unit = _CYCLE_UNIT.get(cycle, "day")
        if unit == "day":
            return date(1970, 1, 1) + timedelta(days=index)
        if unit == "month":
            return date(index // 12, index % 12 + 1, 1)
        if unit == "quarter":
            return date(index // 4, (index % 4) * 3 + 1, 1)
        return date(index, 1, 1)

    @staticmethod
    def _default_range(cycle: str) -> tuple[date, date]:
        today = date.today()
        if cycle == "D":
            return today - timedelta(days=365), today
        if cycle == "M":
            return today - timedelta(days=365 * 3), today
        return today - timedelta(days=365 * 6), today

    # --- 수집 -----------------------------------------------------------
    def fetch(self, request: ConnectorRequest) -> ConnectorResult:
        result = ConnectorResult()
        if not self._settings.configured:
            result.notes.append(
                "ECOS 인증키가 없어 공식 샘플 키('sample')로 수집합니다. "
                "샘플 키는 일부 통계만 제공할 수 있습니다."
            )

        selected = self._select_indicators(request)
        if not selected:
            result.notes.append("질문에 부합하는 ECOS 지표를 찾지 못해 수집을 건너뛰었습니다.")
            return result

        for indicator in selected:
            try:
                source = self._fetch_indicator(indicator, request)
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

        key_stat = self._fetch_key_statistics(request)
        if key_stat is not None:
            result.sources.append(key_stat)
        return result

    def _select_indicators(self, request: ConnectorRequest) -> List[Dict[str, Any]]:
        haystack = " ".join(
            [request.query or "", request.company or "", " ".join(request.keywords or [])]
        ).lower()
        if not haystack.strip():
            return self._indicators[:3]
        scored = []
        for ind in self._indicators:
            score = sum(1 for kw in ind["keywords"] if kw.lower() in haystack)
            if score:
                scored.append((score, ind))
        if not scored:
            # 결정론적 폴백: 금리/환율/소비 중 일부를 기본 포함
            defaults = [i for i in self._indicators if i["key"] in ("base_rate", "usd_krw_close")]
            return defaults
        scored.sort(key=lambda x: (-x[0], x[1]["key"]))
        return [ind for _, ind in scored[:3]]

    def _fetch_indicator(
        self, indicator: Dict[str, Any], request: ConnectorRequest
    ) -> Optional[SourceDoc]:
        cycle = indicator["cycle"]
        start_d, end_d = self._resolve_range(request, cycle)

        rows_by_time: Dict[str, Dict[str, Any]] = {}
        url_by_time: Dict[str, str] = {}
        calls = 0
        url = ""
        end_index = self._to_index(end_d, cycle)
        start_index = self._to_index(start_d, cycle)
        cap = self._row_cap
        last_error: Optional[ConnectorFailure] = None

        # 최신 데이터가 뒤에 오도록 '끝' 쪽 구간부터 역순으로 조회한다.
        # (샘플 키는 1회 10건 제한이 있어 구간을 나눠서 호출한다.)
        cursor = end_index
        while cursor >= start_index and calls < _MAX_CALLS_PER_INDICATOR:
            window_start_index = max(start_index, cursor - cap + 1)
            chunk_start = self._format_date(self._from_index(window_start_index, cycle), cycle)
            chunk_end = self._format_date(self._from_index(cursor, cycle), cycle)
            url = self._statistic_url(
                indicator["stat_code"],
                cycle,
                chunk_start,
                chunk_end,
                indicator["item_code"],
                indicator.get("item_code2"),
            )
            try:
                _, payload = self._http.get_json(
                    url, provider=self.provider.value, endpoint="StatisticSearch"
                )
            except ConnectorFailure as exc:
                last_error = exc
                break
            calls += 1
            if isinstance(payload, dict) and "RESULT" in payload:
                code, message = map_ecos_code(payload["RESULT"].get("CODE", ""))
                raise ConnectorFailure(
                    code,
                    message,
                    provider=self.provider.value,
                    detail=payload["RESULT"].get("CODE"),
                )
            block = payload.get("StatisticSearch") if isinstance(payload, dict) else None
            if not block:
                break
            for row in block.get("row") or []:
                time_label = row.get("TIME") or ""
                if not time_label:
                    continue
                # 같은 시점이 여러 세부항목(성별/연령 등)으로 중복되면 '전체'(99988)를 우선한다.
                existing = rows_by_time.get(time_label)
                if existing is None or _is_preferred_row(row, existing):
                    rows_by_time[time_label] = row
                    url_by_time[time_label] = url
            if len(rows_by_time) >= _TARGET_OBSERVATIONS:
                break
            if window_start_index <= start_index:
                break
            cursor = window_start_index - 1

        if not rows_by_time:
            if last_error is not None:
                raise last_error
            raise ConnectorFailure(
                ERR_NOT_FOUND,
                f"{indicator['label']} 에 해당하는 시계열 데이터가 없습니다.",
                provider=self.provider.value,
                detail=indicator["key"],
            )

        metrics: List[Metric] = []
        raw_lines: List[str] = []
        for time_label in sorted(rows_by_time):
            row = rows_by_time[time_label]
            value = row.get("DATA_VALUE")
            if value in (None, "", "-"):
                continue
            try:
                num = float(str(value).replace(",", ""))
            except ValueError:
                continue
            unit = (row.get("UNIT_NAME") or "").strip() or None
            label = (row.get("ITEM_NAME1") or indicator["label"]).strip()
            metrics.append(
                Metric(
                    label=label,
                    value=num,
                    raw_value=str(value),
                    unit=unit,
                    period=self._period_from_time(time_label, cycle),
                    context=f"{label} {time_label} {value}{' ' + unit if unit else ''}".strip(),
                )
            )
            raw_lines.append(
                f"{label} | {time_label} | {value}{' ' + unit if unit else ''}".strip()
            )

        if not metrics:
            raise ConnectorFailure(
                ERR_NOT_FOUND,
                f"{indicator['label']} 데이터가 모두 비어 있습니다.",
                provider=self.provider.value,
                detail=indicator["key"],
            )

        latest_label = metrics[-1].period or ""
        latest_value = metrics[-1].value
        latest_unit = metrics[-1].unit
        latest_time = sorted(rows_by_time)[-1] if rows_by_time else ""
        # 출처 링크는 '최신 값을 담은 구간'을 가리키도록 한다.
        latest_url = url_by_time.get(latest_time, url)
        title = f"[ECOS] {indicator['label']} ({latest_label or '기간 미상'})"
        excerpt = (
            f"{indicator['label']} 최신값 {latest_value}"
            f"{' ' + latest_unit if latest_unit else ''} ({latest_label})"
        )
        return SourceDoc(
            source_id=f"ecos:{indicator['key']}",
            provider=self.provider,
            title=title,
            url=latest_url.replace(self._settings.effective_key, "<API_KEY>"),
            provenance=Provenance.LIVE,
            retrieved_at=_now(),
            published_at=self._published_from(latest_label),
            period_label=latest_label,
            unit=latest_unit,
            excerpt=excerpt,
            raw_text="\n".join(raw_lines),
            metrics=metrics,
            publisher="한국은행 경제통계시스템(ECOS)",
            doc_id=indicator["stat_code"],
            extra={
                "stat_code": indicator["stat_code"],
                "item_code": indicator["item_code"],
                "cycle": cycle,
                "indicator_key": indicator["key"],
                "observation_count": len(metrics),
                "api_calls": calls,
                "range": {
                    "start": self._format_date(start_d, cycle),
                    "end": self._format_date(end_d, cycle),
                },
                "api_key_mode": "live" if self._settings.configured else "sample",
            },
        )

    def _resolve_range(self, request: ConnectorRequest, cycle: str) -> tuple[date, date]:
        """요청 기간(또는 기본 기간)을 주기 단위로 해석한다."""
        if request.start_date and request.end_date:
            start_d = self._parse_date(request.start_date, cycle)
            end_d = self._parse_date(request.end_date, cycle)
            if end_d < start_d:
                start_d, end_d = end_d, start_d
            return start_d, end_d
        return self._default_range(cycle)

    def _fetch_key_statistics(self, request: ConnectorRequest) -> Optional[SourceDoc]:
        """ECOS '100대 통계지표' 스냅샷(실시간 거시 지표)."""
        base = self._settings.base_url.rstrip("/")
        key = self._settings.effective_key
        url = f"{base}/KeyStatisticList/{key}/json/kr/1/{self._row_cap}/"
        try:
            _, payload = self._http.get_json(
                url, provider=self.provider.value, endpoint="KeyStatisticList"
            )
        except ConnectorFailure:
            return None
        if not isinstance(payload, dict):
            return None
        block = payload.get("KeyStatisticList")
        if not block:
            return None
        rows = block.get("row") or []
        if not rows:
            return None

        metrics: List[Metric] = []
        lines: List[str] = []
        for row in rows:
            value = row.get("DATA_VALUE")
            name = (row.get("KEYSTAT_NAME") or "").strip()
            if not name or value in (None, ""):
                continue
            try:
                num = float(str(value).replace(",", ""))
            except ValueError:
                continue
            unit = (row.get("UNIT_NAME") or "").strip() or None
            cycle = (row.get("CYCLE") or "").strip()
            metrics.append(
                Metric(
                    label=name,
                    value=num,
                    raw_value=str(value),
                    unit=unit,
                    period=self._period_from_time(cycle, "D" if len(cycle) == 8 else "M"),
                    context=f"{name} | {cycle} | {value} {unit or ''}".strip(),
                )
            )
            lines.append(f"{name} | {cycle} | {value} {unit or ''}".strip())
        if not metrics:
            return None
        return SourceDoc(
            source_id="ecos:key_statistics",
            provider=self.provider,
            title="[ECOS] 100대 주요 통계지표 스냅샷",
            url=url.replace(self._settings.effective_key, "<API_KEY>"),
            provenance=Provenance.LIVE,
            retrieved_at=_now(),
            published_at=self._published_from(metrics[0].period),
            period_label=metrics[0].period,
            unit=metrics[0].unit,
            excerpt=f"{metrics[0].label} {metrics[0].value} {metrics[0].unit or ''} 외 {len(metrics)}개 지표".strip(),
            raw_text="\n".join(lines),
            metrics=metrics,
            publisher="한국은행 경제통계시스템(ECOS)",
            doc_id="KeyStatisticList",
            extra={
                "observation_count": len(metrics),
                "api_key_mode": "live" if self._settings.configured else "sample",
            },
        )

    # --- 보조 ------------------------------------------------------------
    @staticmethod
    def _parse_date(raw: str, cycle: str) -> date:
        raw = (raw or "").strip().replace("-", "").replace(".", "")
        try:
            if cycle == "A" and len(raw) == 4:
                return date(int(raw), 12, 31)
            if cycle == "M" and len(raw) == 6:
                return date(int(raw[:4]), int(raw[4:6]), 1)
            if len(raw) == 8:
                return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            pass
        return date.today() - timedelta(days=365)

    @staticmethod
    def _period_from_time(time_label: str, cycle: str) -> Optional[str]:
        raw = (time_label or "").strip()
        if not raw:
            return None
        if cycle == "A" and len(raw) == 4:
            return raw
        if cycle == "M" and len(raw) == 6:
            return f"{raw[:4]}-{raw[4:6]}"
        if cycle == "Q" and len(raw) >= 5 and "Q" in raw:
            return raw
        if len(raw) == 8:
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        if len(raw) == 6:
            return f"{raw[:4]}-{raw[4:6]}"
        return raw

    @staticmethod
    def _published_from(period: Optional[str]) -> Optional[date]:
        if not period:
            return None
        raw = period.replace("-", "")
        try:
            if len(raw) == 8:
                return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            if len(raw) == 6:
                return date(int(raw[:4]), int(raw[4:6]), 1)
            if len(raw) == 4:
                return date(int(raw), 12, 31)
        except ValueError:
            return None
        return None


def _is_preferred_row(candidate: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    """같은 시점의 여러 세부항목 중 더 대표적인 행을 고른다.

    ECOS 의 2차 항목코드 중 '99988'(전체)를 가장 선호하고,
    세부항목이 없는 행을 그 다음으로 선호한다.
    """

    def rank(row: Dict[str, Any]) -> Tuple[int, int]:
        code2 = row.get("ITEM_CODE2")
        if code2 is None:
            return (2, 0)
        if str(code2) == "99988":
            return (3, 0)
        return (1, 0)

    return rank(candidate) > rank(existing)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
