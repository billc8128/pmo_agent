from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import imaging
from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from db import queries


def build_meta_tools(ctx: RequestContext):
    @tool(
        "list_users",
        "List all known users (handle, display name, when they joined).",
        {},
    )
    async def list_users(args: dict) -> dict[str, Any]:
        try:
            return ok({"users": queries.list_profiles()})
        except Exception as e:
            return err(str(e))

    @tool(
        "lookup_user",
        "Resolve a single handle to a user record. Accepts handles with or without @.",
        {"handle": str},
    )
    async def lookup_user(args: dict) -> dict[str, Any]:
        try:
            h = args.get("handle", "")
            rec = queries.lookup_profile(h)
            if not rec:
                return ok({"found": False, "handle": h})
            return ok({"found": True, **rec})
        except Exception as e:
            return err(str(e))

    @tool(
        "get_recent_turns",
        "Fetch up to N recent turns for a user, optionally narrowed to a time window or project root.",
        {"user_id": str, "since": str, "until": str, "project_root": str, "limit": int},
    )
    async def get_recent_turns(args: dict) -> dict[str, Any]:
        try:
            rows = queries.recent_turns(
                args["user_id"],
                since_iso=args.get("since") or None,
                until_iso=args.get("until") or None,
                project_root=args.get("project_root") or None,
                limit=int(args.get("limit") or 50),
            )
            return ok({"turns": rows, "count": len(rows)})
        except Exception as e:
            return err(str(e))

    @tool(
        "get_project_overview",
        "Fetch cached per-project narrative summaries for a user.",
        {"user_id": str},
    )
    async def get_project_overview(args: dict) -> dict[str, Any]:
        try:
            return ok({"projects": queries.project_overview(args["user_id"])})
        except Exception as e:
            return err(str(e))

    @tool(
        "get_activity_stats",
        "Aggregate turn counts for the last N days, broken down by project and day.",
        {"user_id": str, "days": int},
    )
    async def get_activity_stats(args: dict) -> dict[str, Any]:
        try:
            return ok(queries.turn_counts_by_window(args["user_id"], days=int(args.get("days") or 7)))
        except Exception as e:
            return err(str(e))

    @tool(
        "generate_image",
        "Generate an image and embed it into the reply using [IMAGE:<image_key>].",
        {"prompt": str, "size": str},
    )
    async def generate_image(args: dict) -> dict[str, Any]:
        try:
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                return err("prompt is required")
            result = await imaging.generate_and_upload(
                conversation_key=ctx.conversation_key or "anon",
                prompt=prompt,
                size=(args.get("size") or "2K").strip(),
            )
            return ok(result)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}")

    @tool(
        "today_iso",
        "Return current UTC date and useful anchors. Also includes the asker open_id.",
        {},
    )
    async def today_iso(args: dict) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        user_timezone = "Asia/Shanghai"
        user_timezone_source = "fallback"
        if ctx.sender_open_id:
            try:
                from feishu import contact

                user = await contact.get_user(ctx.sender_open_id)
                if user.get("time_zone"):
                    user_timezone = user["time_zone"]
                    user_timezone_source = "feishu_contact"
            except Exception:
                pass
        return ok(
            {
                "now": now.isoformat(),
                "today_start": today_start.isoformat(),
                "yesterday_start": (today_start - timedelta(days=1)).isoformat(),
                "yesterday_end": today_start.isoformat(),
                "seven_days_ago": (now - timedelta(days=7)).isoformat(),
                "thirty_days_ago": (now - timedelta(days=30)).isoformat(),
                "asker_open_id": ctx.sender_open_id,
                "user_timezone": user_timezone,
                "user_timezone_source": user_timezone_source,
            }
        )

    @tool(
        "resolve_people",
        "Resolve people by handle/open_id/email/phone/name. Return resolved, ambiguous, unresolved.",
        {"people": list},
    )
    async def resolve_people(args: dict) -> dict[str, Any]:
        from feishu import contact

        resolved: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for raw in args.get("people") or []:
            if isinstance(raw, dict):
                open_id = (raw.get("open_id") or "").strip()
                email = (raw.get("email") or "").strip()
                phone = (raw.get("phone") or raw.get("mobile") or "").strip()
                name = (raw.get("name") or raw.get("handle") or "").strip()
            else:
                s0 = str(raw).strip()
                open_id = s0 if s0.startswith("ou_") else ""
                email = s0 if "@" in s0 and "." in s0 else ""
                phone = s0 if s0.replace("+", "").replace("-", "").isdigit() and len(s0) >= 7 else ""
                name = "" if open_id or email or phone else s0
            s = open_id or email or phone or name
            if s.startswith("ou_"):
                user = await contact.get_user(s)
                resolved.append({"input": s, **user, "source": "open_id"})
                continue
            prof = queries.lookup_profile_by_handle_or_display(name or s) if (name or s) and not email and not phone else None
            if prof:
                linked = queries.lookup_feishu_link_by_user_id(prof["id"])
                if linked and linked.get("open_id"):
                    resolved.append({"input": name or s, **linked, "source": "profiles"})
                else:
                    unresolved.append(name or s)
                continue
            if email or phone:
                linked = queries.lookup_feishu_link_by_email(email) if email else None
                if not linked and phone:
                    linked = queries.lookup_feishu_link_by_phone(phone)
                if linked and linked.get("open_id"):
                    resolved.append({"input": s, **linked, "source": "profiles"})
                    continue
                found = await contact.batch_get_id_by_email_or_phone(
                    emails=[email] if email else None,
                    phones=[phone] if phone else None,
                )
                users = [u for u in (found.get("users") or []) if u.get("open_id")]
                if len(users) == 1:
                    resolved.append({"input": s, **users[0], "source": "email_or_phone"})
                elif len(users) > 1:
                    ambiguous.append({"input": s, "candidates": users})
                else:
                    unresolved.append(s)
                continue
            try:
                candidates = await contact.search_users(name) if name else []
            except Exception:
                candidates = []
            if len(candidates) == 1:
                resolved.append({"input": name, **candidates[0], "source": "directory_search"})
            elif len(candidates) > 1:
                ambiguous.append({"input": name, "candidates": candidates})
            else:
                unresolved.append(name)
        return ok({"resolved": resolved, "ambiguous": ambiguous, "unresolved": unresolved})

    @tool(
        "undo_last_action",
        "Undo the asker's most recent bot write action in this chat, or a selected target.",
        {"last_for_me": bool, "target_id": str, "target_kind": str},
    )
    async def undo_last_action(args: dict) -> dict[str, Any]:
        try:
            row = None
            if args.get("target_id") and args.get("target_kind"):
                row = queries.get_bot_action_by_target(
                    chat_id=ctx.chat_id,
                    sender_open_id=ctx.sender_open_id,
                    target_id=args["target_id"],
                    target_kind=args["target_kind"],
                )
            else:
                row = queries.last_bot_action_for_sender_in_chat(
                    chat_id=ctx.chat_id,
                    sender_open_id=ctx.sender_open_id,
                )
            if row is None:
                return ok({"status": "noop", "message": "没找到可撤销的最近动作"})
            if row is queries.LastIsInFlight:
                return ok({"status": "in_flight", "message": "上一个动作还在进行中，等它结束后再撤销"})
            if row is queries.LastWasUnreachable:
                return err("无法自动撤销上一个动作，请人工检查")
            return ok(await _undo_row(row))
        except Exception as e:
            return err(f"{type(e).__name__}: {e}")

    return [
        list_users,
        lookup_user,
        get_recent_turns,
        get_project_overview,
        get_activity_stats,
        generate_image,
        today_iso,
        resolve_people,
        undo_last_action,
    ]


async def _undo_row(row: dict[str, Any]) -> dict[str, Any]:
    from feishu import bitable, calendar, docx, drive

    action_type = row.get("action_type")
    target_kind = row.get("target_kind")
    result = row.get("result") or {}
    target_id = row.get("target_id")

    if row.get("status") == "undone":
        return {"status": "already_undone", "source_action_id": row.get("id")}

    ws = queries.get_bot_workspace()
    if action_type in {"schedule_meeting", "restore_schedule_meeting"} and target_id:
        await calendar.delete_event(result.get("calendar_id") or (ws or {}).get("calendar_id"), target_id)
    elif action_type == "cancel_meeting":
        snapshot = result.get("pre_cancel_event_snapshot") or {}
        calendar_id = result.get("calendar_id") or snapshot.get("calendar_id")
        try:
            await calendar.get_event(calendar_id, target_id)
            restored = None
        except Exception as e:
            if "not" not in str(e).lower() and "404" not in str(e):
                raise
            restored = await _restore_cancelled_meeting(row, snapshot, calendar_id)
            if restored.get("partial_success"):
                queries.record_undo_audit(
                    row,
                    result_patch=restored,
                    status="reconciled_unknown",
                    error=restored.get("error"),
                )
                return {
                    "status": "partial_success",
                    "source_action_id": row.get("id"),
                    "source_action_type": action_type,
                    **restored,
                }
    elif action_type in {"create_doc", "create_meeting_doc"}:
        if target_kind == "file":
            await drive.delete_file(target_id or result.get("source_file_token"), file_type="file")
        elif target_kind == "docx":
            await drive.delete_file(target_id or result.get("doc_token"), file_type="docx")
            if result.get("source_file_token"):
                try:
                    await drive.delete_file(result["source_file_token"], file_type="file")
                except Exception:
                    pass
        elif result.get("import_ticket"):
            try:
                imported = await drive.get_import_task(result["import_ticket"])
                if imported.get("doc_token"):
                    await drive.delete_file(imported["doc_token"], file_type="docx")
            finally:
                await drive.delete_file(result.get("source_file_token"), file_type="file")
        elif result.get("source_file_token"):
            await drive.delete_file(result["source_file_token"], file_type="file")
    elif action_type == "append_to_doc" and target_kind == "docx_block_append":
        await docx.delete_blocks(target_id, result.get("parent_block_id") or target_id, result.get("appended_block_ids") or [])
    elif action_type == "append_action_items" and ws:
        await bitable.batch_delete_records(ws["base_app_token"], row["target_id"], result.get("record_ids") or [])
    elif action_type == "append_to_my_table" and ws:
        await bitable.batch_delete_records(ws["base_app_token"], row["target_id"], result.get("record_ids") or [])
    elif action_type == "create_bitable_table" and ws:
        await bitable.delete_table(ws["base_app_token"], target_id)
    else:
        return {"status": "unsupported", "source_action_type": action_type}

    queries.retire_source_action(row["id"])
    queries.record_undo_audit(row)
    return {"status": "undone", "source_action_id": row.get("id"), "source_action_type": action_type}


async def _restore_cancelled_meeting(source_row: dict[str, Any], snapshot: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    from feishu import calendar

    result = source_row.get("result") or {}
    if result.get("source_meeting_action_id"):
        queries.retire_source_action(result["source_meeting_action_id"])

    restore_message_id = f"restore:{source_row['id']}"
    existing = queries.get_bot_action(restore_message_id, "restore_schedule_meeting")
    if existing and existing.get("status") in {"success", "reconciled_unknown"}:
        return existing.get("result") or {}

    attendees = snapshot.get("attendees") or []
    title = snapshot.get("summary") or snapshot.get("title") or "Restored meeting"
    restore_row = existing
    if restore_row is None:
        try:
            restore_row = queries.insert_bot_action_pending(
                message_id=restore_message_id,
                chat_id=source_row["chat_id"],
                sender_open_id=source_row["sender_open_id"],
                action_type="restore_schedule_meeting",
                args={"source_cancel_action_id": source_row["id"], "snapshot": snapshot},
                logical_key=f"restore:{source_row['id']}",
                result={"predecessor_action_id": source_row["id"], "attendees": attendees},
            )
        except queries.BotActionInsertConflict as exc:
            restore_row = exc.existing_row
    if not restore_row:
        raise RuntimeError("failed to create restore audit row")

    created = await calendar.create_event(
        calendar_id=calendar_id,
        title=title,
        start_time=snapshot["start_time"],
        end_time=snapshot["end_time"],
        description=snapshot.get("description") or "",
        idempotency_key=f"restore_schedule_meeting:{restore_row['id']}",
    )
    queries.record_bot_action_target_pending(
        restore_row["id"],
        target_id=created["event_id"],
        target_kind="calendar_event",
        result_patch={**created, "predecessor_action_id": source_row["id"], "attendees": attendees},
    )
    try:
        await calendar.invite_attendees(calendar_id, created["event_id"], attendees)
    except Exception as e:
        error = f"restore_attendee_invite_failed: {type(e).__name__}: {e}"
        partial_result = {
            **created,
            "attendees": attendees,
            "predecessor_action_id": source_row["id"],
            "restore_action_id": restore_row["id"],
            "partial_success": True,
            "reconciliation_kind": "partial_success",
            "error": error,
        }
        queries.record_bot_action_target_pending(
            restore_row["id"],
            result_patch=partial_result,
        )
        queries.mark_bot_action_reconciled_unknown(
            restore_row["id"],
            reconciliation_kind="partial_success",
            error=error,
            keep_lock=True,
        )
        return partial_result
    restored = {**created, "attendees": attendees, "predecessor_action_id": source_row["id"]}
    queries.mark_bot_action_success(restore_row["id"], restored)
    return restored


def build_meta_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_meta",
        version="0.1.0",
        tools=build_meta_tools(ctx),
    )
