# Trade Journal and P&L Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Journal page to FreqUI: a month P&L calendar for both bots plus a per-trade review
(entry checks, exit, fees, mini chart, notes and tags saved on the bot).

**Architecture:** The freqtrade fork gets two journal endpoints on top of the existing per-trade
custom data table and a strategy helper that saves an `entry_snapshot` when an entry fills. FreqUI
computes the calendar from closed trades it already loads, groups them in the user's timezone, and
reads snapshots and notes through the bot API.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / pytest (freqtrade); Vue 3 / Pinia / Nuxt UI /
date-fns-tz / vue-echarts / vitest (FreqUI).

**Spec:** `docs/superpowers/specs/2026-10-30-trade-journal-design.md`

**Repos and branches:**
- freqtrade: `/Users/omid/Desktop/Projects/freqtrade`, branch `feat/trade-journal` (exists).
- Strategies: `/Users/omid/Desktop/Projects/freqtrade/user_data/strategies/` (gitignored, never committed).
- FreqUI: `/Users/omid/Desktop/Projects/frequi`, branch `feat/modern-login`.

**Change from the spec:** the snapshot helper lives in the fork at `freqtrade/strategy/journal.py`
(tracked and unit-tested) instead of `user_data/strategies/journal_helpers.py`. Strategies import
it with `from freqtrade.strategy.journal import save_entry_snapshot`. It ships inside the image like
the rest of `freqtrade/`.

## Global Constraints

- Journal custom data keys: `journal_note` (str) and `journal_tags` (list[str]). The journal endpoints write no other key.
- Note at most 2000 characters (Python `len`, code points). At most 10 tags, each 1–30 characters after trimming. Duplicate tags are dropped case-insensitively, keeping the first spelling. Breaking a limit → HTTP 400. Unknown trade → HTTP 404.
- An empty note deletes `journal_note`; an empty tag list deletes `journal_tags`.
- Snapshot key `entry_snapshot`, shape `{"v": 1, "candle": ISO str, "enter_tag": str|null, "checks": {name: {"met": bool, "gap": float|null}}, "values": {col: float|null}}`. NaN → null.
- The snapshot is saved only in live and dry-run run modes, only on a trade's first filled entry, and every failure is caught and logged as a warning.
- The Bitpin backtest must still give 124 trades / +23.23%: `freqtrade backtesting -c user_data/config_bitpin.json --strategy BitpinBreakout --timerange 20250701-20261026 --datadir user_data/data/bitpin --dry-run-wallet 100`.
- UI copy (en): "No snapshot (trade opened before journaling)", "Chart not available", "Saved ✓", "Not saved – retry". Every new key exists in `locales/en.ts` and `locales/fa.ts`; sentence case, no all-caps.
- Calendar days use `settingsStore.timezone`. Weeks start on Saturday when `settingsStore.locale === 'fa'`, otherwise Monday.
- Note saves are debounced 800 ms.
- Nothing under 12px; all text passes WCAG AA in both themes. Touch targets use `pointer-coarse:min-h-11` / `size-11`.
- Live bots are never touched during development. Local testing uses dry-run bots only (ports 8095/8096, scratchpad override configs).
- FreqUI build: `npx --yes pnpm@11.4.0 build`; tests: `npx --yes pnpm@11.4.0 vitest run`.

## Review Focus

1. **Open trades in the trade list.** `/trades` returns open trades too; they must never count on the calendar or in the journal list. → test in Task 4 (`groupByDay` skips open trades).
2. **Empty month / bot with no closed trades.** The calendar renders a blank grid and the summary shows dashes, never `NaN` or `Infinity`. → test in Task 4 (`monthSummary([])`).
3. **Persian notes and look-alike tags.** A 2000-character Persian note is accepted; "FOMO" and "fomo " collapse to one tag "FOMO". → tests in Task 1 (`normalize_journal`).
4. **Closing the slide-over within 800 ms of typing.** The pending save is flushed on unmount, not dropped. → test in Task 6 (flush on unmount).
5. **No analyzed candle at or before the open date** (bot just restarted, empty dataframe). The snapshot is skipped with a warning and the trade is unaffected. → test in Task 2.

---

### Task 1: Journal endpoints (freqtrade)

**Files:**
- Create: `freqtrade/rpc/journal.py`
- Modify: `freqtrade/rpc/rpc.py` (two methods next to `_rpc_list_custom_data`, ~line 1299)
- Modify: `freqtrade/rpc/api_server/api_schemas.py` (after `ListCustomData`, ~line 870)
- Modify: `freqtrade/rpc/api_server/api_trading.py` (after `list_custom_data`, ~line 231)
- Test: `tests/rpc/test_journal.py` (new), `tests/rpc/test_rpc_apiserver.py`

**Interfaces:**
- Produces:
  - `freqtrade/rpc/journal.py`: `JOURNAL_NOTE_KEY = "journal_note"`, `JOURNAL_TAGS_KEY = "journal_tags"`, `MAX_NOTE_LEN = 2000`, `MAX_TAGS = 10`, `MAX_TAG_LEN = 30`, `normalize_journal(note: str, tags: list[str]) -> tuple[str, list[str]]` (strips the note's surrounding whitespace, trims and dedupes tags, raises `ValueError` with a readable message on a limit).
  - `RPC._rpc_set_journal(self, trade_id: int, note: str, tags: list[str]) -> dict` → `{"trade_id", "note", "tags", "updated_at"}`; raises `RPCException` when the trade is missing, `ValueError` on limits. Decorated with `@custom_data_rpc_wrapper`.
  - `RPC._rpc_list_journal(self) -> dict[int, dict]` → `{trade_id: {"note": str, "tags": list[str], "updated_at": datetime|None}}`, only trades with at least one journal key; a missing key gives `""` / `[]`. Decorated with `@custom_data_rpc_wrapper`.
  - Schemas `JournalUpdate(note: str = "", tags: list[str] = [])`, `JournalEntry(trade_id: int, note: str, tags: list[str], updated_at: datetime | None = None)`.
  - Routes `PUT /trades/{tradeid}/journal` (response `JournalEntry`; `ValueError` → 400, `RPCException` → 404) and `GET /trades/journal` (response `dict[int, JournalEntry]`, keyed by trade id, `trade_id` also inside each entry).

- [ ] **Step 1: Write failing unit tests** in `tests/rpc/test_journal.py`:

```python
def test_normalize_journal_trims_and_dedupes():
    assert normalize_journal("  hi  ", ["FOMO", "fomo ", " late", ""]) == ("hi", ["FOMO", "late"])

def test_normalize_journal_accepts_persian_at_limit():
    note = "ی" * 2000
    assert normalize_journal(note, []) == (note, [])

@pytest.mark.parametrize("note,tags", [
    ("x" * 2001, []),
    ("", [f"t{i}" for i in range(11)]),
    ("", ["x" * 31]),
])
def test_normalize_journal_limits(note, tags):
    with pytest.raises(ValueError):
        normalize_journal(note, tags)
```

- [ ] **Step 2: Run** `pytest tests/rpc/test_journal.py -v`. Expected: FAIL (`ModuleNotFoundError: freqtrade.rpc.journal`).

- [ ] **Step 3: Implement `freqtrade/rpc/journal.py`** with the constants and `normalize_journal` above. Count 11 tags after dedupe and trimming, so 11 distinct tags fail.

- [ ] **Step 4: Run** the same command. Expected: PASS.

- [ ] **Step 5: Write failing API tests** in `tests/rpc/test_rpc_apiserver.py`. Use the setup of `test_api_custom_data_single_trade` (`Trade.reset_trades()`, `CustomDataWrapper.reset_custom_data()`, `create_mock_trades_usdt(fee, use_db=True)`). Add a `client_put(client, url, data)` helper shaped like `client_post`.
  - `test_api_journal_save_and_list`: PUT `/trades/1/journal` `{"note": "late entry", "tags": ["fomo"]}` → 200, body `note == "late entry"`, `tags == ["fomo"]`. Then GET `/trades/journal` → `{"1": {...note "late entry", tags ["fomo"]...}}`, and `trade1.get_custom_data("journal_note") == "late entry"`.
  - `test_api_journal_empty_deletes`: after a save, PUT `{"note": "", "tags": []}` → 200; `trade1.get_custom_data_entry("journal_note") is None`, `get_custom_data_entry("journal_tags") is None`, and GET `/trades/journal` → `{}`.
  - `test_api_journal_limits`: PUT with `"x" * 2001` → 400.
  - `test_api_journal_unknown_trade`: PUT `/trades/999/journal` → 404.
  - `test_api_journal_keeps_strategy_data`: `trade1.set_custom_data("entry_snapshot", {"v": 1})`, then PUT a note → `trade1.get_custom_data("entry_snapshot") == {"v": 1}`.

- [ ] **Step 6: Run** `pytest tests/rpc/test_rpc_apiserver.py -k journal -v`. Expected: FAIL (404/405 on the new routes).

- [ ] **Step 7: Implement** the schemas, the two RPC methods and the two routes. `_rpc_set_journal` looks up the trade with `Trade.get_trades(trade_filter=[Trade.id == trade_id]).first()`, calls `normalize_journal`, then `trade.set_custom_data` or `trade.delete_custom_data(key)` per key. `_rpc_list_journal` selects `_CustomData` rows whose `cd_key` is one of the two keys and converts values with `CustomDataWrapper._convert_custom_data`. `updated_at` is the newer of the two rows' `updated_at or created_at`.

- [ ] **Step 8: Run** `pytest tests/rpc/test_journal.py tests/rpc/test_rpc_apiserver.py -v` and `ruff check freqtrade/rpc tests/rpc`. Expected: all PASS, no lint errors.

- [ ] **Step 9: Commit**

```bash
git add freqtrade/rpc/journal.py freqtrade/rpc/rpc.py freqtrade/rpc/api_server/api_schemas.py freqtrade/rpc/api_server/api_trading.py tests/rpc/test_journal.py tests/rpc/test_rpc_apiserver.py
git commit -m "API: add trade journal endpoints (notes and tags in custom data)"
```

### Task 2: Entry snapshot helper (freqtrade) and strategy hooks

**Files:**
- Create: `freqtrade/strategy/journal.py`
- Test: `tests/strategy/test_journal.py`
- Modify (not committed): `user_data/strategies/BitpinBreakout.py`, `user_data/strategies/NobitexScalp.py`

**Interfaces:**
- Produces:
  - `build_entry_snapshot(row: pd.Series, enter_tag: str | None, value_cols: list[str]) -> dict` (the Global Constraints shape; `candle` = `row["date"].isoformat()`; checks from every `entry_check__<name>` column, gap from `entry_gap__<name>` when present; missing value columns are left out).
  - `save_entry_snapshot(strategy: IStrategy, pair: str, trade: Trade, order: Order, value_cols: list[str]) -> None`. It returns early unless `strategy.dp.runmode` is `RunMode.LIVE` or `RunMode.DRY_RUN`, `order.ft_order_side == trade.entry_side`, `trade.nr_of_successful_entries == 1`, and `trade.get_custom_data("entry_snapshot") is None`. It takes the last row of `strategy.dp.get_analyzed_dataframe(pair, strategy.timeframe)[0]` with `date <= trade.open_date_utc`, calls `trade.set_custom_data("entry_snapshot", ...)`, and wraps everything in `try/except Exception` → `logger.warning`.

- [ ] **Step 1: Write failing tests** in `tests/strategy/test_journal.py`:
  - `test_build_entry_snapshot`: a row with `date=2026-10-30 12:00 UTC`, `entry_check__breakout=1`, `entry_gap__breakout=0.0`, `entry_check__btc_trend=0`, `entry_gap__btc_trend=nan`, `close=1.5`, `rsi=nan`, and `value_cols=["close", "rsi", "missing"]` → `{"v": 1, "candle": "2026-10-30T12:00:00+00:00", "enter_tag": "breakout", "checks": {"breakout": {"met": True, "gap": 0.0}, "btc_trend": {"met": False, "gap": None}}, "values": {"close": 1.5, "rsi": None}}`.
  - `test_save_entry_snapshot_no_candle`: the mocked dataframe's only row is after `trade.open_date_utc` → no custom data is set, `log_has_re("entry snapshot", caplog)` is true, and no exception is raised.
  - `test_save_entry_snapshot_skips_backtest`: runmode `BACKTEST` → `trade.set_custom_data` is not called.

- [ ] **Step 2: Run** `pytest tests/strategy/test_journal.py -v`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `freqtrade/strategy/journal.py`.

- [ ] **Step 4: Run** the same command plus `ruff check freqtrade/strategy/journal.py`. Expected: PASS.

- [ ] **Step 5: Add the hook to both strategies:** `def order_filled(self, pair, trade, order, current_time, **kwargs) -> None: save_entry_snapshot(self, pair, trade, order, VALUES)`, with `VALUES = ["close", "trend_ema", "highest_close", "volume_avg"]` for BitpinBreakout and `["close", "rsi", "bb_lower", "bb_mid"]` for NobitexScalp. If NobitexScalp already defines `order_filled`, call the helper from it.

- [ ] **Step 6: Verify signals did not change.** Run the backtest command from Global Constraints. Expected: 124 trades, total profit 23.23%.

- [ ] **Step 7: Verify in dry-run.** Start a local dry-run BitpinBreakout bot with a scratchpad override (dry_run true, blank keys, own sqlite, API 127.0.0.1:8095, `force_entry_enable: true`), force an entry with `POST /api/v1/forceenter`, then `GET /api/v1/trades/<id>/custom-data?key=entry_snapshot`. Expected: one entry whose value has `v == 1` and the four BitpinBreakout checks. Keep this bot for Task 7.

- [ ] **Step 8: Commit** (the strategies stay uncommitted)

```bash
git add freqtrade/strategy/journal.py tests/strategy/test_journal.py
git commit -m "Strategy: add entry snapshot helper for the trade journal"
```

### Task 3: FreqUI types and store methods

**Files:**
- Create: `src/types/journal.ts` (export from `src/types/index.ts`)
- Modify: `src/stores/ftbot.ts`, `src/stores/ftbotwrapper.ts`

**Interfaces:**
- Produces:
  - Types: `JournalEntry { trade_id: number; note: string; tags: string[]; updated_at?: string | null }`, `JournalMap = Record<number, JournalEntry>`, `EntrySnapshot { v: 1; candle: string; enter_tag: string | null; checks: Record<string, { met: boolean; gap: number | null }>; values: Record<string, number | null> }`.
  - Per-bot state: `journal: JournalMap` (default `{}`), `journalSupported: boolean | null` (default `null`).
  - Per-bot actions: `getJournal(): Promise<void>` (404 → `journalSupported = false`, `journal = {}`, no alert; success → `true`); `saveJournal(tradeId: number, note: string, tags: string[]): Promise<JournalEntry>` (updates `journal`, deletes the entry when both are empty, rejects on error without an alert); `getEntrySnapshot(tradeId: number): Promise<EntrySnapshot | null>` (uses `/trades/{id}/custom-data?key=entry_snapshot`, null when absent); `fetchPairCandles(payload: PairCandlePayload): Promise<PairHistory>` (same request as `getPairCandles`, no state writes).
  - Wrapper getter: `allJournalSelectedBots: Record<string, JournalMap>` keyed by botId.
  - `getJournal()` is pushed into the slow-refresh `updates` list right after `getTrades()` (ftbot.ts ~line 250).

- [ ] **Step 1: Implement** the types, state, actions and getter above.
- [ ] **Step 2: Run** `npx --yes pnpm@11.4.0 typecheck`. Expected: no new errors.
- [ ] **Step 3: Commit** `git commit -m "Store: journal, entry snapshot and candle fetch calls"`.

### Task 4: Journal maths (pure functions)

**Files:**
- Create: `src/utils/journal.ts`
- Test: `tests/unit/journal.spec.ts`

**Interfaces:**
- Produces:
  - `interface DayStats { date: string; profitAbs: number; tradeCount: number; wins: number; grossWin: number; grossLoss: number; byBot: Record<string, number> }`
  - `dayKey(ts: number, timeZone: string): string` (`yyyy-MM-dd` via `formatInTimeZone`).
  - `groupByDay(trades: ClosedTrade[], timeZone: string, botIds?: string[]): Record<string, DayStats>` (skips `is_open` trades and trades without `close_timestamp`; profit is `profit_abs ?? 0`; a win is `profit_abs > 0`; `botIds` filters when given).
  - `monthSummary(days: DayStats[]): { profitAbs: number; tradeCount: number; winRate: number | null; bestDay: DayStats | null; worstDay: DayStats | null; profitFactor: number | null }` (profitFactor = grossWin / |grossLoss|, null when grossLoss is 0; winRate null when tradeCount is 0).
  - `monthGrid(year: number, month: number, weekStartsOn: 0 | 1 | 6): (string | null)[]` (month is 1–12; leading and trailing `null` padding; length is a multiple of 7).
  - `feeSplit(trade: ClosedTrade): { gross: number; fees: number; net: number; feeShare: number | null }` (fees = fee_open_cost + fee_close_cost, missing → 0; net = profit_abs; gross = net + fees; feeShare = fees / gross when gross > 0, else null).

- [ ] **Step 1: Write failing tests** in `tests/unit/journal.spec.ts`:
  - `dayKey(Date.UTC(2026, 9, 29, 21, 0), 'UTC') === '2026-10-29'` and `dayKey(same, 'Asia/Tehran') === '2026-10-30'`.
  - `groupByDay`: two closed trades on bots `a` (+2) and `b` (−0.5) on the same day plus one open trade → one day with `profitAbs 1.5`, `tradeCount 2`, `wins 1`, `byBot {a: 2, b: -0.5}`. With `botIds ['a']` → `profitAbs 2`.
  - `monthSummary([])` → `{profitAbs: 0, tradeCount: 0, winRate: null, bestDay: null, worstDay: null, profitFactor: null}`.
  - `monthSummary` of days +3 (grossWin 3) and −1 (grossLoss −1), 3 trades, 2 wins → `profitAbs 2`, `winRate 2/3`, `profitFactor 3`, best day the +3 day, worst day the −1 day.
  - `monthGrid(2026, 10, 6)`: Oct 1 2026 is a Thursday → 5 leading nulls, `grid[5] === '2026-10-01'`, `grid.length % 7 === 0`. `monthGrid(2026, 10, 1)` → 3 leading nulls.
  - `feeSplit({profit_abs: 0.3, fee_open_cost: 0.13, fee_close_cost: 0.13})` → gross 0.56, fees 0.26, net 0.3, feeShare ≈ 0.4643. Losing trade with gross ≤ 0 → feeShare null.
- [ ] **Step 2: Run** `npx --yes pnpm@11.4.0 vitest run tests/unit/journal.spec.ts`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `src/utils/journal.ts`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "Journal: day grouping, month summary, calendar grid and fee split"`.

### Task 5: Journal page (calendar and trade list)

**Files:**
- Create: `src/views/JournalView.vue`, `src/components/journal/PnlCalendar.vue`, `src/components/journal/JournalTradeList.vue`, `src/composables/useJournal.ts`
- Modify: `src/router/index.ts` (route `{ path: '/journal', name: 'Freqtrade Journal', meta: { title: 'nav.journal' }, component: () => import('@/views/JournalView.vue') }`), `src/composables/useAppNav.ts` (entry after Dashboard: `nav.journal`, `/journal`, `i-lucide-notebook-pen`, description `home.journalHelp`, `visible: !botStore.canRunBacktest`), `src/i18n/locales/en.ts`, `src/i18n/locales/fa.ts`

**Interfaces:**
- Consumes: Task 3 store members, Task 4 functions.
- Produces: `useJournal()` returning `{ month: Ref<{year: number; month: number}>, botFilter: Ref<string | 'all'>, selectedDay: Ref<string | null>, tagFilter: Ref<string | null>, notesOnly: Ref<boolean>, days: ComputedRef<Record<string, DayStats>>, summary: ComputedRef<ReturnType<typeof monthSummary>>, trades: ComputedRef<ClosedTrade[]>, unreachableBots: ComputedRef<string[]> }`. `trades` is the closed trades after every filter, newest first. The day filter uses `dayKey(close_timestamp, settingsStore.timezone)`.

- [ ] **Step 1: Implement** `useJournal`, then `PnlCalendar` (month buttons, bot switcher of "All bots" plus each selected bot, the grid from `monthGrid` with the locale's week start, cells showing `formatPrice` profit with `text-profit`/`text-loss`, a tint whose opacity scales with |profit| / max |profit| in the month, the trade count, the summary row, and dots plus a tapped-day total under `sm`). Then `JournalTradeList` (columns bot, pair, close time via `fmtTime`, duration, profit % and USDT, exit reason, tags, note icon; filter chips; a row click sets that bot's `tradeDetail` the way TradeList does). Then `JournalView` with calendar on top and list below. A warning chip names each bot in `unreachableBots`.
- [ ] **Step 2: Add i18n keys** under `nav.journal`, `home.journalHelp` and a new `journal` group (calendar, summary labels, filters, empty states) in en and fa.
- [ ] **Step 3: Run** `npx --yes pnpm@11.4.0 vitest run` (the i18n spec included) and the typecheck. Expected: PASS, no new type errors.
- [ ] **Step 4: Check in the browser** against the two dry-run bots from Task 7's recipe (or start them now): the calendar shows the forced trades on the right day in UTC and in Asia/Tehran, clicking a day filters the list, "All bots" sums both, and a 375px width has no horizontal scroll.
- [ ] **Step 5: Commit** `git commit -m "Journal page: P&L calendar and closed trades list"`.

### Task 6: Trade review section (slide-over)

**Files:**
- Create: `src/components/journal/TradeReview.vue`, `src/components/journal/TradeNotes.vue`, `src/components/journal/MiniTradeChart.vue`
- Modify: `src/composables/useNextEntry.ts` (export `checkLabel(strategy: string, key: string): string` and `checkHelp(strategy: string, key: string): string | null`, built on `knownFor` and `label`), `src/components/ftbot/TradeDetailSlideover.vue` (render `<TradeReview :trade :bot>` above `TradeDetail` when `!trade.is_open`), en/fa locales
- Test: `tests/unit/tradeNotes.spec.ts`

**Interfaces:**
- Consumes: Task 3 `getEntrySnapshot`, `saveJournal`, `fetchPairCandles`, `journal`, `journalSupported`; Task 4 `feeSplit`.
- Produces: `TradeNotes` props `{ trade: ClosedTrade; bot: <bot store> }`, emitting nothing; it reads and writes through `bot.saveJournal`.

- [ ] **Step 1: Write a failing test** in `tests/unit/tradeNotes.spec.ts` (`// @vitest-environment happy-dom`; mount with `@vue/test-utils`, Nuxt UI inputs stubbed with plain `textarea`/`input` via `global.stubs`, a stub bot whose `saveJournal` is a `vi.fn()` resolving an entry, and fake timers):
  - typing a note and advancing 800 ms calls `saveJournal(1, 'late', [])` once;
  - typing and unmounting after 200 ms calls `saveJournal` once (flush on unmount);
  - a rejected save shows the text "Not saved – retry" and keeps the typed note in the textarea.
- [ ] **Step 2: Run** `npx --yes pnpm@11.4.0 vitest run tests/unit/tradeNotes.spec.ts`. Expected: FAIL (component missing).
- [ ] **Step 3: Implement `TradeNotes.vue`** with `useDebounceFn(save, 800)`, a flush in `onBeforeUnmount`, a status of `idle | saving | saved | error`, `UTextarea` with `maxlength` 2000, a tag input with `maxlength` 30 that stops at 10 tags, and suggestions from the tags in `bot.journal`. It is hidden when `bot.journalSupported === false`.
- [ ] **Step 4: Run** the same test. Expected: PASS.
- [ ] **Step 5: Implement `TradeReview.vue`**: "Why it entered" (snapshot checks with ✓/✗, `checkLabel`/`checkHelp` using `bot.botState.strategy`, gap via `fmtGap`, values list; no snapshot → enter tag plus the no-snapshot text), "Why it exited" (exit reason mapped to plain words with a fallback to the raw reason, open rate, close rate, `stop_loss_abs`), "Fees" (`feeSplit`, share as a percentage), `MiniTradeChart`, `TradeNotes`. Cache snapshots and candles per `${botId}:${trade_id}` in module scope.
- [ ] **Step 6: Implement `MiniTradeChart.vue`**: `fetchPairCandles({ pair, timeframe: bot.botState.timeframe, limit: 1000 })`. Slice from 10 candles before the entry candle to 10 after the exit candle and draw a vue-echarts candlestick, 160px tall, with entry and exit markPoints. If the entry candle is not in the data, show "Chart not available" and a button that sets `bot.selectedPair = trade.pair` and routes to `/graph`.
- [ ] **Step 7: Run** the full vitest suite and the typecheck. Expected: PASS.
- [ ] **Step 8: Commit** `git commit -m "Trade review: entry checks, exit, fees, mini chart and notes"`.

### Task 7: End-to-end check, build and install

**Files:**
- Modify: `freqtrade/rpc/api_server/ui/installed/` (build output), `.uiversion` = `3.0.2-redesign-24`

- [ ] **Step 1: Two local dry-run bots** on 8095 (BitpinBreakout, config_bitpin.json) and 8096 (NobitexScalp, config_nobitex.json), each with a scratchpad override: dry_run true, blank keys, own sqlite, generated `uitest` login, `force_entry_enable: true`, CORS_origins listing both ports. Both run the freqtrade checkout on `feat/trade-journal`.
- [ ] **Step 2: Create trades.** On each bot, force two entries, then force-exit them (`POST /forceexit`). Expected: 4 closed trades, each with an `entry_snapshot`.
- [ ] **Step 3: Check in the browser pane** (dev server with `FT_API_URL=http://127.0.0.1:8095`): both bots are added; the calendar sums both and the per-bot switch works; the review shows checks, exit, fees and the mini chart; a note and tags save ("Saved ✓"), survive a full reload, and show in the list and the tag filter; Persian locale shows a Saturday week start and RTL layout.
- [ ] **Step 4: Check the fallbacks.** Stop bot 8096 → its warning chip appears and the page still renders. Point a third bot entry at a stock-freqtrade dry-run (or mock a 404 on `/trades/journal`) → notes hidden for that bot.
- [ ] **Step 5: Run the contrast audit** (in-page WCAG script) on the Journal page and the review, in light and dark. Expected: every text node ≥ 4.5:1 (≥ 3:1 for large text).
- [ ] **Step 6: Build and install**: `npx --yes pnpm@11.4.0 build`; copy `dist/` into `freqtrade/rpc/api_server/ui/installed` keeping `.gitkeep`; `printf '3.0.2-redesign-24' > .uiversion`. Stop the dry-run bots and the dev server.
- [ ] **Step 7: Commit and push FreqUI** (`git push origin feat/modern-login`).

### Task 8: Deploy (the user approves or runs it)

- [ ] **Step 1: Merge** `feat/trade-journal` into `develop` (fast-forward) after the whole-branch review passes, and push both to `origin`. Never push to upstream.
- [ ] **Step 2: Copy the strategy edits** in `user_data/strategies/` along, ready for deploy.sh to sync.
- [ ] **Step 3: Ask the user** to run (or approve) the full deploy, `deploy/bitpin/deploy.sh` without `SKIP_BUILD`. It builds the amd64 image, uploads it (15–30 min, report progress in MB and %), and recreates both containers, ideally when neither bot has an open trade.
- [ ] **Step 4: Verify live**: `docker ps` healthy for both containers; `GET /api/v1/trades/journal` returns `{}` (200) on both bots; no ERROR lines in either log since the restart; the Journal page loads at https://freq.laragirls.ir (through Claude in Chrome, since the gate cookie lives there).
