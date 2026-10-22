"""국가데이터처 KOSIS 커넥터.

확인된 사실(2026-10-21 실측):
  * Base URL : https://kosis.kr/openapi
  * 인증키는 쿼리 파라미터 apiKey 로 전달.
  * 키가 없으면 {"err":"10"}, 키가 틀리면 {"err":"11"} 를 반환(문자열 형태의 JSON).
  * 검색: /statisticsSearch.do?method=getList&apiKey=&format=json&jsonVD=Y&searchNm=&resultCount=&startCount=
  * 데이터: /Param/statisticsParameterData.do?method=getList&apiKey=&format=json&jsonVD=Y
            &orgId=&tblId=&objL1=ALL&itmId=ALL&prdSe=&startPrdDe=&endPrdDe=
  * 국가통계포털 공지(2026-03-05): HTTPS 전용, 인증키당 분당 1,000건, 1회 40,000셀 제한.

주의: 인증키가 없으면 데이터를 만들지 않고 실패로 보고한다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import KosisSettings
from ..errors import ConnectorFailure, ERR_AUTH, ERR_NOT_FOUND, ERR_QUOTA, map_kosis_err
from ..models import ConnectorError, Metric, ProviderName, Provenance, SourceDoc
from ..observability import get_logger
from .base import Connector, ConnectorRequest, ConnectorResult
from .http_client import HttpClient

log = get_logger(__name__)

#: 수록주기 후보 (통계검색의 PRD_SE 와 조회 파라미터 prdSe 는 코드 체계가 다를 수 있다)
_PERIOD_CANDIDATES = ["Y", "M", "Q", "A", "F", "IR"]

#: 통계표 1건당 시도할 최대 HTTP 호출 수(쿼터 보호)
_MAX_CALLS_PER_TABLE = 12
#: 시도할 최대 수록주기 후보 수
_MAX_PERIOD_CANDIDATES = 3


class KosisConnector(Connector):
    """KOSIS(국가통계포털) 커넥터."""

    label = "국가데이터처 KOSIS"
    provider = ProviderName.KOSIS
    kind = "national_statistics"

    def __init__(self, settings: KosisSettings, http: HttpClient) -> None:
        super().__init__(max_retries=2, timeout=settings.timeout_seconds)
        self._settings = settings
        self._http = http

    @property
    def configured(self) -> bool:
        return self._settings.configured

    @property
    def mode(self) -> str:
        return "live" if self._settings.configured else "unavailable"

    # --- 수집 -----------------------------------------------------------
    def fetch(self, request: ConnectorRequest) -> ConnectorResult:
        result = ConnectorResult()
        if not self._settings.configured:
            result.errors.append(
                ConnectorError(
                    provider=self.provider,
                    code=ERR_AUTH,
                    message=(
                        "KOSIS 인증키(KOSIS_API_KEY)가 설정되지 않아 통계를 수집하지 못했습니다."
                    ),
                    detail="키 발급: https://kosis.kr/openapi/ -> 회원가입 -> 활용신청",
                )
            )
            return result

        keywords = self._pick_keywords(request)
        if not keywords:
            result.notes.append("KOSIS 검색어를 만들지 못해 수집을 건너뛰었습니다.")
            return result

        # 검색어 → 통계표 순으로 시도하고, 하나라도 데이터가 나오면 채택한다.
        for keyword in keywords:
            try:
                tables = self._search_tables(keyword, request)
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
            if not tables:
                continue
            for table in tables[:2]:
                try:
                    source = self._fetch_table_data(table, request)
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
                    source.extra["search_keyword"] = keyword
                    result.sources.append(source)
            if result.sources:
                return result

        if not result.sources and not result.errors:
            result.errors.append(
                ConnectorError(
                    provider=self.provider,
                    code=ERR_NOT_FOUND,
                    message=(
                        f"KOSIS에서 {'/'.join(keywords)} 에 해당하는 통계를 찾지 못했습니다."
                    ),
                    detail=f"searchNm={','.join(keywords)}",
                )
            )
        return result

    # --- 검색 -----------------------------------------------------------
    def _search_tables(self, keyword: str, request: ConnectorRequest) -> List[Dict[str, Any]]:
        base = self._settings.base_url.rstrip("/")
        url = f"{base}/statisticsSearch.do"
        _, payload = self._http.get_json(
            url,
            provider=self.provider.value,
            endpoint="statisticsSearch.do",
            params={
                "method": "getList",
                "apiKey": self._settings.api_key,
                "format": "json",
                "jsonVD": "Y",
                "searchNm": keyword,
                "resultCount": max(5, min(request.max_items, 20)),
                "startCount": 1,
            },
        )
        data = _coerce_json(payload)
        err = str(data.get("err") or "")
        if err and err != "0":
            code, message = map_kosis_err(err)
            raise ConnectorFailure(
                code, message, provider=self.provider.value, detail=f"KOSIS err={err}"
            )
        rows = data.get("result") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            tbl_id = row.get("TBL_ID")
            org_id = row.get("ORG_ID")
            if not tbl_id or not org_id:
                continue
            out.append(
                {
                    "org_id": str(org_id),
                    "tbl_id": str(tbl_id),
                    "tbl_nm": row.get("TBL_NM") or row.get("MT_ATITLE") or keyword,
                    "org_nm": row.get("ORG_NM") or "",
                    "prd_se": (row.get("PRD_SE") or "").strip(),
                    "start_prd_de": row.get("STRT_PRD_DE"),
                    "end_prd_de": row.get("END_PRD_DE"),
                    "link_url": row.get("LINK_URL"),
                }
            )
        return out

    # --- 데이터 조회 -----------------------------------------------------
    def _fetch_table_data(
        self, table: Dict[str, Any], request: ConnectorRequest
    ) -> Optional[SourceDoc]:
        base = self._settings.base_url.rstrip("/")
        url = f"{base}/Param/statisticsParameterData.do"

        # 검색 응답에는 PRD_SE 가 없으므로 후보를 순서대로 시도한다.
        candidates = list(_PERIOD_CANDIDATES)
        if table.get("prd_se"):
            candidates = [table["prd_se"]] + [c for c in candidates if c != table["prd_se"]]
        candidates = candidates[:_MAX_PERIOD_CANDIDATES]

        items = self._fetch_items(table)
        obj_codes = self._fetch_obj_codes(table)
        last_error: Optional[ConnectorFailure] = None
        calls = 0

        for prd_se in candidates:
            start_de, end_de = self._period_range(request, table, prd_se)
            for obj_l1, itm_id in self._param_combinations(table, items, obj_codes):
                if calls >= _MAX_CALLS_PER_TABLE:
                    break
                calls += 1
                try:
                    _, payload = self._http.get_json(
                        url,
                        provider=self.provider.value,
                        endpoint="statisticsParameterData.do",
                        params=self._build_params(
                            table, prd_se, start_de, end_de, obj_l1, itm_id
                        ),
                    )
                except ConnectorFailure as exc:
                    last_error = exc
                    if exc.code == ERR_QUOTA:
                        raise
                    break  # 이 조합은 실패 -> 다른 조합 시도

                data = _coerce_json(payload)
                err = str(data.get("err") or "")
                if err and err not in ("0", ""):
                    code, message = map_kosis_err(err)
                    last_error = ConnectorFailure(
                        code,
                        message,
                        provider=self.provider.value,
                        detail=f"KOSIS err={err} (prdSe={prd_se}, objL1={obj_l1}, itmId={itm_id})",
                    )
                    if code == ERR_QUOTA:
                        raise last_error
                    continue  # 같은 prdSe 에서 다른 조합 시도

                rows = data.get("result") or []
                if not rows:
                    last_error = ConnectorFailure(
                        ERR_NOT_FOUND,
                        f"'{table['tbl_nm']}' 에서 데이터를 찾지 못했습니다(prdSe={prd_se}).",
                        provider=self.provider.value,
                        detail=f"tblId={table['tbl_id']}",
                    )
                    continue

                source = self._build_source(table, rows, url, prd_se, start_de, end_de)
                if source is not None:
                    return source

        if last_error is not None:
            raise last_error
        return None

    def _fetch_items(self, table: Dict[str, Any]) -> List[Dict[str, Any]]:
        """통계표의 항목(ITM) 목록을 가져온다(최대 1회 호출)."""
        base = self._settings.base_url.rstrip("/")
        try:
            _, payload = self._http.get_json(
                f"{base}/statisticsData.do",
                provider=self.provider.value,
                endpoint="statisticsData.do",
                params={
                    "method": "getMeta",
                    "type": "ITM",
                    "apiKey": self._settings.api_key,
                    "format": "json",
                    "jsonVD": "Y",
                    "orgId": table["org_id"],
                    "tblId": table["tbl_id"],
                },
            )
        except ConnectorFailure:
            return []
        data = _coerce_json(payload)
        rows = data.get("result") or []
        return [r for r in rows if isinstance(r, dict) and r.get("ITM_ID")]

    def _fetch_obj_codes(self, table: Dict[str, Any]) -> List[str]:
        """통계표의 분류(OBJ) 코드 목록(최대 1회 호출)."""
        base = self._settings.base_url.rstrip("/")
        try:
            _, payload = self._http.get_json(
                f"{base}/statisticsData.do",
                provider=self.provider.value,
                endpoint="statisticsData.do",
                params={
                    "method": "getMeta",
                    "type": "OBJ",
                    "apiKey": self._settings.api_key,
                    "format": "json",
                    "jsonVD": "Y",
                    "orgId": table["org_id"],
                    "tblId": table["tbl_id"],
                },
            )
        except ConnectorFailure:
            return []
        data = _coerce_json(payload)
        rows = data.get("result") or []
        codes: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("OBJ_ID", "C1", "OBJ_CODE"):
                value = row.get(key)
                if value:
                    codes.append(str(value))
                    break
        return codes

    def _param_combinations(
        self, table: Dict[str, Any], items: List[Dict[str, Any]], obj_codes: List[str]
    ) -> List[Tuple[Optional[str], Optional[str]]]:
        """조회 파라미터 조합 후보.

        objL1='ALL' 이나 itmId='ALL' 을 허용하지 않는 통계표가 있어
        여러 조합을 순서대로 시도한다(호출 횟수 제한 있음).
        """
        combos: List[Tuple[Optional[str], Optional[str]]] = [("ALL", "ALL"), (None, "ALL")]
        for code in obj_codes[:2]:
            combos.append((code, "ALL"))
        if items:
            first = str(items[0].get("ITM_ID"))
            combos.append(("ALL", first))
            combos.append((None, first))
            for code in obj_codes[:1]:
                combos.append((code, first))
        seen = set()
        unique: List[Tuple[Optional[str], Optional[str]]] = []
        for combo in combos:
            if combo in seen:
                continue
            seen.add(combo)
            unique.append(combo)
        return unique[:4]

    def _build_params(
        self,
        table: Dict[str, Any],
        prd_se: str,
        start_de: str,
        end_de: str,
        obj_l1: Optional[str],
        itm_id: Optional[str],
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "method": "getList",
            "apiKey": self._settings.api_key,
            "format": "json",
            "jsonVD": "Y",
            "orgId": table["org_id"],
            "tblId": table["tbl_id"],
            "prdSe": prd_se,
            "startPrdDe": start_de,
            "endPrdDe": end_de,
        }
        if obj_l1 is not None:
            params["objL1"] = obj_l1
        if itm_id is not None:
            params["itmId"] = itm_id
        return params

    def _build_source(
        self,
        table: Dict[str, Any],
        rows: List[Any],
        url: str,
        prd_se: str,
        start_de: str,
        end_de: str,
    ) -> Optional[SourceDoc]:
        """KOSIS 행 -> SourceDoc.

        실제 응답 필드(실측 기준):
          값 DT / 시점 PRD_DE / 단위 UNIT_NM / 항목 ITM_NM / 분류 C1_NM
          최종갱신일 LST_CHN_DE
        objL1=ALL 로 조회하면 분류(C1)별로 같은 시점이 여러 행으로 오므로,
        분류 코드 순으로 가장 앞선 행(보통 전체/집계)을 대표로 삼는다.
        """
        metrics: List[Metric] = []
        lines: List[str] = []
        seen_periods: Dict[str, Dict[str, Any]] = {}
        updated_at: Optional[str] = None

        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get("DT")
            if value in (None, ""):
                continue
            try:
                num = float(str(value).replace(",", ""))
            except ValueError:
                continue
            period_key = str(row.get("PRD_DE") or "")
            classifier = str(row.get("C1") or "")
            existing = seen_periods.get(period_key)
            if existing is None or classifier < str(existing.get("C1") or ""):
                seen_periods[period_key] = row
            changed = row.get("LST_CHN_DE")
            if isinstance(changed, str) and changed.strip():
                updated_at = changed.strip()

        for period_key in sorted(seen_periods):
            row = seen_periods[period_key]
            value = row.get("DT")
            try:
                num = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                continue
            item_name = (row.get("ITM_NM") or table["tbl_nm"]).strip()
            class_name = (row.get("C1_NM") or "").strip()
            label = f"{item_name} ({class_name})" if class_name else item_name
            unit = (row.get("UNIT_NM") or "").strip() or None
            period = self._period_from_prd_de(period_key, prd_se)
            metrics.append(
                Metric(
                    label=label,
                    value=num,
                    raw_value=str(value),
                    unit=unit,
                    period=period,
                    context=f"{label} | {period_key} | {value}{(' ' + unit) if unit else ''}",
                )
            )
            lines.append(
                f"{label} | {period_key} | {value}{(' ' + unit) if unit else ''}".strip()
            )

        if not metrics:
            return None

        link = table.get("link_url") or url
        published = None
        if updated_at:
            try:
                published = datetime.strptime(updated_at[:10], "%Y-%m-%d").date()
            except ValueError:
                published = None

        return SourceDoc(
            source_id=f"kosis:{table['org_id']}:{table['tbl_id']}:{prd_se}",
            provider=self.provider,
            title=f"[KOSIS] {table['tbl_nm']}",
            url=str(link),
            provenance=Provenance.LIVE,
            retrieved_at=_now(),
            published_at=published,
            period_label=metrics[-1].period,
            unit=metrics[-1].unit,
            excerpt=f"{metrics[-1].label} {metrics[-1].value} 외 {len(metrics)}개 관측치".strip(),
            raw_text="\n".join(lines),
            metrics=metrics,
            publisher=table.get("org_nm") or "국가데이터처 KOSIS",
            doc_id=f"{table['org_id']}/{table['tbl_id']}",
            extra={
                "org_id": table["org_id"],
                "tbl_id": table["tbl_id"],
                "prd_se": prd_se,
                "range": {"start": start_de, "end": end_de},
                "observation_count": len(metrics),
                "last_updated": updated_at,
            },
        )

    @staticmethod
    def _period_from_prd_de(raw: str, prd_se: str) -> Optional[str]:
        raw = (raw or "").strip()
        if not raw:
            return None
        if len(raw) == 4:
            return raw
        if len(raw) == 6:
            year, month = raw[:4], int(raw[4:6])
            if prd_se == "Q":
                return f"{year}Q{((month - 1) // 3) + 1}"
            return f"{year}-{month:02d}"
        return raw

    # --- 보조 ------------------------------------------------------------
    @staticmethod
    def _pick_keywords(request: ConnectorRequest) -> List[str]:
        """검색어 후보 목록(앞에서부터 시도)."""
        candidates: List[str] = []
        for kw in request.keywords:
            kw = (kw or "").strip()
            if kw and kw not in candidates:
                candidates.append(kw)
        for candidate in (request.company, request.topic, request.query):
            candidate = (candidate or "").strip()[:60]
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates[:3]

    @staticmethod
    def _period_range(
        request: ConnectorRequest, table: Dict[str, Any], prd_se: str
    ) -> tuple[str, str]:
        """시작/종료 시점을 prdSe 에 맞는 표기로 변환한다."""
        today = date.today()
        raw_start = request.start_date or ""
        raw_end = request.end_date or ""
        if raw_start and raw_end:
            start = raw_start.replace("-", "").replace(".", "")[:8]
            end = raw_end.replace("-", "").replace(".", "")[:8]
            if len(start) == 8 and len(end) == 8:
                return _to_prd_format(start, prd_se), _to_prd_format(end, prd_se)
        start = str(table.get("start_prd_de") or "").strip()
        end = str(table.get("end_prd_de") or "").strip()
        if start and end:
            return _to_prd_format(start, prd_se), _to_prd_format(end, prd_se)
        return str(today.year - 5), str(today.year)


def _to_prd_format(raw: str, prd_se: str) -> str:
    """YYYYMMDD / YYYYMM / YYYY 를 prdSe 에 맞는 표기로 변환한다."""
    raw = (raw or "").strip()
    if prd_se in ("M", "Q"):
        if len(raw) >= 6:
            return raw[:6]
        if len(raw) == 4:
            return raw + "01"
        return raw
    if len(raw) >= 4:
        return raw[:4]
    return raw


def _coerce_json(payload: Any) -> Dict[str, Any]:
    """KOSIS 응답을 {result: [...], err, errMsg} 형태로 정규화한다.

    * 정상 응답은 JSON 배열(테이블 목록) 또는 {"result": [...]} 객체다.
    * 오류 응답은 {"err":"11","errMsg":"..."} 형태이며, 어떤 경우에도 문자열일 수 있다.
    """
    data: Any = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    if isinstance(data, list):
        return {"result": data}
    if isinstance(data, dict):
        return data
    return {}


def _first_str(row: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
