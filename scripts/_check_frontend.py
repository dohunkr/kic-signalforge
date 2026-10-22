"""프론트엔드 정적 검사 (JS 문법 · CSS 균형 · id 연결)."""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "web" / "static"


def main() -> int:
    failures = []

    # 1) JS 문법
    js_path = STATIC / "app.js"
    result = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    print("js syntax:", "OK" if result.returncode == 0 else "FAIL")
    if result.returncode != 0:
        failures.append(result.stderr[:400])

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    # 2) CSS 중괄호 균형
    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    print("css braces:", "OK" if depth == 0 else f"FAIL (depth={depth})")
    if depth != 0:
        failures.append("css brace imbalance")

    # 3) JS가 참조하는 id가 HTML에 있는가 (JS가 동적으로 만드는 id 도 허용)
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    html_ids |= set(re.findall(r'id=\\?"([^"\\]+)\\?"', js))
    used_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(used_ids - html_ids)
    print("js -> html id:", "OK" if not missing else f"MISSING {missing}")
    if missing:
        failures.append(f"missing ids: {missing}")

    # 4) Design.md 팔레트 compliance
    design_tokens = [
        "#6366F1", "#4F46E5", "#20970B", "#9C9C9C", "#FAFAFA", "#FFFFFF",
        "#0A0A0A", "#6B6B6B", "#E8E8EC", "#10B981", "#F59E0B", "#EF4444",
    ]
    css_upper = css.upper()
    absent = [t for t in design_tokens if t.upper() not in css_upper]
    print("design tokens:", "OK" if not absent else f"MISSING {absent}")
    if absent:
        failures.append(f"design tokens missing: {absent}")

    # 5) 폰트
    for font in ("General Sans", "DM Sans", "JetBrains Mono"):
        if font not in html and font not in css:
            failures.append(f"font missing: {font}")
    print("fonts:", "OK" if not any("font missing" in f for f in failures) else "FAIL")

    # 6) 프론트엔드에 키 이름이 없는가
    secret_markers = ["NVIDIA_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "KOSIS_API_KEY"]
    leaked = [m for m in secret_markers if m in html or m in js or m in css]
    print("secret markers:", "OK" if not leaked else f"FAIL {leaked}")
    if leaked:
        failures.append(f"secret markers in frontend: {leaked}")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("프론트엔드 정적 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
