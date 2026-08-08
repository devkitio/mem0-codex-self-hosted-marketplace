#!/usr/bin/env python3
"""从固定上游提交物化经过审查的 Mem0 生产源码。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "services" / "mem0-server" / "upstream.json"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _run(arguments: list[str], cwd: Path) -> str:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "无错误输出"
        raise RuntimeError(f"命令执行失败（{arguments[0]}）：{details}")
    return completed.stdout.strip()


def _load_manifest() -> tuple[dict[str, Any], Path]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("Mem0 上游清单格式无效")
    repository = manifest.get("repository")
    commit = manifest.get("commit")
    patch_name = manifest.get("patch")
    patch_sha256 = manifest.get("patch_sha256")
    if (
        not isinstance(repository, str)
        or not repository.startswith("https://github.com/")
        or not repository.endswith(".git")
        or not isinstance(commit, str)
        or not GIT_SHA_RE.fullmatch(commit)
        or not isinstance(patch_name, str)
        or Path(patch_name).name != patch_name
        or not isinstance(patch_sha256, str)
        or not SHA256_RE.fullmatch(patch_sha256)
    ):
        raise RuntimeError("Mem0 上游清单内容无效")
    patch_path = (MANIFEST_PATH.parent / patch_name).resolve()
    if patch_path.parent != MANIFEST_PATH.parent.resolve() or not patch_path.is_file():
        raise RuntimeError("Mem0 生产补丁不存在")
    actual_hash = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    if actual_hash != patch_sha256:
        raise RuntimeError("Mem0 生产补丁哈希不匹配")
    return manifest, patch_path


def materialize(target: Path) -> None:
    manifest, patch_path = _load_manifest()
    target = target.resolve()
    if target.exists():
        raise RuntimeError(f"目标目录已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".mem0-materialize-", dir=target.parent))
    try:
        _run(["git", "init", "--quiet"], temporary)
        _run(["git", "config", "core.autocrlf", "false"], temporary)
        _run(["git", "config", "core.eol", "lf"], temporary)
        _run(["git", "remote", "add", "origin", manifest["repository"]], temporary)
        _run(
            ["git", "fetch", "--quiet", "--depth", "1", "origin", manifest["commit"]],
            temporary,
        )
        _run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], temporary)
        if _run(["git", "rev-parse", "HEAD"], temporary) != manifest["commit"]:
            raise RuntimeError("物化后的 Mem0 上游提交不匹配")
        _run(
            [
                "git",
                "apply",
                "--check",
                "--unidiff-zero",
                "--whitespace=nowarn",
                str(patch_path),
            ],
            temporary,
        )
        _run(
            [
                "git",
                "apply",
                "--unidiff-zero",
                "--whitespace=nowarn",
                str(patch_path),
            ],
            temporary,
        )
        _run(["git", "diff", "--check"], temporary)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="从固定提交生成 Mem0 生产源码")
    parser.add_argument("target", type=Path, help="必须尚不存在的输出目录")
    arguments = parser.parse_args()
    materialize(arguments.target)
    print(f"Mem0 生产源码已物化到：{arguments.target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
