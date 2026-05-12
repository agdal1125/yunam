#!/usr/bin/env python3
"""Smoke test for multi-principal identity + visibility ACL (Phase A+B).

Exercises the v6 schema migration, persist_turn with user_id+visibility,
load_history with viewer ACL, and the privacy skill's mark_turn_private
tool — all against a real SQLite DB in a tempdir, without touching
Anthropic, Voyage, or Telegram.

Usage (from repo root):
    PYTHONPATH=gateway python3 scripts/smoke_multiuser.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path


def _setup_path() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))


async def _test_v6_migration_and_persist() -> None:
    print("[1/5] v6 schema migration + persist_turn with user_id/visibility...")
    from yunam.sessions import SessionStore, ToolCall

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        store = await SessionStore.open(db_path)
        try:
            # jaekeun (shared) turn
            await store.persist_turn(
                chat_id=42,
                user_text="hello",
                assistant_text="hi",
                tool_calls=[],
                principal_user_id=6022344291,
                visibility="shared",
            )
            # jaekeun private turn
            await store.persist_turn(
                chat_id=42,
                user_text="이건 비밀이야",
                assistant_text="ok",
                tool_calls=[ToolCall(
                    name="dummy", input={}, result_preview="x",
                    is_error=False, elapsed_ms=1, skill_id="x", scope="x",
                    principal_user_id=6022344291,
                )],
                principal_user_id=6022344291,
                visibility="private:6022344291",
            )
            # yoolim shared turn
            await store.persist_turn(
                chat_id=42,
                user_text="자기야",
                assistant_text="ㅎㅇ",
                tool_calls=[],
                principal_user_id=8699080746,
                visibility="shared",
            )

            # Inspect raw DB.
            db = store._conn  # type: ignore[attr-defined]
            async with db.execute(
                "SELECT user_id, visibility, role FROM messages WHERE chat_id = ? "
                "ORDER BY id ASC",
                (42,),
            ) as cur:
                rows = await cur.fetchall()
            assert rows == [
                (6022344291, "shared", "user"),
                (None,       "shared", "assistant"),
                (6022344291, "private:6022344291", "user"),
                (None,       "private:6022344291", "assistant"),
                (8699080746, "shared", "user"),
                (None,       "shared", "assistant"),
            ], f"unexpected rows: {rows}"
            print("       ✓ messages persisted with user_id + visibility")

            # tool_calls should also carry principal_user_id.
            async with db.execute(
                "SELECT principal_user_id, name FROM tool_calls WHERE chat_id = ?",
                (42,),
            ) as cur:
                tc_rows = await cur.fetchall()
            assert tc_rows == [(6022344291, "dummy")], tc_rows
            print("       ✓ tool_calls.principal_user_id wired through")
        finally:
            await store.close()


async def _test_load_history_acl() -> None:
    print("[2/5] load_history filters by viewer_user_id...")
    from yunam.sessions import SessionStore

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        store = await SessionStore.open(db_path)
        try:
            await store.persist_turn(
                chat_id=42,
                user_text="잘 잤어?",
                assistant_text="응 잘 잤어",
                tool_calls=[],
                principal_user_id=6022344291,
                visibility="shared",
            )
            await store.persist_turn(
                chat_id=42,
                user_text="와이프한테 비밀이지만 나 토요일에 약속있어",
                assistant_text="알겠어, 너만 알게 할게",
                tool_calls=[],
                principal_user_id=6022344291,
                visibility="private:6022344291",
            )

            jaekeun_view = await store.load_history(42, viewer_user_id=6022344291)
            yoolim_view = await store.load_history(42, viewer_user_id=8699080746)

            jaekeun_text = "\n".join(m["content"] for m in jaekeun_view)
            yoolim_text = "\n".join(m["content"] for m in yoolim_view)

            assert "잘 잤어" in jaekeun_text and "잘 잤어" in yoolim_text
            assert "비밀이지만" in jaekeun_text, "jaekeun should see his own private msg"
            assert "비밀이지만" not in yoolim_text, "yoolim must not see jaekeun's private msg"
            assert "너만 알게" in jaekeun_text and "너만 알게" not in yoolim_text, (
                "assistant reply inherits the user's visibility — must be hidden from yoolim"
            )
            print("       ✓ private:jaekeun rows filtered out for yoolim's view")
            print("       ✓ shared rows visible to both")

            # NULL viewer bypasses ACL (used only by maintenance scripts).
            unfiltered = await store.load_history(42, viewer_user_id=None)
            assert any("비밀이지만" in m["content"] for m in unfiltered)
            print("       ✓ viewer_user_id=None bypasses the ACL (admin path)")
        finally:
            await store.close()


def _test_privacy_heuristic() -> None:
    print("[3/5] _detect_private_visibility heuristic...")
    from yunam.orchestrator import _detect_private_visibility

    JK = 6022344291
    cases: list[tuple[str, str]] = [
        ("이건 비밀이야", f"private:{JK}"),
        ("와이프한테 말하지 마", f"private:{JK}"),
        ("don't tell yoolim about this", f"private:{JK}"),
        ("between us", f"private:{JK}"),
        ("그냥 평범한 메시지야", "shared"),
        ("오늘 날씨 어때?", "shared"),
        ("둘만 알자, 진심으로", f"private:{JK}"),
    ]
    for text, expected in cases:
        got = _detect_private_visibility(text, JK)
        assert got == expected, f"{text!r} → {got} (expected {expected})"
    # speaker_user_id=None always returns shared (no one to be private to).
    assert _detect_private_visibility("이건 비밀이야", None) == "shared"
    print(f"       ✓ {len(cases)} heuristic cases + None-speaker passthrough")


async def _test_mark_turn_private_tool() -> None:
    print("[4/5] privacy skill's mark_turn_private tool...")
    from yunam.skills import build_privacy_skill
    from yunam.skills.base import DispatchContext, SkillRegistry

    skill = build_privacy_skill()
    registry = SkillRegistry([skill])

    _, spec = registry.lookup("mark_turn_private")
    turn_meta: dict = {"visibility": "shared", "visibility_source": "heuristic"}
    ctx = DispatchContext(
        chat_id=42,
        principal_user_id=6022344291,
        principal_name="jaekeun",
        turn_meta=turn_meta,
    )
    msg = await spec.handler({}, ctx)
    assert turn_meta["visibility"] == "private:6022344291", turn_meta
    assert turn_meta["visibility_source"] == "tool"
    assert "jaekeun" in msg, msg
    print("       ✓ tool flips turn_meta visibility from shared → private:<id>")

    # Idempotent second call.
    msg2 = await spec.handler({}, ctx)
    assert "no change" in msg2.lower(), msg2
    print("       ✓ second call is a no-op (idempotent)")

    # No speaker — soft message, no crash.
    no_speaker_meta: dict = {"visibility": "shared"}
    no_speaker_ctx = DispatchContext(
        chat_id=42, principal_user_id=None, principal_name=None,
        turn_meta=no_speaker_meta,
    )
    msg3 = await spec.handler({}, no_speaker_ctx)
    assert no_speaker_meta["visibility"] == "shared", no_speaker_meta
    assert "no current speaker" in msg3
    print("       ✓ no-speaker path returns soft message without changing meta")


def _test_principal_loader() -> None:
    print("[5/5] _load_principals JSON + legacy fallback...")
    import os
    from yunam.config import _load_principals

    saved = {k: os.environ.get(k) for k in ("YUNAM_PRINCIPALS", "TELEGRAM_ALLOWED_USER_ID")}
    try:
        # JSON form, two principals, explicit owner.
        os.environ["YUNAM_PRINCIPALS"] = (
            '[{"user_id":6022344291,"name":"jaekeun","is_owner":true},'
            '{"user_id":8699080746,"name":"yoolim"}]'
        )
        os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
        ps = _load_principals()
        assert len(ps) == 2 and ps[0].name == "jaekeun" and ps[0].is_owner
        assert ps[1].name == "yoolim" and not ps[1].is_owner
        print("       ✓ JSON form parsed; explicit owner respected")

        # JSON form without is_owner — first principal becomes owner.
        os.environ["YUNAM_PRINCIPALS"] = (
            '[{"user_id":1,"name":"a"},{"user_id":2,"name":"b"}]'
        )
        ps = _load_principals()
        assert ps[0].is_owner and not ps[1].is_owner
        print("       ✓ default owner assignment when none flagged")

        # Legacy fallback.
        os.environ.pop("YUNAM_PRINCIPALS", None)
        os.environ["TELEGRAM_ALLOWED_USER_ID"] = "999"
        ps = _load_principals()
        assert len(ps) == 1 and ps[0].user_id == 999 and ps[0].name == "jaekeun"
        assert ps[0].is_owner
        print("       ✓ legacy TELEGRAM_ALLOWED_USER_ID synthesizes one owner principal")

        # Both unset → KeyError.
        os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
        try:
            _load_principals()
        except KeyError:
            print("       ✓ both unset raises KeyError (fail-fast)")
        else:
            raise AssertionError("expected KeyError when neither env var is set")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _test_allowed_chats_loader() -> None:
    print("[6/7] _load_allowed_chats JSON + CSV...")
    import os
    from yunam.config import _load_allowed_chats

    saved = os.environ.get("YUNAM_ALLOWED_CHATS")
    try:
        os.environ.pop("YUNAM_ALLOWED_CHATS", None)
        assert _load_allowed_chats() == ()
        os.environ["YUNAM_ALLOWED_CHATS"] = ""
        assert _load_allowed_chats() == ()
        os.environ["YUNAM_ALLOWED_CHATS"] = "[-1001234567890, -1009876543210]"
        assert _load_allowed_chats() == (-1001234567890, -1009876543210)
        os.environ["YUNAM_ALLOWED_CHATS"] = "-1001234567890,-1009876543210"
        assert _load_allowed_chats() == (-1001234567890, -1009876543210)
        os.environ["YUNAM_ALLOWED_CHATS"] = "  -123 , -456  "  # whitespace tolerant
        assert _load_allowed_chats() == (-123, -456)
        # Bad input
        os.environ["YUNAM_ALLOWED_CHATS"] = "[1,2,1]"  # duplicate
        try:
            _load_allowed_chats()
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate chat_id should raise")
        os.environ["YUNAM_ALLOWED_CHATS"] = "[1, abc]"  # non-int
        try:
            _load_allowed_chats()
        except ValueError:
            pass
        else:
            raise AssertionError("non-int chat_id should raise")
        print("       ✓ JSON / CSV / whitespace / duplicate / non-int cases")
    finally:
        if saved is None:
            os.environ.pop("YUNAM_ALLOWED_CHATS", None)
        else:
            os.environ["YUNAM_ALLOWED_CHATS"] = saved


def _test_chat_authorization() -> None:
    print("[7/7] is_authorized_chat: DM always, group only if listed...")
    from yunam.auth import is_authorized_chat
    from yunam.config import Config, Principal

    # Build a minimal Config-like object — only the fields is_authorized_chat reads.
    # Real Config is a frozen dataclass; we reuse it to stay honest.
    cfg = Config(
        telegram_token="x",
        principals=(Principal(user_id=1, name="x", is_owner=True),),
        allowed_chats=(-100123, -100456),
        group_triggers=(),
        anthropic_api_key="x", voyage_api_key="x",
        vault_path=Path("/tmp"), filevault_path=Path("/tmp"), db_path=Path("/tmp/db"),
        timezone="Asia/Seoul",
        schedule_enabled=False, daily_reflection_hour=22, daily_reflection_minute=30,
        nudge_sweeper_enabled=False, nudge_sweep_interval_seconds=60.0,
        jina_api_key=None, sweettracker_api_key=None, gcal_mcp_url=None,
    )

    class _FakeChat:
        def __init__(self, chat_id: int, chat_type: str):
            self.id = chat_id
            self.type = chat_type
            self.title = "fake"

    class _FakeUpdate:
        def __init__(self, chat: _FakeChat):
            self.effective_chat = chat

    # DM: chat_id == user_id, type=private — always allowed.
    assert is_authorized_chat(_FakeUpdate(_FakeChat(1, "private")), cfg) is True
    # DM for an arbitrary user — still allowed at chat layer (principal layer
    # is the gate for unknown users).
    assert is_authorized_chat(_FakeUpdate(_FakeChat(99999, "private")), cfg) is True
    # Allowed groups.
    assert is_authorized_chat(_FakeUpdate(_FakeChat(-100123, "supergroup")), cfg) is True
    assert is_authorized_chat(_FakeUpdate(_FakeChat(-100456, "group")), cfg) is True
    # Disallowed group.
    assert is_authorized_chat(_FakeUpdate(_FakeChat(-100999, "supergroup")), cfg) is False
    # No chat (edge).
    assert is_authorized_chat(_FakeUpdate(None), cfg) is False
    # Empty allowlist → DM yes, groups no.
    cfg_dm_only = Config(
        telegram_token="x",
        principals=(Principal(user_id=1, name="x", is_owner=True),),
        allowed_chats=(),
        group_triggers=(),
        anthropic_api_key="x", voyage_api_key="x",
        vault_path=Path("/tmp"), filevault_path=Path("/tmp"), db_path=Path("/tmp/db"),
        timezone="Asia/Seoul",
        schedule_enabled=False, daily_reflection_hour=22, daily_reflection_minute=30,
        nudge_sweeper_enabled=False, nudge_sweep_interval_seconds=60.0,
        jina_api_key=None, sweettracker_api_key=None, gcal_mcp_url=None,
    )
    assert is_authorized_chat(_FakeUpdate(_FakeChat(1, "private")), cfg_dm_only) is True
    assert is_authorized_chat(_FakeUpdate(_FakeChat(-100123, "supergroup")), cfg_dm_only) is False
    print("       ✓ DM unconditionally allowed; group requires allowlist match")


def _test_group_trigger_matcher() -> None:
    print("[8/8] group trigger matcher + stripper...")
    from yunam.auth import (
        DEFAULT_GROUP_TRIGGERS,
        match_group_trigger,
        strip_group_trigger,
    )

    triggers = DEFAULT_GROUP_TRIGGERS
    # Positive matches.
    cases_match = [
        ("유남아 일정 알려줘", "유남아"),
        ("유남 잘 지냈어?", "유남"),
        ("yunam, 오늘 뭐했어?", "yunam"),
        ("Yunam help", "yunam"),       # case-insensitive on ASCII
        ("yunam!", "yunam"),
        ("유남아", "유남아"),            # bare vocative
    ]
    for text, expected in cases_match:
        got = match_group_trigger(text, triggers)
        assert got is not None, f"{text!r}: expected match, got None"
        # Defaults are case-preserved in the source list; matcher returns
        # whatever entry it found (may be the lowered or original form).
        assert got.lower() == expected.lower(), f"{text!r}: got {got!r}"
    # Negative matches (no false positives).
    cases_nomatch = [
        "yunamcorp picks up the package",  # no boundary
        "오늘 yunam 부를까?",                # not at start
        "그건 비밀이야",                      # unrelated
        "",                                  # empty
    ]
    for text in cases_nomatch:
        got = match_group_trigger(text, triggers)
        assert got is None, f"{text!r}: expected no match, got {got!r}"
    print(f"       ✓ {len(cases_match)} positive + {len(cases_nomatch)} negative cases")

    # Stripper.
    stripped_cases = [
        ("유남아 일정 알려줘", "일정 알려줘"),
        ("yunam, 오늘 뭐했어?", "오늘 뭐했어?"),
        ("yunam!", ""),
        ("유남 잘 지냈어?", "잘 지냈어?"),
        ("그냥 평범한 메시지", "그냥 평범한 메시지"),  # no trigger, unchanged
    ]
    for text, expected in stripped_cases:
        got = strip_group_trigger(text, triggers)
        assert got == expected, f"{text!r}: got {got!r}, expected {expected!r}"
    print(f"       ✓ {len(stripped_cases)} stripper cases")

    # Empty trigger list short-circuits.
    assert match_group_trigger("유남아 hi", ()) is None
    assert strip_group_trigger("유남아 hi", ()) == "유남아 hi"
    print("       ✓ empty trigger list = no-op")


async def _main() -> None:
    _setup_path()
    await _test_v6_migration_and_persist()
    await _test_load_history_acl()
    _test_privacy_heuristic()
    await _test_mark_turn_private_tool()
    _test_principal_loader()
    _test_allowed_chats_loader()
    _test_chat_authorization()
    _test_group_trigger_matcher()
    print("\nall multiuser smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
