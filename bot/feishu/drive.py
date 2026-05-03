from __future__ import annotations

import asyncio
import io
import re
import time
from typing import Any

import httpx
import lark_oapi as lark
from feishu.client import feishu_client
from feishu.contact import _tenant_access_token


def _lark_client() -> lark.Client:
    return feishu_client.client


async def delete_file(file_token: str | None, *, file_type: str) -> None:
    if not file_token:
        return
    from lark_oapi.api.drive.v1 import DeleteFileRequest

    req = DeleteFileRequest.builder().file_token(file_token).type(file_type).build()
    resp = await asyncio.to_thread(_lark_client().drive.v1.file.delete, req)
    if not resp.success() and "not" not in str(resp.msg).lower():
        raise RuntimeError(f"drive.delete_file failed: {resp.code} {resp.msg}")


async def upload_markdown_source(title: str, markdown_body: str, folder_token: str) -> str:
    from lark_oapi.api.drive.v1 import UploadAllFileRequest, UploadAllFileRequestBody

    filename = _safe_filename(title, suffix=".md")
    data = markdown_body.encode("utf-8")
    body = (
        UploadAllFileRequestBody.builder()
        .file_name(filename)
        .parent_type("explorer")
        .parent_node(folder_token)
        .size(len(data))
        .file(io.BytesIO(data))
        .build()
    )
    req = UploadAllFileRequest.builder().request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().drive.v1.file.upload_all, req)
    if not resp.success():
        raise RuntimeError(f"drive.upload_markdown_source failed: {resp.code} {resp.msg}")
    return resp.data.file_token


async def create_import_task(source_file_token: str, title: str, folder_token: str) -> str:
    from lark_oapi.api.drive.v1 import CreateImportTaskRequest, ImportTask, ImportTaskMountPoint

    point = ImportTaskMountPoint.builder().mount_type(1).mount_key(folder_token).build()
    task = (
        ImportTask.builder()
        .file_token(source_file_token)
        .file_extension("md")
        .type("docx")
        .file_name(_safe_filename(title, suffix=".docx"))
        .point(point)
        .build()
    )
    req = CreateImportTaskRequest.builder().request_body(task).build()
    resp = await asyncio.to_thread(_lark_client().drive.v1.import_task.create, req)
    if not resp.success():
        raise RuntimeError(f"drive.create_import_task failed: {resp.code} {resp.msg}")
    return resp.data.ticket


async def get_import_task(ticket: str) -> dict[str, Any]:
    from lark_oapi.api.drive.v1 import GetImportTaskRequest

    req = GetImportTaskRequest.builder().ticket(ticket).build()
    resp = await asyncio.to_thread(_lark_client().drive.v1.import_task.get, req)
    if not resp.success():
        raise RuntimeError(f"drive.get_import_task failed: {resp.code} {resp.msg}")
    task = getattr(resp.data, "result", None) or getattr(resp.data, "task", None) or getattr(resp.data, "import_task", None)
    if task is None and hasattr(resp.data, "ticket"):
        task = resp.data
    if task is None:
        return {}
    return {
        "ticket": getattr(task, "ticket", ticket),
        "job_status": getattr(task, "job_status", None),
        "job_error_msg": getattr(task, "job_error_msg", None),
        "doc_token": getattr(task, "token", None),
        "url": getattr(task, "url", None),
    }


async def poll_import_task(ticket: str, *, timeout_seconds: int = 300, interval_seconds: float = 0.5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await get_import_task(ticket)
        status = last.get("job_status")
        if status == 0 and last.get("doc_token"):
            return last
        if status not in {None, 1, 2}:
            raise RuntimeError(last.get("job_error_msg") or f"import task failed with status {status}")
        await asyncio.sleep(interval_seconds)
    raise TimeoutError(f"drive import task timed out: ticket={ticket} last={last}")


async def create_doc_from_markdown(title: str, markdown_body: str, folder_token: str) -> dict[str, Any]:
    """Create a docx from Markdown.

    Prefer the Drive import path. If the tenant has not enabled import
    permissions, the caller may fall back to `create_empty_docx`.
    """
    source_file_token = await upload_markdown_source(title, markdown_body, folder_token)
    ticket = await create_import_task(source_file_token, title, folder_token)
    result = await poll_import_task(ticket)
    return {
        "doc_token": result["doc_token"],
        "url": result.get("url") or _doc_url(result["doc_token"]),
        "source_file_token": source_file_token,
        "import_ticket": ticket,
    }


async def create_empty_docx(title: str, folder_token: str) -> dict[str, Any]:
    from lark_oapi.api.docx.v1 import CreateDocumentRequest, CreateDocumentRequestBody

    body = CreateDocumentRequestBody.builder().folder_token(folder_token).title(title).build()
    req = CreateDocumentRequest.builder().request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().docx.v1.document.create, req)
    if not resp.success():
        raise RuntimeError(f"docx.document.create failed: {resp.code} {resp.msg}")
    doc = resp.data.document
    token = doc.document_id
    return {"doc_token": token, "url": _doc_url(token)}


async def _root_folder_token() -> str:
    token = await _tenant_access_token()
    async with httpx.AsyncClient(timeout=10.0) as ac:
        resp = await ac.get(
            "https://open.feishu.cn/open-apis/drive/explorer/v2/root_folder/meta",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"drive.root_folder_meta failed: {data.get('code')} {data.get('msg')}")
    folder_token = (data.get("data") or {}).get("token")
    if not folder_token:
        raise RuntimeError("drive.root_folder_meta failed: missing root folder token")
    return folder_token


async def create_folder(name: str) -> str:
    from lark_oapi.api.drive.v1 import CreateFolderFileRequest, CreateFolderFileRequestBody

    body = CreateFolderFileRequestBody.builder().name(name).folder_token(await _root_folder_token()).build()
    req = CreateFolderFileRequest.builder().request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().drive.v1.file.create_folder, req)
    if not resp.success():
        raise RuntimeError(f"drive.create_folder failed: {resp.code} {resp.msg}")
    return resp.data.token


def _safe_filename(title: str, *, suffix: str) -> str:
    base = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", title).strip()[:80] or "untitled"
    return base if base.endswith(suffix) else base + suffix


def _doc_url(token: str) -> str:
    return ""
