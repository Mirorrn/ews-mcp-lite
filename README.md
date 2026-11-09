# ews-mcp-lite

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that gives an
AI assistant typed access to a **Microsoft Exchange mailbox over EWS**: mail, calendar,
people, tasks, drafts and (explicitly enabled) sending.

- **FastAPI** host with the **official MCP Python SDK** (Streamable HTTP) mounted at `/mcp`
- **Live EWS only**: no cache, no database, no background sync
- **28 tools** in three capability tiers, plus a kill-switch for anything that leaves the mailbox
- Short id aliases (`m1`, `e2`, `d3` …) and compact result cards instead of raw EWS ids and HTML
- Built with **uv**; Python 3.12+

It is a slimmed-down sibling of [ews-mcp](https://github.com/azizmazrou/ews-mcp) (v5 /
release line 4.5). See [Differences from ews-mcp v5](#differences-from-ews-mcp-v5).

## Quick start

```bash
git clone <this repo> && cd ews-mcp-lite
uv sync
cp .env.example .env          # fill in EWS_SERVER_URL, EWS_EMAIL, EWS_PASSWORD ...
uv run ews-mcp-lite           # serves http://127.0.0.1:8000/mcp and /health
```

Connect Claude Code:

```bash
claude mcp add --transport http exchange http://127.0.0.1:8000/mcp
# with MCP_API_KEY set:
claude mcp add --transport http exchange http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <your MCP_API_KEY>"
```

Any other MCP client that speaks Streamable HTTP works the same way.

## Configuration

Environment variables (or a `.env` file in the working directory). See [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `EWS_SERVER_URL` | (required) | EWS endpoint, usually `https://<mail-server>/EWS/Exchange.asmx` |
| `EWS_EMAIL` | (required) | Mailbox address |
| `EWS_USERNAME` | `EWS_EMAIL` | Login name, if different (`DOMAIN\user`, UPN) |
| `EWS_PASSWORD` | | Password |
| `EWS_INSECURE_SKIP_VERIFY` | `false` | Skip TLS verification (test servers only) |
| `EWS_TZ` | `UTC` | IANA timezone for `today`, `+1d`, naive times and rendered dates |
| `EWS_CAPABILITY_TIER` | `draft` | `read`, `draft` or `full`; tools above the tier are not registered |
| `SEND_ENABLED` | `false` | Kill-switch for everything that leaves the mailbox |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Bind address |
| `MCP_API_KEY` | | If set, every request except `GET /health` needs `Authorization: Bearer <key>` |
| `LOG_LEVEL` | `INFO` | |

Authentication against Exchange uses exchangelib's auto-negotiation (Basic, NTLM or
Negotiate, whatever the server offers); the auth type is never pinned. OAuth / Microsoft 365
is not supported: Exchange Online is retiring EWS for third-party apps.

When `MCP_HOST` is `127.0.0.1`/`localhost`, the MCP SDK's DNS-rebinding protection only
accepts `Host: 127.0.0.1:<port>` / `localhost:<port>`. Binding to another address turns that
check off; put the server behind a reverse proxy with TLS and set `MCP_API_KEY` in that case.

## Tools

Ids in results are short aliases; pass them back exactly. They live in memory and are lost
on restart (a stale alias returns a `validation` error telling the model to re-run the
list/search tool). Raw EWS ids are accepted too.

| Tool | Tier | Kind | What it does |
|---|---|---|---|
| `list_folders` | read | read | Folder tree with total/unread counts (`f` ids) |
| `search_messages` | read | read | Live search in a folder: text, sender, subject, dates, unread, attachments |
| `get_message` | read | read | One message: recipients, cleaned body, attachment list |
| `get_thread` | read | read | Conversation from Inbox + Sent Items, oldest first |
| `get_attachment` | read | read | One attachment as text, base64 (≤ 512 KiB) or metadata |
| `get_mailbox_overview` | read | read | Unread count, newest unread cards, today's events |
| `waiting_on` | read | read | Sent mail with no reply after N days (live scan, capped at 200) |
| `list_events` | read | read | Events in a window, recurrences expanded (`e` ids) |
| `get_event` | read | read | One event with attendees and responses |
| `check_availability` | read | read | Free/busy for attendees plus up to 5 common free slots |
| `find_people` | read | read | Directory (ResolveNames) and personal contacts (`p` ids) |
| `get_contact` | read | read | One person by `p` id, contact id or email |
| `get_oof_settings` | read | read | Out-of-office state and replies |
| `list_tasks` | read | read | Open (or all) tasks (`k` ids) |
| `get_server_status` | read | read | Version, tier, kill-switch, tools, connection; `probe=true` makes a live call |
| `create_draft` | draft | write | New mail or reply/reply-all saved to Drafts (`d` ids); never sends |
| `update_draft` | draft | write | Change a draft's recipients, subject, body |
| `delete_draft` | draft | write | Move a draft to Deleted Items |
| `update_messages` | draft | write | Bulk (≤ 50) read flag and categories |
| `move_messages` | draft | write | Bulk (≤ 50) move to a folder |
| `create_event` | draft | write | Create an event; inviting attendees needs `SEND_ENABLED` |
| `update_event` | draft | write | Change subject/time/location/body; notifying attendees needs `SEND_ENABLED` |
| `update_task` | draft | write | Complete a task and/or set its due date |
| `send_draft` | full | send | Send an existing draft |
| `respond_to_event` | full | send | Accept / tentative / decline (sends a reply to the organizer) |
| `cancel_event` | full | send | Cancel a meeting you organize (sends cancellations) |
| `set_oof` | full | send | Set out-of-office replies (they go to other people) |
| `delete_messages` | full | destructive | Bulk (≤ 50) delete; default moves to Deleted Items, `permanent=true` hard-deletes |

Totals: read 15, draft 8 (23 at tier `draft`), full 5 (28 at tier `full`).

Errors come back as MCP tool errors whose text is JSON:
`{"code": "...", "message": "...", "hint": "..."}` with `code` one of `validation`,
`not_found`, `tier_blocked`, `kill_switch`, `auth_failed`, `upstream_unavailable`,
`upstream_error`. Tracebacks never reach the model.

## Safety model

1. **Tiers.** `EWS_CAPABILITY_TIER` decides which tools exist at all. The default `draft`
   can read and prepare, but has no tool that sends or deletes mail.
2. **Kill-switch.** With `SEND_ENABLED=false` (default) every send-class tool refuses
   before touching Exchange: `send_draft`, `respond_to_event`, `cancel_event`, `set_oof`.
   `create_event` / `update_event` check it only when they would notify attendees.
3. **Recoverable deletes.** `delete_messages` moves to Deleted Items unless `permanent=true`.

**There is no confirmation step.** Unlike ews-mcp v5 (two-phase, content-bound confirm
tokens), lite executes send-class and destructive tools as soon as they are called. Only
run tier `full` with `SEND_ENABLED=true` with a client that asks the user before tool calls,
and keep the default tier otherwise.

## Differences from ews-mcp v5

| v5 feature | lite | Why |
|---|---|---|
| SQLite mirror, FTS5 search, background sync | dropped | Live EWS only, nothing stored on disk |
| Arabic normalization in search | dropped | No local index to normalize |
| Optional semantic search (`find_similar`, pgvector) | dropped | Out of scope |
| Two-phase confirm, recipient allow/deny lists, send rate cap | dropped | Kept tiers + kill-switch only |
| Hash-chained audit log | dropped | Use the server log |
| Persistent alias store (SQLite) | in-memory | Aliases reset on restart |
| stdio transport, REST shim, OpenAPI, `/metrics` | Streamable HTTP + `/health` | FastAPI-hosted MCP only |
| Connection warm-up loop, circuit breaker | lazy connect, reset on auth/network errors | Minimal |

Fixed compared with v5:

- `check_availability` passes exchangelib `EWSDateTime`/`EWSTimeZone` values to
  `get_free_busy_info` (v5 passes `zoneinfo` datetimes and fails with `'ms_id'`).
- `get_event` retries without attendee properties when the server fails on them
  (grommunio, E-3127) and returns `attendees: null` with a note; `list_events` never asks
  for attendees.
- Message dates fall back from `DateTimeReceived` to `DateTimeSent`.
- exchangelib 5.0.3 cannot parse UTC timestamps with fractional seconds (grommunio sends
  `…05.198803600Z`); a small shim in `compat.py` fixes this so those dates are no longer `None`.
- Free-text search uses restrictions (`subject`/`body` contains), not AQS query strings,
  which some servers silently ignore.

## Development

```bash
uv sync
uv run ruff check
uv run pytest                      # unit tests, no network
uv run pytest -m integration       # live tests, see below
```

### Local test server: grommunio

[grommunio](https://grommunio.com) is an open-source groupware server with an EWS
implementation. `dev/grommunio/` builds it from the official
[gromox-container](https://github.com/grommunio/gromox-container) repository at a pinned
commit, creates two users and seeds test mail, a calendar event, a task and a contact:

```bash
dev/grommunio/setup.sh       # ~10 min on first run
dev/grommunio/teardown.sh    # stop and delete all data
```

Then run the integration suite against it:

```bash
export EWS_IT_URL=https://127.0.0.1/EWS/Exchange.asmx EWS_IT_INSECURE=1 EWS_IT_TZ=Europe/Berlin \
       EWS_IT_USER=alice@grommunio.test EWS_IT_PASSWORD='Al1cePass!' \
       EWS_IT_PEER=bob@grommunio.test   EWS_IT_PEER_PASSWORD='B0bPass!!'
uv run pytest -m integration
```

Integration tests tag everything they create with a unique marker and delete it afterwards.

**Apple Silicon notes** (found while building this; `setup.sh` handles the first two):

- The aarch64 `grommunio-chat` package has an unresolvable dependency, so the core image
  fails to build. The script removes chat from the core image and sets `ENABLE_CHAT=false`.
- `grommunio-office` has no aarch64 build; that image runs as x86_64 under emulation.
- Running the x86_64 core image under Rosetta does not work: gromox's daemons spin at 100 %
  CPU and never answer. The core must be built natively (the script does).
- `gromox-timer` fails to start (its binary is missing from the current package). It only
  handles deferred delivery.
- The stack publishes its ports (25, 80, 443, 465, 587, 993, 995, 8443 …) on **all
  interfaces**, i.e. it is reachable from your network. Stop it when you are not using it.

**grommunio EWS limitations** (lite works around them; real Exchange does not have them):

- Body text cannot be searched (`ErrorInvalidRestriction`, E-3224): `search_messages`
  falls back to subject-only matching and says so in a `note`.
- Attendee properties of plain appointments fail (E-3127): see `get_event` above.
- Filtering by conversation id returns nothing: `get_thread` shows only the message
  itself, and `waiting_on` matches replies by conversation id and normalized subject.
- Room lists (`GetRoomLists`) are not supported.

## License

MIT, see [LICENSE](LICENSE). Portions derived from
[ews-mcp](https://github.com/azizmazrou/ews-mcp) (MIT).
