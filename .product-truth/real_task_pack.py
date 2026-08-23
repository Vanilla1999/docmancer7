#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"[0-9a-f]{40}")
MANIFEST_PROTOCOL = "docatlas-source-real-task-pack-v1"
REPORT_PROTOCOL = "docatlas-source-real-task-gold-v1"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git(*args: str, cwd: Path = ROOT, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=cwd, input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def _require_git(*args: str, cwd: Path = ROOT, input_bytes: bytes | None = None) -> bytes:
    result = _git(*args, cwd=cwd, input_bytes=input_bytes)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _first_parent(commit: str) -> str:
    parts = _require_git("rev-list", "--parents", "-n", "1", commit).decode().strip().split()
    if len(parts) != 2:
        raise ValueError(f"{commit}: task fix must have exactly one parent")
    return parts[1]


def _changed_paths(base: str, head: str) -> list[str]:
    return [line for line in _require_git("diff", "--name-only", base, head).decode().splitlines() if line]


def _blob(commit: str, path: str) -> bytes:
    return _require_git("show", f"{commit}:{path}")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != 1 or manifest.get("protocol") != MANIFEST_PROTOCOL:
        raise ValueError("real-task manifest identity mismatch")
    frozen = str(manifest.get("frozen_inventory_head") or "")
    if SHA_RE.fullmatch(frozen) is None:
        raise ValueError("frozen_inventory_head must be a full Git SHA")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 8:
        raise ValueError("real-task pack requires exactly eight tasks")
    ids: set[str] = set()
    fixes: set[str] = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            raise ValueError("task row must be an object")
        task_id = str(task.get("id") or "")
        fix = str(task.get("fix_commit") or "")
        if not task_id or task_id in ids:
            raise ValueError("task ids must be unique")
        if SHA_RE.fullmatch(fix) is None or fix in fixes:
            raise ValueError(f"{task_id}: fix_commit must be a unique full SHA")
        ids.add(task_id); fixes.add(fix)
        for key in ("issue_text", "hidden_test_path", "public_nodeid", "hidden_nodeid"):
            if not isinstance(task.get(key), str) or not str(task[key]).strip():
                raise ValueError(f"{task_id}: {key} is required")
        gold_paths = task.get("gold_paths")
        if not isinstance(gold_paths, list) or not gold_paths or any(not isinstance(path, str) or not path for path in gold_paths):
            raise ValueError(f"{task_id}: explicit gold_paths are required")
        if len(str(task["issue_text"])) > 1200:
            raise ValueError(f"{task_id}: issue_text exceeds bound")
        hidden = Path(str(task["hidden_test_path"]))
        if hidden.is_absolute() or ".." in hidden.parts or "tests" not in hidden.parts:
            raise ValueError(f"{task_id}: hidden_test_path must stay under repository tests")


def _env(worktree: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(worktree) + (os.pathsep + existing if existing else "")
    return env


def _test(worktree: Path, nodeid: str, timeout: int) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", nodeid],
        cwd=worktree, env=_env(worktree), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout_sha256": _sha(result.stdout),
        "stderr_sha256": _sha(result.stderr),
    }


def _overlay_hidden(worktree: Path, fix: str, path: str) -> tuple[bytes | None, bool]:
    target = worktree / path
    existed = target.is_file()
    previous = target.read_bytes() if existed else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_blob(fix, path))
    return previous, existed


def _restore_hidden(worktree: Path, path: str, previous: bytes | None, existed: bool) -> None:
    target = worktree / path
    if existed:
        assert previous is not None
        target.write_bytes(previous)
    else:
        target.unlink(missing_ok=True)


def _attempt(
    task: Mapping[str, Any], *, base: str, fix: str, gold_paths: list[str], patch: bytes, number: int
) -> dict[str, Any]:
    hidden_path = str(task["hidden_test_path"])
    timeout = int(task.get("test_timeout_seconds") or 180)
    with tempfile.TemporaryDirectory(prefix=f"docmancer7-real-{task['id']}-{number}-") as raw:
        worktree = Path(raw) / "repo"
        _require_git("worktree", "add", "--detach", str(worktree), base)
        try:
            public_base = _test(worktree, str(task["public_nodeid"]), timeout)
            previous, existed = _overlay_hidden(worktree, fix, hidden_path)
            try:
                hidden_base = _test(worktree, str(task["hidden_nodeid"]), timeout)
            finally:
                _restore_hidden(worktree, hidden_path, previous, existed)

            applied = _git("apply", "--whitespace=nowarn", "-", cwd=worktree, input_bytes=patch)
            patch_applied = applied.returncode == 0
            changed: list[str] = []
            public_gold = {"returncode": 99, "stdout_sha256": "", "stderr_sha256": ""}
            hidden_gold = dict(public_gold)
            if patch_applied:
                changed = _require_git("diff", "--name-only", cwd=worktree).decode().splitlines()
                public_gold = _test(worktree, str(task["public_nodeid"]), timeout)
                previous, existed = _overlay_hidden(worktree, fix, hidden_path)
                try:
                    hidden_gold = _test(worktree, str(task["hidden_nodeid"]), timeout)
                finally:
                    _restore_hidden(worktree, hidden_path, previous, existed)

            exact_surface = sorted(changed) == sorted(gold_paths)
            passed = bool(
                public_base["returncode"] == 0
                and hidden_base["returncode"] == 1
                and patch_applied
                and exact_surface
                and public_gold["returncode"] == 0
                and hidden_gold["returncode"] == 0
            )
            return {
                "attempt": number,
                "public_base": public_base,
                "hidden_base": hidden_base,
                "patch_applied": patch_applied,
                "gold_surface_exact": exact_surface,
                "public_gold": public_gold,
                "hidden_gold": hidden_gold,
                "passed": passed,
            }
        finally:
            _git("worktree", "remove", "--force", str(worktree))
            _git("worktree", "prune")


def run_task(manifest: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    fix = str(task["fix_commit"])
    frozen = str(manifest["frozen_inventory_head"])
    if _git("merge-base", "--is-ancestor", fix, frozen).returncode:
        raise ValueError(f"{task['id']}: fix is not reachable from frozen inventory head")
    base = _first_parent(fix)
    changed = _changed_paths(base, fix)
    gold_paths = [str(path) for path in task["gold_paths"]]
    if any(path not in changed or path.startswith("tests/") for path in gold_paths):
        raise ValueError(f"{task['id']}: gold_paths are not a production subset of the fix")
    hidden_path = str(task["hidden_test_path"])
    if hidden_path not in changed:
        raise ValueError(f"{task['id']}: hidden repository test was not changed by the fix")
    patch = _require_git("diff", "--binary", base, fix, "--", *gold_paths)
    if not patch.strip():
        raise ValueError(f"{task['id']}: production-only gold patch is empty")
    attempts = [
        _attempt(task, base=base, fix=fix, gold_paths=gold_paths, patch=patch, number=number)
        for number in (1, 2)
    ]
    reproducible = all(item["passed"] for item in attempts)
    return {
        "id": task["id"],
        "fix_commit": fix,
        "base_commit": base,
        "issue_sha256": _sha(str(task["issue_text"]).encode("utf-8")),
        "hidden_test_path": hidden_path,
        "hidden_test_sha256": _sha(_blob(fix, hidden_path)),
        "production_paths": gold_paths,
        "gold_patch_sha256": _sha(patch),
        "attempts": attempts,
        "gold_reproducible": reproducible,
        "real_model_oracle_executed": False,
        "real_model_oracle_passed": False,
        "valid": False,
    }


def build_report(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    rows: list[dict[str, Any]] = []
    for task in manifest["tasks"]:
        try:
            rows.append(run_task(manifest, task))
        except Exception as exc:
            rows.append({
                "id": str(task.get("id") or "unknown"),
                "status": "setup_error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
                "attempts": [],
                "gold_reproducible": False,
                "real_model_oracle_executed": False,
                "real_model_oracle_passed": False,
                "valid": False,
            })
    gold = sum(row.get("gold_reproducible") is True for row in rows)
    return {
        "schema_version": 1,
        "protocol": REPORT_PROTOCOL,
        "repository": manifest["repository"],
        "frozen_inventory_head": manifest["frozen_inventory_head"],
        "manifest_sha256": _sha(_canonical(manifest)),
        "tasks": rows,
        "summary": {
            "task_count": 8,
            "gold_reproducible_tasks": gold,
            "real_model_oracle_tasks": 0,
            "valid_tasks": 0,
        },
        "claim_boundary": {
            "gold_control_complete": gold == 8,
            "real_model_oracle_complete": False,
            "task_pack_ready": False,
            "product_truth_proven": False,
            "product_failure_proven": False,
            "product_maturity": "Beta",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    report = build_report(_load(manifest_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"docmancer7 real-task gold: {report['summary']['gold_reproducible_tasks']}/8; oracle=0/8; valid=0/8")
    return 0 if report["summary"]["gold_reproducible_tasks"] == 8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
