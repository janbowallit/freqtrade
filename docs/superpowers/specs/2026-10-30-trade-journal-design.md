# Trade journal and daily P&L calendar

Date: 2026-10-30. Status: design approved in chat, awaiting spec review.

## Goal

Both live bots (Bitpin `BitpinBreakout` 1h, Nobitex `NobitexScalp` 15m, both staked in USDT)
trade real money, and today the only way to understand a closed trade is to read the raw trade
detail. This adds:

1. A **trade journal**: for every closed trade, why it entered, why it exited, what fees cost,
   a mini chart, and notes and tags the user writes, saved on the bot so every device sees them.
2. A **daily P&L calendar**: a month grid of net profit per day, both bots summed, with a per-bot
   filter. Clicking a day filters the journal to that day's trades.

Success: the user can open the Journal page on desktop or phone, see each day's result at a
glance, and for any trade closed after this ships, see which entry checks were met and why it
exited, and leave a note that is still there on another device.

Out of scope: alerts, Toman values, editing strategy parameters, exporting the journal.

## Where the work lives

| Part | Repo / path | Notes |
| --- | --- | --- |
| Journal API | this repo, `freqtrade/rpc/api_server/api_trading.py` (+ `rpc.py`, `api_schemas.py`) | Fork-only, on a feature branch |
| Entry snapshot | `user_data/strategies/` (gitignored, not in git) | Both strategies |
| Journal page | `~/Desktop/Projects/frequi`, branch `feat/modern-login` | Built into `freqtrade/rpc/api_server/ui/installed` |

## 1. Bot side

### Journal endpoints

Both use the normal API login, like every other route.

- `PUT /api/v1/trades/{trade_id}/journal`, body `{"note": str, "tags": [str]}`.
  - Stores custom data keys `journal_note` (string) and `journal_tags` (list) through
    `Trade.set_custom_data`. Works for open and closed trades.
  - An empty note deletes `journal_note`; an empty tag list deletes `journal_tags`.
  - Limits: note at most 2000 characters; at most 10 tags, each 1–30 characters after trimming;
    duplicate tags are dropped. Breaking a limit returns 400. An unknown trade returns 404.
  - Returns the stored `{trade_id, note, tags, updated_at}`.
  - It can only write those two keys, so strategy data such as `entry_snapshot` is never touched.
- `GET /api/v1/trades/journal` returns `{"<trade_id>": {"note", "tags", "updated_at"}}` for every
  trade that has at least one journal key. One call, so the UI list can show tags and filter
  without a request per trade.

The existing `GET /trades/{trade_id}/custom-data?key=entry_snapshot` already serves the snapshot.

The UI detects support by calling `GET /trades/journal`: a 404 means the bot has no journal
endpoints, and the UI hides notes and tag filters for that bot.

### Entry snapshot

A strategy `order_filled` callback runs when an order fills. When the order is the trade's first
filled entry, it reads the analyzed dataframe (`self.dp.get_analyzed_dataframe(pair, timeframe)`),
takes the last candle at or before the trade's open date, and stores custom data `entry_snapshot`:

```json
{
  "v": 1,
  "candle": "2026-10-30T12:00:00+00:00",
  "enter_tag": "breakout",
  "checks": {"breakout": {"met": true, "gap": 0.0}, "btc_trend": {"met": true, "gap": null}},
  "values": {"close": 1.234, "rsi": 28.1}
}
```

- `checks` comes from the `entry_check__<name>` / `entry_gap__<name>` columns that both
  strategies already publish for the Next entry panel. `gap` is null when there is no gap column
  or the value is NaN.
- `values` is a short per-strategy list of indicator columns (for example RSI, the lower Bollinger
  band and close for NobitexScalp). NaN becomes null.
- The shared code goes in `user_data/strategies/journal_helpers.py`. If freqtrade's strategy
  loader can't import a sibling module, the helper becomes a small method copied into each
  strategy. The implementation plan checks this first.
- Any failure only logs a warning. Saving the snapshot must never block, delay or change a trade.
- Trades opened before this ships have no snapshot.
- Both strategies' signals must not change. The Bitpin backtest must still give 124 trades /
  +23.23% (period and pairs as in `strategyReference.ts`).

## 2. UI (FreqUI)

### Journal page

The route is `/journal`, in the sidebar after Dashboard (`useAppNav.ts`). Every text is in
`locales/en.ts` and `locales/fa.ts`.

**Month calendar (top)**

- A seven-column month grid with previous and next month buttons. A cell shows the net profit in
  USDT (profit/loss colour, cell tint scaled to the size of the profit) and the trade count. Days
  with no trades are blank.
- Above it, a bot switcher: All bots (sum) or one bot. It uses the selected bots
  (`allClosedTradesSelectedBots`).
- A trade belongs to the day of its close time in `settingsStore.timezone`. Weeks start on
  Saturday in Persian and on Monday in English.
- Month summary: total net profit, number of trades, win rate, best day, worst day, and profit
  factor (gross wins / gross losses; shown as a dash when there are no losses).
- Phones: cells shrink to the day number with a coloured dot. Tapping a day shows its total and
  filters the list.

**Closed trades list (below)**

- Filters: the selected day (shown as a chip that clears it), bot, tag, and "has notes".
- Columns: bot, pair, close time, duration, profit % and USDT, exit reason, tags, note icon.
- Clicking a row opens the existing global `TradeDetailSlideover`.

### Review section in the trade slide-over

It shows for every closed trade, wherever the slide-over is opened.

- **Why it entered:** the snapshot's checks as a ✓/✗ list with gaps and values, labelled with the
  same `KNOWN` / `STRATEGY_KNOWN` names and help texts as the Next entry panel. Without a snapshot
  it shows the enter tag and "No snapshot (trade opened before journaling)".
- **Why it exited:** the exit reason in plain words (ROI, stoploss, trailing stop, time stop, exit
  signal, force exit), with the entry price, the exit price and the stoploss in force at exit
  (`stop_loss_abs`).
- **Fees:** gross profit, fees paid (open + close fee cost), net profit, and fees as a share of
  the gross profit.
- **Mini chart:** candles from 10 before the entry to 10 after the exit, from `/pair_candles`,
  with entry and exit markers. Older trades are often outside the bot's candle window, so they
  get "Chart not available" and a link to the Chart page for that pair.
- **Notes:** a text box and a tag input with suggestions from tags already used on that bot.
  Saves 800 ms after typing stops and shows "Saved ✓" or "Not saved – retry". Hidden when the
  bot has no journal endpoints.

## 3. Data flow, errors, testing, shipping

### Data flow

- Page load: per selected bot, the closed trades from the existing `getTrades` paging and
  `GET /trades/journal`, each once. A new `useJournal` composable groups trades by day and
  computes the summaries.
- Opening a trade: `entry_snapshot` and the chart candles, cached per bot and trade id.
- Editing notes: debounced `PUT`, then the cached journal map updates so the list's tags and note
  icon change at once.
- The page refreshes when the bot store's regular refresh runs, so new closed trades appear.

### Errors

- A bot without journal endpoints: notes and tag filters are hidden for that bot only.
- An unreachable bot: its trades are missing and a warning chip names it. The page still renders.
- A failed save keeps the text in the box, shows "Not saved – retry" and retries on the next
  edit. Text is never lost silently.
- A missing or malformed snapshot shows the "No snapshot" text, not an error.

### Testing

- freqtrade (`tests/rpc/test_rpc_apiserver.py`): save, update, delete by empty value, limits
  (400), unknown trade (404), only `journal_*` keys written, `GET /trades/journal` contents.
- Snapshot helper: a unit test with a fake dataframe (checks, gaps, NaN to null, candle choice),
  and a dry-run forced entry that produces a snapshot.
- FreqUI unit tests: day grouping across timezones (for example 23:30 UTC lands on the next day
  in Asia/Tehran), the all-bots sum and per-bot filter, the month summary maths, and the fee split.
  The existing i18n test covers the new texts.
- End to end: two local dry-run bots on 8095/8096 (the scratchpad recipe), forced entries and
  exits to create trades, then the calendar, day filter, review and notes (saved, reloaded,
  shown after reopening) in the browser pane, plus a WCAG AA contrast check in both themes.

### Shipping order

1. freqtrade fork changes on a feature branch, tested locally in dry-run.
2. Strategy changes; the Bitpin backtest check passes.
3. FreqUI build, tested against the local dry-run bots, installed into `ui/installed`.
4. One full deploy (image rebuild and a 15–30 min upload). Both live bots restart once, ideally
   while they have no open trades. Because the UI hides notes on bots without the endpoints,
   shipping the UI before or after the image is safe.
