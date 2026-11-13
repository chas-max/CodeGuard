"""OSV 开源漏洞情报 MCP Server：支持 OSV.dev 实时查询与离线已知高危 CVE 兜底。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("codeguard-osv-security")

# 离线兜底已知高危包（在无公网 / CI 隔离环境仍可给出准确安全提示）
_OFFLINE_ADVISORIES: dict[tuple[str, str], list[dict[str, str]]] = {
    ("PyPI", "pyyaml"): [
        {
            "id": "GHSA-8q59-q68h-6hv4",
            "summary": "PyYAML < 5.4 yaml.load 存在任意代码执行风险，应升级并改用 safe_load",
            "fixed": "5.4",
            "severity": "CRITICAL",
        }
    ],
    ("PyPI", "requests"): [
        {
            "id": "GHSA-9wx4-h78v-vm56",
            "summary": "requests < 2.32.0 存在 .netrc 凭据泄露与 verify=False 降级风险",
            "fixed": "2.32.0",
            "severity": "MEDIUM",
        }
    ],
    ("PyPI", "urllib3"): [
        {
            "id": "GHSA-34jh-p97f-mpxf",
            "summary": "urllib3 < 2.2.2 重定向时可能泄露 Proxy-Authorization 请求头",
            "fixed": "2.2.2",
            "severity": "MEDIUM",
        }
    ],
    ("PyPI", "fastapi"): [
        {
            "id": "GHSA-qf9m-vfgh-m389",
            "summary": "FastAPI < 0.109.1 ReDoS via Content-Type header parsing",
            "fixed": "0.109.1",
            "severity": "HIGH",
        }
    ],
    ("PyPI", "jinja2"): [
        {
            "id": "GHSA-h75v-3vvj-5mfj",
            "summary": "Jinja2 < 3.1.4 xmlattr filter XSS 注入风险",
            "fixed": "3.1.4",
            "severity": "MEDIUM",
        }
    ],
    ("PyPI", "cryptography"): [
        {
            "id": "GHSA-6vqw-3v5j-54x4",
            "summary": "cryptography < 42.0.4 PKCS12 解析 NULL 指针解引用 DoS",
            "fixed": "42.0.4",
            "severity": "HIGH",
        }
    ],
    ("npm", "lodash"): [
        {
            "id": "GHSA-35jh-r3h4-6jhm",
            "summary": "lodash < 4.17.21 存在原型链污染与命令注入风险",
            "fixed": "4.17.21",
            "severity": "HIGH",
        }
    ],
}


def _offline_lookup(ecosystem: str, package: str, version: str) -> list[dict[str, str]]:
    key = (ecosystem, package.lower())
    entries = _OFFLINE_ADVISORIES.get(key, [])
    if not version:
        return entries
    return [dict(item, queried_version=version) for item in entries]


@mcp.tool()
def query_osv_vulnerability(
    package: str,
    ecosystem: str = "PyPI",
    version: str = "",
) -> dict[str, Any]:
    """查询开源依赖包在 OSV.dev 中的已知安全漏洞（无外网时自动回退离线 CVE 知识库）。"""
    pkg = (package or "").strip()
    eco = (ecosystem or "PyPI").strip()
    ver = (version or "").strip()
    if not pkg:
        return {"ok": False, "error": "package 不能为空", "vulns": []}

    payload: dict[str, Any] = {"package": {"name": pkg, "ecosystem": eco}}
    if ver:
        payload["version"] = ver

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post("https://api.osv.dev/v1/query", json=payload)
            resp.raise_for_status()
            data = resp.json()
        raw_vulns = data.get("vulns") or []
        summarized = [
            {
                "id": v.get("id", ""),
                "summary": (v.get("summary") or v.get("details") or "")[:240],
                "aliases": (v.get("aliases") or [])[:5],
            }
            for v in raw_vulns[:10]
        ]
        return {
            "ok": True,
            "source": "osv.dev",
            "package": pkg,
            "ecosystem": eco,
            "version": ver,
            "count": len(raw_vulns),
            "vulns": summarized,
        }
    except Exception as exc:
        fallback = _offline_lookup(eco, pkg, ver)
        return {
            "ok": True,
            "source": "offline_fallback",
            "package": pkg,
            "ecosystem": eco,
            "version": ver,
            "count": len(fallback),
            "vulns": fallback,
            "network_note": str(exc)[:160],
        }


def _parse_requirements(text: str) -> list[tuple[str, str, str]]:
    deps: list[tuple[str, str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:[=<>!~]+\s*([A-Za-z0-9_.\-]+))?", line)
        if match:
            deps.append(("PyPI", match.group(1), match.group(2) or ""))
    return deps


@mcp.tool()
def scan_manifest_dependencies(
    manifest_path: str = "requirements.txt",
    repo_root: str = "",
) -> dict[str, Any]:
    """扫描仓库依赖清单（requirements.txt / pyproject.toml / package.json）并检查高危漏洞。"""
    base = Path(repo_root or os.environ.get("CODEGUARD_REPO_ROOT") or ".").resolve()
    target = (base / manifest_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return {"ok": False, "error": f"拒绝越界路径: {manifest_path}"}
    if not target.is_file():
        return {"ok": False, "error": f"清单文件不存在: {manifest_path}"}

    content = target.read_text(encoding="utf-8", errors="replace")
    deps: list[tuple[str, str, str]] = []
    if target.name == "package.json":
        import json

        try:
            pkg_json = json.loads(content)
            for section in ("dependencies", "devDependencies"):
                for name, ver in (pkg_json.get(section) or {}).items():
                    deps.append(("npm", str(name), str(ver).lstrip("^~")))
        except Exception as exc:
            return {"ok": False, "error": f"package.json 解析失败: {exc}"}
    elif target.name == "pyproject.toml":
        for match in re.finditer(r'"([A-Za-z0-9_.\-]+)\s*([><=!~][^"]*)?"', content):
            deps.append(("PyPI", match.group(1), (match.group(2) or "").lstrip("=<>!~ ")))
    else:
        deps = _parse_requirements(content)

    findings: list[dict[str, Any]] = []
    for eco, name, ver in deps[:40]:
        vulns = _offline_lookup(eco, name, ver)
        if vulns:
            findings.append({"ecosystem": eco, "package": name, "version": ver, "vulns": vulns})

    return {
        "ok": True,
        "manifest": manifest_path,
        "parsed_count": len(deps),
        "flagged_count": len(findings),
        "findings": findings,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
