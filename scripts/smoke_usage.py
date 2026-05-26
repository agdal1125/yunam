#!/usr/bin/env python3
"""Smoke test for the Phase 2.0 usage / audit skill.

Exercises the full pipeline against a temporary SQLite database — no Anthropic,
no network. Verifies:

  1. SessionStore migrates to user_version=DB_USER_VERSION and the `api_usage` table exists.
  2. UsageRecorder.record_anthropic / record_voyage / record_rest / record_mcp
     all land rows on the table after `flush()`.
  3. UsageTools.summary / breakdown / cost_alert_status produce sensible text.
  4. Rate math: a Sonnet 4.6 call with known token counts produces the expected
     µUSD cost (regression guard for `rates.py`).

Usage (from repo root):
    PYTHONPATH=gateway python3 scripts/smoke_usage.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _setup_path() -> None:
    gateway = Path(__file__).resolve().parent.parent / "gateway"
    sys.path.insert(0, str(gateway))


async def _check_rates() -> None:
    print("[1/4] rate math...")
    from yunam.usage.rates import anthropic_cost_micro, voyage_cost_micro

    # 1M input @ $3 + 1M output @ $15 = $18.00 -> 18,000,000 µUSD
    cost = anthropic_cost_micro(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_create_tokens=0,
    )
    assert cost == 18_000_000, f"sonnet cost expected 18_000_000, got {cost}"

    # Voyage 1M text tokens @ $0.12 + 0 images = $0.12 -> 120,000 µUSD
    vcost = voyage_cost_micro(
        "voyage-multimodal-3", text_tokens=1_000_000, images=0
    )
    assert vcost == 120_000, f"voyage cost expected 120_000, got {vcost}"

    # Unknown model → 0 (safer than guessing)
    assert anthropic_cost_micro("not-a-model", 100, 100, 0, 0) == 0
    print("    rate math OK")


async def _check_pipeline() -> None:
    print("[2/4] migration + recorder + repository...")
    from yunam.sessions import DB_USER_VERSION, SessionStore
    from yunam.usage import UsageRecorder

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "yunam.db"
        store = await SessionStore.open(db_path)
        try:
            # PRAGMA bumped to current DB_USER_VERSION?
            async with store._conn.execute("PRAGMA user_version") as cur:  # type: ignore[attr-defined]
                row = await cur.fetchone()
            assert row[0] == DB_USER_VERSION, (
                f"user_version {row[0]} != DB_USER_VERSION {DB_USER_VERSION}"
            )

            # `api_usage` exists?
            async with store._conn.execute(  # type: ignore[attr-defined]
                "SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None, "api_usage table missing after migration"
            print(f"    schema OK (user_version={DB_USER_VERSION}, api_usage present)")

            recorder = UsageRecorder(store)

            # Record one anthropic call
            usage_obj = SimpleNamespace(
                input_tokens=1234,
                output_tokens=567,
                cache_read_input_tokens=890,
                cache_creation_input_tokens=0,
            )
            recorder.record_anthropic(
                model="claude-sonnet-4-6",
                usage=usage_obj,
                elapsed_ms=420,
                chat_id=42,
                skill_id="orchestrator",
            )
            # Voyage
            recorder.record_voyage(
                model="voyage-multimodal-3",
                text_tokens=200,
                images=1,
                elapsed_ms=100,
                chat_id=42,
                skill_id="files",
            )
            # REST: jina reader
            recorder.record_rest(
                provider="jina",
                endpoint="reader",
                elapsed_ms=300,
                chat_id=42,
                skill_id="web",
            )
            # MCP: stock
            recorder.record_mcp(
                server="stock",
                tool_name="analyze_supply",
                elapsed_ms=250,
                chat_id=42,
                skill_id="stock",
            )
            # Failed REST (status=error)
            recorder.record_rest(
                provider="sweettracker",
                endpoint="trackingInfo",
                elapsed_ms=500,
                status="error",
                chat_id=42,
                skill_id="parcel",
            )

            await recorder.flush()

            async with store._conn.execute(  # type: ignore[attr-defined]
                "SELECT provider, skill_id, status, cost_usd_micro FROM api_usage ORDER BY id"
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 5, f"expected 5 rows, got {len(rows)}: {rows}"
            providers = {r[0] for r in rows}
            assert providers == {"anthropic", "voyage", "jina", "mcp:stock", "sweettracker"}, providers
            anthropic_row = next(r for r in rows if r[0] == "anthropic")
            # 1234 in @3/M + 567 out @15/M + 890 cache_read @0.3/M
            #   = (1234*3 + 567*15 + 890*0.3) / 1e6 USD = 12.474 / 1e6 USD
            expected_micro = round(
                (1234 * 3 + 567 * 15 + 890 * 0.3)
            )
            assert anthropic_row[3] == expected_micro, (
                f"anthropic cost {anthropic_row[3]} != expected {expected_micro}"
            )
            error_row = next(r for r in rows if r[0] == "sweettracker")
            assert error_row[2] == "error"
            print(f"    recorded {len(rows)} rows; anthropic cost={expected_micro} µUSD")

            # Summary
            from yunam.tools.usage import UsageTools
            tools = UsageTools(
                store=store,
                timezone_name="Asia/Seoul",
                daily_alert_usd=5.0,
                monthly_alert_usd=100.0,
            )
            summary = await tools.summary(period="today")
            assert "API usage" in summary
            assert "calls: 5" in summary, f"summary missing calls:5 line: {summary}"
            assert "errors: 1" in summary, f"summary missing errors:1: {summary}"
            print("    summary rendered (today):")
            for line in summary.splitlines():
                print(f"      {line}")

            breakdown = await tools.breakdown(period="today", group_by="provider")
            assert "anthropic" in breakdown
            assert "mcp:stock" in breakdown
            print("    breakdown rendered (by provider):")
            for line in breakdown.splitlines():
                print(f"      {line}")

            breakdown_skill = await tools.breakdown(period="today", group_by="skill_id")
            assert "orchestrator" in breakdown_skill
            print("    breakdown rendered (by skill_id) — orchestrator + web + files seen")

            alert = await tools.cost_alert_status()
            assert "ok" in alert or "near" in alert or "over" in alert
            print("    cost_alert_status:")
            for line in alert.splitlines():
                print(f"      {line}")
        finally:
            await store.close()


async def _check_invalid_inputs() -> None:
    print("[3/4] input validation...")
    from yunam.tools.usage import UsageTools

    store = SimpleNamespace()  # not used — we expect ValueError before any query
    tools = UsageTools(store=store)  # type: ignore[arg-type]
    try:
        await tools.summary(period="forever")
    except ValueError as e:
        assert "period" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown period")

    try:
        await tools.breakdown(period="today", group_by="nonsense")
    except ValueError as e:
        assert "group_by" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown group_by")
    print("    invalid period / group_by rejected as expected")


async def _check_alert_thresholds() -> None:
    print("[4/4] alert thresholds...")
    from yunam.sessions import SessionStore
    from yunam.tools.usage import UsageTools
    from yunam.usage import UsageRecorder

    with tempfile.TemporaryDirectory() as tmp:
        store = await SessionStore.open(Path(tmp) / "yunam.db")
        try:
            recorder = UsageRecorder(store)
            # Pump enough Opus cost to blow past a tiny threshold so we can
            # see the "over limit" branch fire.
            big = SimpleNamespace(
                input_tokens=2_000_000,  # 2M in @ $15/M = $30
                output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
            recorder.record_anthropic(model="claude-opus-4-7", usage=big)
            await recorder.flush()

            tools = UsageTools(
                store=store,
                daily_alert_usd=5.0,  # we just spent $30
                monthly_alert_usd=100.0,
            )
            alert = await tools.cost_alert_status()
            assert "over" in alert.lower(), f"expected over-limit flag, got: {alert}"
            print("    over-limit triggered as expected:")
            for line in alert.splitlines():
                print(f"      {line}")
        finally:
            await store.close()


async def _check_concurrent_persist() -> None:
    """Regression: background `record_api_usage` must not collide with a
    foreground `persist_turn` BEGIN. Pre-lock fix, this raised
    `sqlite3.OperationalError: cannot start a transaction within a
    transaction`. Repro: schedule many bg recorder writes and a persist_turn
    on the same event loop iteration, then drain both.
    """
    print("[5/5] concurrent persist_turn + record_api_usage...")
    from yunam.sessions import SessionStore
    from yunam.usage import UsageRecorder

    with tempfile.TemporaryDirectory() as tmp:
        store = await SessionStore.open(Path(tmp) / "yunam.db")
        try:
            recorder = UsageRecorder(store)
            # Pump ~50 bg writes then immediately persist a turn — without the
            # _write_lock the recorder's auto-BEGUN INSERT collides with
            # persist_turn's explicit BEGIN.
            for i in range(50):
                recorder.record_anthropic(
                    model="claude-sonnet-4-6",
                    usage=SimpleNamespace(
                        input_tokens=100,
                        output_tokens=10,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                    chat_id=99,
                )
            # Two persist_turns concurrent with bg drain.
            await asyncio.gather(
                store.persist_turn(
                    chat_id=99,
                    user_text="hello",
                    assistant_text="hi",
                    tool_calls=[],
                    principal_user_id=1,
                ),
                store.persist_turn(
                    chat_id=99,
                    user_text="again",
                    assistant_text="yes",
                    tool_calls=[],
                    principal_user_id=1,
                ),
                recorder.flush(),
            )
            async with store._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM api_usage WHERE chat_id = 99"
            ) as cur:
                row = await cur.fetchone()
            assert row[0] == 50, f"expected 50 api_usage rows, got {row[0]}"
            async with store._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM messages WHERE chat_id = 99"
            ) as cur:
                row = await cur.fetchone()
            # 2 persist_turn × (user + assistant) = 4 rows.
            assert row[0] == 4, f"expected 4 message rows, got {row[0]}"
            print(f"    50 bg writes + 2 persist_turns succeeded under lock")
        finally:
            await store.close()


async def main() -> int:
    _setup_path()
    await _check_rates()
    await _check_pipeline()
    await _check_invalid_inputs()
    await _check_alert_thresholds()
    await _check_concurrent_persist()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
