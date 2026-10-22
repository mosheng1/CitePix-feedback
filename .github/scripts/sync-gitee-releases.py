#!/usr/bin/env python3
"""将 GitHub Releases 同步到 Gitee。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mosheng1/CitePix-feedback")
GITEE_REPO = os.environ["GITEE_REPO"]
GITHUB_TOKEN = os.environ["GH_TOKEN"]
GITEE_TOKEN = os.environ["GITEE_TOKEN"]
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
EVENT_PATH = Path(os.environ["GITHUB_EVENT_PATH"])


def request_json(
    base_url: str,
    path: str,
    token: str,
    method: str = "GET",
    payload: dict | None = None,
    gitee: bool = False,
):
    """调用 API，并在错误信息中隐藏令牌。"""
    url = f"{base_url}{path}"
    if gitee:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}access_token={urllib.parse.quote(token)}"
    headers = {"Accept": "application/json", "User-Agent": "CitePix-release-sync"}
    if not gitee:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"API 请求失败：{method} {path}，HTTP {error.code}，{detail}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"网络请求失败：{method} {path}，{error.reason}") from error


def request_form(path: str, token: str, method: str, payload: dict) -> dict:
    """按 Gitee API 要求以 formData 提交 Release 参数。"""
    url = f"{GITEE_API}{path}?access_token={urllib.parse.quote(token)}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "CitePix-release-sync",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"API 请求失败：{method} {path}，HTTP {error.code}，{detail}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"网络请求失败：{method} {path}，{error.reason}") from error


def github_releases() -> list[dict]:
    """按本次检出的标签逐个读取 Release，规避列表接口返回空数组的问题。"""
    tag_output = subprocess.run(
        ["git", "tag", "--list", "v*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    releases = []
    for tag in sorted(filter(None, tag_output.splitlines())):
        encoded_tag = urllib.parse.quote(tag, safe="")
        try:
            releases.append(
                request_json(
                    GITHUB_API,
                    f"/repos/{GITHUB_REPO}/releases/tags/{encoded_tag}",
                    GITHUB_TOKEN,
                )
            )
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                print(f"标签没有对应 GitHub Release，跳过：{tag}", flush=True)
                continue
            raise
    return releases


def gitee_releases() -> list[dict]:
    releases = []
    page = 1
    while True:
        batch = request_json(
            GITEE_API,
            f"/repos/{GITEE_REPO}/releases?per_page=100&page={page}",
            GITEE_TOKEN,
            gitee=True,
        )
        if not batch:
            return releases
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def event_release() -> dict | None:
    if EVENT_NAME == "workflow_dispatch":
        return None
    event = json.loads(EVENT_PATH.read_text(encoding="utf-8"))
    return event.get("release")


def release_assets(release_id: int) -> list[dict]:
    return request_json(
        GITEE_API,
        f"/repos/{GITEE_REPO}/releases/{release_id}/attach_files",
        GITEE_TOKEN,
        gitee=True,
    ) or []


def delete_asset(release_id: int, asset_id: int) -> None:
    request_json(
        GITEE_API,
        f"/repos/{GITEE_REPO}/releases/{release_id}/attach_files/{asset_id}",
        GITEE_TOKEN,
        method="DELETE",
        gitee=True,
    )


def download_asset(asset: dict, target: Path) -> None:
    try:
        subprocess.run(
            [
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--retry-all-errors",
                "--retry-delay",
                "5",
                "--connect-timeout",
                "30",
                "--max-time",
                "900",
                "--user-agent",
                "CitePix-release-sync",
                "--header",
                "Accept: application/octet-stream",
                "--header",
                f"Authorization: Bearer {GITHUB_TOKEN}",
                "--output",
                str(target),
                asset["url"],
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-500:]
        raise RuntimeError(f"下载附件失败：{asset['name']}，{detail}") from error


def upload_asset(release_id: int, asset: dict, path: Path) -> None:
    url = (
        f"{GITEE_API}/repos/{GITEE_REPO}/releases/{release_id}/attach_files"
        f"?access_token={urllib.parse.quote(GITEE_TOKEN)}"
    )
    try:
        subprocess.run(
            [
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--retry",
                "3",
                "--retry-all-errors",
                "--retry-delay",
                "5",
                "--connect-timeout",
                "30",
                "--max-time",
                "900",
                "--user-agent",
                "CitePix-release-sync",
                "--form",
                f"file=@{path};filename={asset['name']}",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-500:]
        raise RuntimeError(f"上传附件失败：{asset['name']}，{detail}") from error


def sync_assets(github_release: dict, gitee_release: dict) -> None:
    existing = {asset["name"]: asset for asset in release_assets(gitee_release["id"])}
    with tempfile.TemporaryDirectory(prefix="citepix-release-") as directory:
        for asset in github_release.get("assets", []):
            old = existing.get(asset["name"])
            if old and old.get("size") == asset.get("size"):
                print(f"附件已存在，跳过：{asset['name']}", flush=True)
                continue
            if old:
                delete_asset(gitee_release["id"], old["id"])
                print(f"已删除旧附件：{asset['name']}", flush=True)
            path = Path(directory) / asset["name"]
            download_asset(asset, path)
            upload_asset(gitee_release["id"], asset, path)
            print(f"已上传附件：{asset['name']}", flush=True)


def sync_release(github_release: dict, existing: list[dict]) -> None:
    tag = github_release["tag_name"]
    current = next((release for release in existing if release.get("tag_name") == tag), None)
    payload = {
        "tag_name": tag,
        "name": github_release.get("name") or tag,
        "body": github_release.get("body") or "",
        "prerelease": bool(github_release.get("prerelease", False)),
    }
    if current:
        current = request_form(
            f"/repos/{GITEE_REPO}/releases/{current['id']}",
            GITEE_TOKEN,
            method="PATCH",
            payload=payload,
        )
        print(f"已更新 Release：{tag}", flush=True)
    else:
        current = request_form(
            f"/repos/{GITEE_REPO}/releases",
            GITEE_TOKEN,
            method="POST",
            payload={
                **payload,
                "target_commitish": github_release.get("target_commitish") or tag,
            },
        )
        print(f"已创建 Release：{tag}", flush=True)
    sync_assets(github_release, current)


def delete_stale_releases(github_releases_list: list[dict], gitee_releases_list: list[dict]) -> None:
    """手动全量同步时删除 Gitee 中已从 GitHub 删除的 Release。"""
    github_tags = {release["tag_name"] for release in github_releases_list}
    for release in gitee_releases_list:
        if release.get("tag_name") in github_tags:
            continue
        request_json(
            GITEE_API,
            f"/repos/{GITEE_REPO}/releases/{release['id']}",
            GITEE_TOKEN,
            method="DELETE",
            gitee=True,
        )
        print(f"已删除多余 Gitee Release：{release.get('tag_name', release['id'])}", flush=True)


def main() -> None:
    if not GITEE_TOKEN:
        raise RuntimeError("环境变量 GITEE_TOKEN 为空")
    target_releases = gitee_releases()
    event = event_release()
    if EVENT_NAME == "release" and event and event.get("action") == "deleted":
        tag = event["tag_name"]
        current = next((release for release in target_releases if release.get("tag_name") == tag), None)
        if current:
            request_json(
                GITEE_API,
                f"/repos/{GITEE_REPO}/releases/{current['id']}",
                GITEE_TOKEN,
                method="DELETE",
                gitee=True,
            )
            print(f"已删除 Gitee Release：{tag}", flush=True)
        else:
            print(f"Gitee 没有对应 Release，无需删除：{tag}", flush=True)
        return

    if EVENT_NAME == "release" and event:
        release_id = event["id"]
        releases = [
            request_json(
                GITHUB_API,
                f"/repos/{GITHUB_REPO}/releases/{release_id}",
                GITHUB_TOKEN,
            )
        ]
    else:
        releases = github_releases()
        delete_stale_releases(releases, target_releases)
    errors = []
    for release in reversed(releases):
        try:
            sync_release(release, target_releases)
        except Exception as error:  # noqa: BLE001 - 继续处理其他版本后统一失败
            errors.append(f"{release.get('tag_name', '未知版本')}：{error}")
            print(f"同步版本失败，继续处理后续版本：{errors[-1]}", flush=True)
    if errors:
        raise RuntimeError("；".join(errors))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - 工作流需要输出可读的失败原因
        print(f"同步失败：{error}", file=sys.stderr)
        sys.exit(1)
