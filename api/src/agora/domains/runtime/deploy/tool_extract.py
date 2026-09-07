"""tool_extract — 빌드 샌드박스(CodeBuild)에서 실행되는 tool 스키마 추출 유틸리티.

inline_refs(schema): $ref: #/$defs/X 를 재귀 해소해 self-contained JSON Schema로 만들어요.
extract_tools(mcp_instance): MCP 서버 인스턴스(FastMCP 1.x / MCPServer 2.0)에서
  (name, description, inputSchema) 목록을
  뽑아 inline_refs와 민감도 휴리스틱을 적용한 뒤
  [{"name","description","inputSchema","sensitivity"}] 를 반환해요.
discover_mcp_module(root): 소스 트리를 정적 스캔해 MCP 서버 인스턴스를 가진 모듈의
  점 표기 이름(예: "market_index_mcp.server")을 찾아요. mcp 1.x의 FastMCP와
  2.0의 MCPServer를 둘 다 탐지해요. import 부작용 없이 파일만 읽어요.

CLI 엔트리포인트:
  python -m agora.domains.runtime.deploy.tool_extract [--discover-module | --emit]
    (기본, 옵션 없음) : AGORA_MCP_MODULE에서 모듈을 import해 {"tools":[...]} JSON을 stdout에.
    --discover-module : AGORA_SOURCE_ROOT(기본 ".")를 스캔해 MCP 모듈명만 stdout에 출력.
    --emit            : 모듈을 탐지→import→tool 스키마 추출까지 한 번에. tools.json 파일을
                        쓰고 발견한 모듈명을 stdout에 출력해요(빌드가 MCP_MODULE로 캡처).
  buildspec 규약은 infra/lib/runtime-deploy-stack.ts 참고(I2 S3 사이드카 + C1 모듈명).

Task 0 스파이크 결과:
- await mcp.list_tools()는 async 함수 → asyncio.run()으로 호출.
- 반환: list[mcp.types.Tool], 각 Tool은 .name/.description/.inputSchema(dict) 보유.
- 중첩 pydantic BaseModel 파라미터는 $defs+$ref를 생성 → inline_refs로 해소 필요.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from ....shared.mcp_sensitivity import heuristic_sensitivity

# 모듈 최상위에서 MCP 서버 인스턴스를 만드는 흔한 패턴을 잡는 휴리스틱.
# mcp 1.x는 `mcp = FastMCP("...")`, 2.0은 `server = MCPServer(...)` — 둘 다 잡아요.
# import 없이 정적으로 탐지해요.
_MCP_SERVER_ASSIGN = re.compile(r"^\s*(\w+)\s*=\s*(?:FastMCP|MCPServer)\s*\(", re.MULTILINE)
# 스캔에서 건너뛸 디렉터리(설치된 의존성·캐시·테스트·가상환경).
_SKIP_DIRS = {"package", "__pycache__", "tests", "test", ".venv", "venv",
              ".git", "node_modules", "build", "dist", ".pytest_cache"}


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """$ref: #/$defs/X 참조를 재귀적으로 해소해 self-contained JSON Schema를 반환해요.

    - $defs가 없으면 schema를 그대로 반환해요(passthrough).
    - $defs가 있으면 깊은 복사 후 모든 $ref를 인라인하고 $defs 키를 제거해요.
    - 중첩 ref(A가 B를 참조하는 등)도 재귀적으로 해소해요.
    """
    if "$defs" not in schema:
        return schema

    defs = schema["$defs"]
    schema_copy = copy.deepcopy(schema)

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref.startswith("#/$defs/"):
                def_name = ref[len("#/$defs/"):]
                if def_name in defs:
                    # ref를 정의로 교체하고 재귀 해소(중첩 ref 처리)
                    return _resolve(copy.deepcopy(defs[def_name]))
                # 알 수 없는 ref는 그대로 둬요
                return node
            # ref 없는 dict: 모든 값에 재귀 적용
            return {k: _resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    result = _resolve(schema_copy)
    # 최상위 $defs 제거(이미 _resolve에서 제외했지만 명시적으로도 처리)
    result.pop("$defs", None)
    return result


# Gateway lambda 타깃 toolSchema가 허용하는 JSON Schema 키(실측: 2026-07-16).
# 이 외의 키(title·anyOf·default·enum 등)는 ValidationException을 유발해요.
_GATEWAY_ALLOWED_KEYS = {"type", "properties", "required", "items", "description"}


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gateway가 받는 제한된 JSON Schema 서브셋으로 정제해요.

    AgentCore Gateway lambda 타깃의 inlinePayload.inputSchema는 type·properties·
    required·items·description 만 허용해요(실측: title·anyOf·default·$ref 등은 거부).
    - pydantic이 붙이는 title 제거.
    - anyOf(Optional·Union) → 첫 구체 타입으로 축약하고 type을 채워요(type 필수).
    - default·enum 등 비허용 키 제거.
    재귀적으로 properties·items 하위까지 정제해요.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    # anyOf/oneOf(Optional·Union)를 첫 번째 non-null 분기로 축약해 type을 확보.
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants and "type" not in schema:
        concrete = next(
            (v for v in variants if isinstance(v, dict) and v.get("type") not in (None, "null")),
            None,
        )
        if concrete:
            merged = sanitize_schema(concrete)
            if "description" in schema and "description" not in merged:
                merged["description"] = schema["description"]
            return merged
    for k, v in schema.items():
        if k not in _GATEWAY_ALLOWED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = sanitize_schema(v)
        else:
            out[k] = v
    # object인데 type이 없으면 보강(Gateway는 type 필수).
    if "properties" in out and "type" not in out:
        out["type"] = "object"
    return out


def _module_name_for(py_file: Path, root: Path) -> tuple[str, Path]:
    """py_file의 점 표기 모듈명과 그 모듈이 import 가능해지는 sys.path 진입점을 계산해요.

    패키지 루트 탐지: __init__.py가 있는 조상 디렉터리를 계속 거슬러 올라가요.
    - src-layout(예: src/market_index_mcp/server.py, src에 __init__.py 없음)이면
      모듈명="market_index_mcp.server", 진입점=src.
    - 평탄(예: server.py가 root 직속)이면 모듈명="server", 진입점=root.
    반환: (dotted_module, path_entry)
    """
    parts = [py_file.stem]
    pkg_dir = py_file.parent
    # __init__.py가 있는 동안 패키지 경계를 거슬러 올라가며 패키지명을 쌓아요.
    while (pkg_dir / "__init__.py").exists() and pkg_dir != root.parent:
        parts.append(pkg_dir.name)
        pkg_dir = pkg_dir.parent
    parts.reverse()
    return ".".join(parts), pkg_dir


def discover_mcp_module(root: str | Path = ".") -> tuple[str, str]:
    """소스 트리를 정적 스캔해 MCP 서버 인스턴스를 가진 모듈명을 찾아요(C1).

    import 부작용 없이 .py 파일 텍스트에서 `X = FastMCP(`(1.x) 또는
    `X = MCPServer(`(2.0) 패턴을 정규식으로 탐지해요.
    후보가 여럿이면 경로가 가장 얕고(트리 상단), 사전순으로 앞선 것을 택해 결정적이에요.

    반환: (dotted_module, path_entry)
      dotted_module — Lambda의 AGORA_MCP_MODULE로 넘길 모듈명(예: "market_index_mcp.server").
      path_entry    — 그 모듈이 import 가능해지는 디렉터리(빌드가 PYTHONPATH에 추가).
    MCP 서버 모듈을 못 찾으면 RuntimeError.
    """
    root = Path(root).resolve()
    candidates: list[tuple[int, str, Path]] = []
    for py_file in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in py_file.relative_to(root).parts):
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _MCP_SERVER_ASSIGN.search(text):
            depth = len(py_file.relative_to(root).parts)
            candidates.append((depth, str(py_file), py_file))
    if not candidates:
        raise RuntimeError(
            f"MCP 서버 인스턴스를 {root} 소스 트리에서 찾지 못했어요. "
            "MCP 모듈에 `mcp = FastMCP(...)`(1.x) 또는 `server = MCPServer(...)`(2.0) "
            "같은 최상위 인스턴스가 있는지 확인하세요."
        )
    candidates.sort(key=lambda c: (c[0], c[1]))
    chosen = candidates[0][2]
    module, path_entry = _module_name_for(chosen, root)
    return module, str(path_entry)


def _tool_name_from_decorator(node: ast.Call | ast.Attribute, func_name: str) -> str | None:
    """@mcp.tool / @mcp.tool(name=..) 데코레이터면 tool 이름을, 아니면 None을 반환해요."""
    # @mcp.tool  (호출 없음) → ast.Attribute; @mcp.tool(...) → ast.Call
    call = node if isinstance(node, ast.Call) else None
    attr = call.func if call else node
    if not (isinstance(attr, ast.Attribute) and attr.attr == "tool"):
        return None
    if call:
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    return func_name


def discover_mcp_tools(files: dict[str, str]) -> list[dict]:
    """소스 파일 텍스트에서 @mcp.tool 데코레이터 함수를 정적 수집해요(코드 실행 없음).

    files: {상대경로: 소스텍스트}. .py가 아니거나 _SKIP_DIRS 하위 경로는 무시해요.
    반환: [{"name": str, "description": str}] — 파일·정의 순서를 보존해요.
    inputSchema는 뽑지 않아요(런타임 pydantic 생성). capability 연결엔 이름만 필요해요.
    """
    result: list[dict] = []
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        if any(part in _SKIP_DIRS for part in path.split("/")):
            continue
        try:
            tree = ast.parse(files[path])
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            # SyntaxError 외에도 null-byte 소스→ValueError, 과도한 중첩→
            # RecursionError/MemoryError가 ast.parse에서 나올 수 있어 손상 파일은 건너뛰어요.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                name = _tool_name_from_decorator(deco, node.name)
                if name is None:
                    continue
                doc = ast.get_docstring(node) or ""
                first = doc.strip().splitlines()[0] if doc.strip() else ""
                # 등록 시점 민감도 자동 제안(IA-22c) — CodeBuild와 동일하게 Bedrock 없이
                # 이름·설명 휴리스틱(Tier-1 + 보수적 기본)만. 등록자가 UI에서 검토·수정해요.
                sensitivity, rationale = heuristic_sensitivity(name, first, {})
                result.append({
                    "name": name,
                    "description": first,
                    "sensitivity": sensitivity,
                    "sensitivityRationale": rationale,
                })
                break
    return result


def extract_tools(mcp_instance) -> list[dict[str, Any]]:
    """FastMCP 인스턴스에서 tool 목록을 추출해요.

    Task 0 스파이크: mcp.list_tools()는 async → asyncio.run() 으로 호출.
    반환 형식: [{"name": str, "description": str, "inputSchema": dict}, ...]
    inputSchema는 inline_refs를 통과해 $ref/$defs 없는 self-contained 형태예요.
    """
    import asyncio

    tools_raw = asyncio.run(mcp_instance.list_tools())
    result = []
    for t in tools_raw:
        description = t.description or ""
        input_schema = sanitize_schema(inline_refs(t.inputSchema))
        sensitivity, _reason = heuristic_sensitivity(
            t.name, description, input_schema
        )
        result.append({
            "name": t.name,
            "description": description,
            # $ref 인라인 후 Gateway 허용 서브셋으로 정제(title·anyOf·default 제거).
            "inputSchema": input_schema,
            # CodeBuild에는 Bedrock 권한을 요구하지 않아요: Tier 1 + 보수적 기본만 사용.
            "sensitivity": sensitivity,
        })
    return result


def _run(instance=None) -> str:
    """FastMCP 인스턴스에서 tool 목록을 추출해 {"tools": [...]} JSON 문자열을 반환해요.

    instance가 None이면 AGORA_MCP_MODULE 환경변수를 읽어 모듈을 동적 import하고
    FastMCP 인스턴스를 탐색해요 (lambda_handler_template.py의 _tools_from_fastmcp와
    동일한 탐색 로직: mod.mcp 우선, 없으면 _tool_manager 속성 스캔).

    테스트에서는 instance를 직접 주입해 mcp 설치 없이 검증할 수 있어요.
    """
    import importlib

    if instance is None:
        module_name = os.environ["AGORA_MCP_MODULE"]
        mod = importlib.import_module(module_name)

        # FastMCP 인스턴스 탐색: lambda_handler_template.py _tools_from_fastmcp와 동일한 로직.
        # 1) mod.mcp 우선 시도 (가장 일반적인 속성명)
        # 2) 없으면 vars(mod).values() 스캔으로 _tool_manager 속성을 가진 객체 탐색(폴백)
        if hasattr(mod, "mcp") and hasattr(getattr(mod, "mcp"), "_tool_manager"):
            instance = mod.mcp
        else:
            for val in vars(mod).values():
                if hasattr(val, "_tool_manager"):
                    instance = val
                    break

        if instance is None:
            raise RuntimeError(
                f"MCP 서버 인스턴스를 {module_name}에서 찾지 못했어요. "
                "모듈에 MCP 서버 인스턴스(mcp = FastMCP(...) 또는 "
                "server = MCPServer(...))가 있는지 확인하세요."
            )

    tools = extract_tools(instance)
    return json.dumps({"tools": tools})


def _emit(root: str | Path, out_path: str | Path) -> str:
    """소스 트리에서 MCP 모듈을 탐지→import→tool 스키마 추출까지 한 번에 해요(--emit).

    tools.json을 out_path에 쓰고, 발견한 모듈명을 반환해요(빌드가 MCP_MODULE로 캡처).
    탐지한 path_entry를 sys.path에 추가해 src-layout 패키지도 import되게 해요.
    """
    import sys

    module, path_entry = discover_mcp_module(root)
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)
    os.environ["AGORA_MCP_MODULE"] = module   # 하위 로직·재현성 위해 노출.
    tools_json = _run()
    Path(out_path).write_text(tools_json, encoding="utf-8")
    return module


def main(argv: list[str] | None = None) -> None:
    """CLI 엔트리포인트. 모든 mcp/asyncio/importlib import는 lazy(함수 내부)라
    mcp 미설치 환경(테스트)에서도 이 모듈 import는 안전해요.

    옵션:
      (없음)            : AGORA_MCP_MODULE 모듈에서 {"tools":[...]} JSON을 stdout에 출력.
      --discover-module : AGORA_SOURCE_ROOT(기본 ".")를 스캔해 MCP 모듈명만 출력.
      --emit            : 탐지→추출까지 한 번에. tools.json(기본 "tools.json")을 쓰고
                          모듈명을 stdout에 출력. buildspec이 이걸로 MCP_MODULE·tools.json을 산출.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    root = os.environ.get("AGORA_SOURCE_ROOT", ".")
    if "--discover-module" in args:
        module, _ = discover_mcp_module(root)
        print(module)
    elif "--discover-path" in args:
        # 빌드가 이 디렉터리 내용을 package/ 루트로 복사해 src-layout을 평탄화해요.
        # (Lambda 함수 루트=package/ 에서 AGORA_MCP_MODULE import가 가능하도록.)
        _, path_entry = discover_mcp_module(root)
        print(path_entry)
    elif "--emit" in args:
        out_path = os.environ.get("AGORA_TOOLS_OUT", "tools.json")
        module = _emit(root, out_path)
        print(module)
    else:
        print(_run())


if __name__ == "__main__":
    main()
