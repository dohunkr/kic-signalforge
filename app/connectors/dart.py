"""금융감독원 OpenDART 커넥터.

확인된 사실(2026-10-21 실측):
  * Base URL : https://opendart.fss.or.kr/api
  * /api/list.json?crtfc_key=...&corp_code=...      -> 키가 틀리면 {"status":"010"} (미등록 키)
  * /api/corpCode.xml?crtfc_key=...                 -> 키가 틀리면 XML에 status 010
  * 인증키(crtfc_key)는 40자리, corp_code 는 8자리.
  * 오류 status(공식 가이드): 010/011/012 인증, 013/014 데이터없음, 020/021 한도,
    100/101 요청값, 800 점검, 900 미정의, 901 기한만료.

주의: 이 커넥터는 인증키가 없으면 "가짜" 데이터를 만들지 않고 실패로 보고한다.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import DartSettings
from ..errors import ConnectorFailure, ERR_AUTH, ERR_NOT_FOUND, map_dart_status
from ..models import ConnectorError, Metric, ProviderName, Provenance, SourceDoc
from ..pipeline.parser import html_to_text
from ..observability import get_logger
from .base import Connector, ConnectorRequest, ConnectorResult
from .http_client import HttpClient

log = get_logger(__name__)

#: Dart 공개 viewer URL (사용자가 클릭해 원문 확인 가능)
DART_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"

# 자주 조회하는 상장사는 DART 대용량 기업코드 ZIP을 매 요청마다 받지 않도록
# 공식 OpenDART 코드와 종목 코드를 함께 캐시한다.
_FAST_CORP_RECORDS = {
    "삼성전자": {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
}

#: 재무제표 조회용 보고서 코드 (공식 가이드)
REPORT_CODES = {
    "annual": "11011",     # 사업보고서
    "half": "11012",       # 반기보고서
    "quarter": "11013",    # 분기보고서
    "audit": "11014",      # 감사보고서
}


class DartConnector(Connector):
    """OpenDART 공시 커넥터."""

    provider = ProviderName.DART
    kind = "disclosures"
    label = "금융감독원 OpenDART"

    def __init__(
        self,
        settings: DartSettings,
        http: HttpClient,
        *,
        corp_cache: Optional[Dict[str, str]] = None,
        cache_dir: Optional[Path] = None,
        cache_ttl_hours: int = 24,
        fetch_bodies: int = 3,
    ) -> None:
        super().__init__(max_retries=2, timeout=settings.timeout_seconds)
        self._settings = settings
        self._http = http
        self._corp_cache: Dict[str, str] = dict(corp_cache or {})
        self._record_cache: Dict[str, Dict[str, str]] = {}
        self._cache_dir = cache_dir
        self._cache_ttl_hours = cache_ttl_hours
        self._fetch_bodies = fetch_bodies

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
                        "OpenDART 인증키(DART_API_KEY)가 설정되지 않아 공시 자료를 수집하지 못했습니다."
                    ),
                    detail="키 발급: https://opendart.fss.or.kr -> 회원가입 -> 인증키 신청/관리",
                )
            )
            return result

        company = (request.company or request.query or "").strip()
        if not company:
            result.notes.append("회사명이 없어 DART 공시 검색을 건너뛰었습니다.")
            return result

        corp_code = self._resolve_corp_code(company)
        if corp_code is None:
            result.errors.append(
                ConnectorError(
                    provider=self.provider,
                    code=ERR_NOT_FOUND,
                    message=f"OpenDART 기업코드에서 '{company}' 을 찾지 못했습니다.",
                    detail="공식 기업명(또는 등록된 별칭)으로 다시 시도하십시오.",
                )
            )
            return result

        disclosures = self._fetch_disclosures(corp_code, request)
        if disclosures is None:
            return result
        result.sources.extend(disclosures)
        # IR URL 을 주지 않아도 Agent 가 공시 원문을 직접 확보한다.
        self._attach_document_bodies(result.sources)
        return result

    def _attach_document_bodies(self, sources: List[SourceDoc]) -> None:
        """상위 공시 몇 건의 원문을 내려받아 raw_text 를 채운다."""
        if self._fetch_bodies <= 0:
            return
        for source in sources[: self._fetch_bodies]:
            rcept_no = str(source.extra.get("rcept_no") or "")
            if not rcept_no:
                continue
            body = self.fetch_document_body(rcept_no)
            if not body:
                continue
            source.raw_text = body
            source.excerpt = body[:200]
            source.extra["body_fetched"] = True
            source.extra["body_chars"] = len(body)

    # --- 기업코드 -------------------------------------------------------
    def _resolve_corp_code(self, company: str) -> Optional[str]:
        record = self._resolve_record(company)
        return record.get("corp_code") if record else None

    def _resolve_stock_code(self, company: str) -> Optional[str]:
        """6자리 종목코드(시세 심볼 해석에 사용)."""
        record = self._resolve_record(company)
        return record.get("stock_code") if record else None

    def _resolve_record(self, company: str) -> Optional[Dict[str, str]]:
        """기업명 → {corp_code, stock_code, corp_name}."""
        if company in self._record_cache:
            return self._record_cache[company]
        normalized = company.replace(" ", "").replace("(주)", "").replace("㈜", "")
        for name, record in _FAST_CORP_RECORDS.items():
            key = name.replace(" ", "").replace("(주)", "").replace("㈜", "")
            if normalized and (normalized == key or normalized in key or key in normalized):
                self._record_cache[company] = record
                return record
        try:
            records = self._load_corp_records()
        except ConnectorFailure as exc:
            log.warning("DART 기업코드 목록 조회 실패: %s", exc.code)
            return None
        if not records:
            return None
        for name, record in records.items():
            if name == company:
                self._record_cache[company] = record
                return record
        for name, record in records.items():
            key = name.replace(" ", "").replace("(주)", "").replace("㈜", "")
            if normalized and (normalized == key or normalized in key or key in normalized):
                self._record_cache[company] = record
                return record
        return None

    def find_companies(self, text: str, *, limit: int = 3) -> List[str]:
        """문장에 등장하는 상장사 이름을 찾아낸다(질문에서 기업 자동 인식용).

        기업코드 목록에 실제로 존재하는 이름만 후보가 되므로
        '2025년' 같은 일반 단어가 걸러진다.
        """
        if not text:
            return []
        fast_matches = [name for name in _FAST_CORP_RECORDS if name in text]
        if fast_matches:
            return sorted(fast_matches, key=len, reverse=True)[:limit]
        try:
            records = self._load_corp_records()
        except ConnectorFailure:
            return []
        found: List[str] = []
        for name in sorted(records, key=len, reverse=True):
            if len(name) < 2 or name not in text:
                continue
            # 더 긴 이름이 이미 잡혔다면 짧은 이름은 중복으로 본다.
            if any(name in existing for existing in found):
                continue
            found.append(name)
            if len(found) >= limit:
                break
        return found

    def _load_corp_codes(self) -> Dict[str, str]:
        """{기업명: 기업코드} (호환용)."""
        return {name: rec["corp_code"] for name, rec in self._load_corp_records().items()}

    def _load_corp_records(self) -> Dict[str, Dict[str, str]]:
        """corpCode.xml(또는 그 ZIP)을 읽어 기업별 레코드를 만든다.

        전체 목록은 수만 건이므로 로컬 캐시(TTL 적용)를 사용한다.
        """
        cached = self._read_corp_cache()
        if cached:
            return cached

        url = f"{self._settings.base_url.rstrip('/')}/corpCode.xml"
        status, payload = self._http.get_bytes(
            url,
            provider=self.provider.value,
            endpoint="corpCode.xml",
            params={"crtfc_key": self._settings.api_key},
        )
        if not payload:
            raise ConnectorFailure(
                ERR_NOT_FOUND, "기업코드 응답이 비어 있습니다.", provider=self.provider.value
            )
        # DART 는 XML 또는 ZIP(바이너리) 두 형태로 내려준다.
        if payload[:2] == b"PK":
            body = self._read_corp_code_zip(payload)
        else:
            text = payload.decode("utf-8", errors="replace")
            if not text.strip():
                raise ConnectorFailure(
                    ERR_NOT_FOUND, "기업코드 응답이 비어 있습니다.", provider=self.provider.value
                )
            self._raise_if_dart_error_xml(text)
            body = text

        records: Dict[str, Dict[str, str]] = {}
        for match in re.finditer(r"<list>(.*?)</list>", body, re.S):
            block = match.group(1)
            corp_code = _tag(block, "corp_code")
            name = _tag(block, "corp_name")
            if corp_code and name:
                records[name] = {
                    "corp_code": corp_code,
                    "corp_name": name,
                    "stock_code": (_tag(block, "stock_code") or "").strip(),
                }
        if not records:
            raise ConnectorFailure(
                "parse", "기업코드 목록을 파싱하지 못했습니다.", provider=self.provider.value
            )
        self._write_corp_cache(records)
        return records

    # --- 기업코드 캐시 ---------------------------------------------------
    @property
    def _cache_path(self) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        return Path(self._cache_dir) / "dart_corp_codes.json"

    def _read_corp_cache(self) -> Dict[str, Dict[str, str]]:
        path = self._cache_path
        if path is None or not path.exists():
            return {}
        try:
            age_hours = (time.time() - path.stat().st_mtime) / 3600
            if age_hours > self._cache_ttl_hours:
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            records = data.get("records")
            if isinstance(records, dict) and records:
                return {
                    str(k): {str(kk): str(vv) for kk, vv in v.items()}
                    for k, v in records.items()
                    if isinstance(v, dict)
                }
        except Exception:  # noqa: BLE001
            return {}
        return {}

    def _write_corp_cache(self, records: Dict[str, Dict[str, str]]) -> None:
        path = self._cache_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"fetched_at": time.time(), "records": records}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("기업코드 캐시 저장 실패(무시): %s", type(exc).__name__)

    @staticmethod
    def _read_corp_code_zip(payload: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".xml"):
                        raw = zf.read(name)
                        for enc in ("utf-8", "cp949", "euc-kr"):
                            try:
                                return raw.decode(enc)
                            except UnicodeDecodeError:
                                continue
                        return raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            raise ConnectorFailure(
                "parse",
                "기업코드 ZIP을 읽지 못했습니다.",
                provider="dart",
                detail=type(exc).__name__,
            ) from exc
        raise ConnectorFailure("parse", "기업코드 ZIP에 XML이 없습니다.", provider="dart")

    # --- 공시 목록 ------------------------------------------------------
    def _fetch_disclosures(
        self, corp_code: str, request: ConnectorRequest
    ) -> Optional[List[SourceDoc]]:
        bgn, end = self._date_range(request)
        url = f"{self._settings.base_url.rstrip('/')}/list.json"
        try:
            _, payload = self._http.get_json(
                url,
                provider=self.provider.value,
                endpoint="list.json",
                params={
                    "crtfc_key": self._settings.api_key,
                    "corp_code": corp_code,
                    "bgn_de": bgn,
                    "end_de": end,
                    "last_reprt_at": "Y",
                },
            )
        except ConnectorFailure:
            raise
        if not isinstance(payload, dict):
            raise ConnectorFailure(
                "parse", "공시 목록 응답 형식이 올바르지 않습니다.", provider=self.provider.value
            )
        status = str(payload.get("status", ""))
        if status != "000":
            code, message = map_dart_status(status)
            raise ConnectorFailure(
                code,
                message,
                provider=self.provider.value,
                detail=f"DART status={status}",
            )
        items = payload.get("list") or []
        if not items:
            raise ConnectorFailure(
                ERR_NOT_FOUND,
                f"선택한 기간({bgn}~{end})에 공시 자료가 없습니다.",
                provider=self.provider.value,
                detail=f"corp_code={corp_code}",
            )

        sources: List[SourceDoc] = []
        for item in items[: request.max_items]:
            rcp_no = item.get("rcept_no")
            report_nm = (item.get("report_nm") or "공시").strip()
            rcept_dt = item.get("rcept_dt")
            try:
                published = date(int(rcept_dt), 4, 1) if False else (
                    date(int(rcept_dt[:4]), int(rcept_dt[4:6]), int(rcept_dt[6:8]))
                    if rcept_dt and len(str(rcept_dt)) == 8
                    else None
                )
            except (TypeError, ValueError):
                published = None
            raw_text = "\n".join(
                [
                    f"공시명: {report_nm}",
                    f"제출자: {item.get('flr_nm') or '제출자'}",
                    f"접수일자: {rcept_dt}",
                    f"접수번호: {rcp_no}",
                    f"기업코드: {corp_code}",
                ]
            )
            sources.append(
                SourceDoc(
                    source_id=f"dart:{rcp_no}",
                    provider=self.provider,
                    title=f"[DART] {report_nm}",
                    url=DART_VIEW_URL.format(rcp_no=rcp_no),
                    provenance=Provenance.LIVE,
                    retrieved_at=_now(),
                    published_at=published,
                    period_label=_period_from_rcept(rcept_dt),
                    excerpt=(
                        f"{item.get('flr_nm') or '제출자'} 제출 · {report_nm} · 접수 {rcept_dt}"
                    ),
                    raw_text=raw_text,
                    publisher=item.get("flr_nm"),
                    doc_id=str(rcp_no),
                    extra={
                        "rcept_no": rcp_no,
                        "corp_code": corp_code,
                        "report_nm": report_nm,
                        "rm": item.get("rm"),
                        "ele_id": item.get("ele_id"),
                        "dcm_no": item.get("dcm_no"),
                    },
                )
            )
        return sources

    # --- 공시 원문(자동 IR 수집) -------------------------------------------
    def fetch_document_body(self, rcept_no: str, *, max_chars: int = 6000) -> Optional[str]:
        """공시서 원문을 내려받아 평문으로 변환한다.

        document.xml 은 ZIP으로 오며, 내부에 접수번호.xml 이 들어 있다.
        사용자가 IR URL 을 주지 않아도 Agent 가 공시 원문을 직접 확보할 수 있다.
        """
        url = f"{self._settings.base_url.rstrip('/')}/document.xml"
        try:
            _, payload = self._http.get_bytes(
                url,
                provider=self.provider.value,
                endpoint="document.xml",
                params={"crtfc_key": self._settings.api_key, "rcept_no": rcept_no},
            )
        except ConnectorFailure:
            return None
        if not payload or payload[:2] != b"PK":
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".xml"):
                        continue
                    raw = zf.read(name)
                    text = raw.decode("utf-8", errors="replace")
                    plain = html_to_text(text)
                    plain = re.sub(r"\n{2,}", "\n", plain).strip()
                    if plain:
                        return plain[:max_chars]
        except Exception as exc:  # noqa: BLE001
            log.info("공시 원문 파싱 실패(무시): %s", type(exc).__name__)
            return None
        return None

    # --- 보조 ------------------------------------------------------------
    @staticmethod
    def _date_range(request: ConnectorRequest) -> Tuple[str, str]:
        today = date.today()
        if request.start_date and request.end_date:
            bgn = request.start_date.replace("-", "")
            end = request.end_date.replace("-", "")
            if len(bgn) == 8 and len(end) == 8:
                return bgn, end
        return (today - timedelta(days=365 * 2)).strftime("%Y%m%d"), today.strftime("%Y%m%d")


def _tag(block: str, name: str) -> Optional[str]:
    m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
    return m.group(1).strip() if m else None


def _raise_if_dart_error_xml(text: str) -> None:
    status = _tag(text, "status")
    if status and status != "000":
        code, message = map_dart_status(status)
        raise ConnectorFailure(
            code,
            message,
            provider="dart",
            detail=f"DART status={status}",
        )


def _period_from_rcept(rcept_dt: Optional[str]) -> Optional[str]:
    raw = str(rcept_dt or "")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
