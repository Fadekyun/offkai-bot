# Offkai Bot

A Discord bot designed to simplify attendance management for large group gatherings and events.

## Table of Contents

- [Motivation](#motivation)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [How to Run](#how-to-run)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Running the Bot](#running-the-bot)
- [Commands](#commands)
- [Reporting Issues](#reporting-issues)
- [Contributing](#contributing)

## Motivation

Offkai Bot was created to streamline the process of attendance gathering for large group events, particularly for dinners after lives, concerts, and other gatherings.

This bot replaces manual methods of collecting attendance such as:
- Google Forms surveys
- Collecting reactions on Discord messages
- Manually tracking "I'm attending" replies

**Previous challenges solved:**
- ✅ Errors in keeping track of attendees
- ✅ Attendees missing important updates
- ✅ Manual data entry and collation
- ✅ No automated notification system
- ✅ Difficulty managing capacity limits and waitlists

## Features

### Core Capabilities

- **📅 Event Management**
  - Create events with venue, address, Google Maps links, and deadlines
  - Set maximum capacity limits for events (with smart validation)
  - Modify event details including capacity (with safety checks)
  - Capacity increase allowed anytime, decrease only when safe
  - Cannot reduce capacity below current attendee count or with active waitlist
  - Open/close event registrations
  - Archive old events
  - Optional participant role: auto-assigned mentionable Discord role for attendees, auto-removed on withdrawal, auto-deleted on archive
  - Optional ping role for deadline reminders (7-day, 3-day, 24-hour)
  - Bilingual (English + Japanese) event messages, rules, DM notifications, and reminders

- **👥 Attendance Tracking**
  - Interactive Discord buttons for attendance confirmation
  - Support for bringing extra people (+0 to +5 guests)
  - Behavior and arrival time confirmations
  - Optional drink preferences tracking
  - Optional event-specific preferred-name capture for attendees and interest checks
  - View current attendance count and attendee list

- **📋 Waitlist System**
  - Automatic waitlist when events reach capacity
  - FIFO (First In, First Out) promotion from waitlist
  - Batch promotion when multiple spots open up
  - Waitlist joins for groups that exceed remaining capacity
  - Join waitlist after registration deadline or when event is closed

- **🔔 Smart Notifications**
  - DM notifications for successful registrations (with responsibility warnings)
  - DM notifications when promoted from waitlist (with responsibility warnings)
  - DM notifications when withdrawing from events (with post-deadline warnings)
  - Fallback to ephemeral channel messages if DMs are disabled
  - Thread notifications when capacity is reached
  - Clear warnings about post-deadline withdrawal consequences

- **🎯 Registration Controls**
  - Prevent duplicate registrations (same user can't register twice for same event)
  - Cross-list duplicate prevention (can't be in both responses and waitlist)
  - Event isolation (registrations are per-event)
  - Withdrawal with automatic waitlist promotion
  - Withdrawal allowed even after event is closed or deadline passed
  - Post-deadline withdrawal warnings inform users of their responsibilities
  - Unified data structure with automatic migration from legacy format

- **🏷️ Names and Identity**
  - RSVP forms optionally ask for the name organizers should use
  - Blank or whitespace-only input falls back to the user's current Discord display name, then username
  - Names are trimmed and snapshotted per event; later Discord nickname changes do not rewrite existing responses
  - Discord `user_id` remains authoritative, while usernames remain available to organizers for auditing and disambiguation
  - Duplicate preferred names are allowed; organizers can correct confirmed attendee and guest names in the admin panel without changing the registration

- **💬 Communication Tools**
  - Broadcast messages to all event attendees
  - Thread-based event discussions
  - Automatic thread management (add/remove users)
  - Pin important event messages

- **🗑️ Management Commands**
  - Delete individual user responses
  - View drinks summary for catering
  - Archive completed events

- **🏆 Ranking / Milestones**
  - Attendance milestone tracking (rank 1, 5, 10) with celebratory DM messages

### Data Architecture

- **Unified Storage Model**
  - Attendees and waitlists share one responses JSON file, with separate arrays per event
  - Legacy response/event/ranking records remain readable; response format migration is written on the next successful mutation
  - Changes are staged, written by atomic file replacement, then published to the in-memory cache; write errors propagate
  - Missing data directories/files are created automatically. Invalid JSON, schema, or registration records fail loading without a writable partial/empty cache; originals remain in place and a best-effort `.corrupted-*.bak` copy is made
  - Repair invalid event/response data (including any legacy waitlist file) before restarting; a backup is not automatic recovery. Invalid optional rankings are logged and remain unloaded/write-disabled until repaired, without blocking attendance startup
  - Legacy defaults and existing optional payment-expiry metadata recovery remain supported; malformed registration records are not silently skipped
  - New events are saved before Discord publication. A failed announcement reports failure but leaves the event and thread identifiers available for recovery
  - Announcement refresh after a committed state change is best-effort: deleted messages are replaced, transient/permission fetch errors never create duplicates, and replacement failure is logged without undoing the saved state
  - Atomicity is per JSON file only: responses, event numbering/status, rankings, and Discord notifications are separate commits, not a cross-file transaction. A later failure can leave earlier commits applied
  - Closing writes attendee numbering before committing the closed event/count. If either write fails the event remains open and closing can be retried; a failed final event write can leave numbering already saved
  - Reopening clears numbering before committing the open state. A clearing or final event-write failure leaves the event closed and retryable; numbering can already be cleared if the final event write fails

- **Responsibility System**
  - All registrations include clear warnings about post-deadline withdrawals
  - Users informed of potential payment obligations and moderation consequences
  - Warnings shown at registration, waitlist join, and promotion
  - Post-deadline withdrawal messages emphasize user responsibility

- **Asynchronous Tasks & Alerts**
  - Robust background task scheduling system for automated event management
  - Clean separation of task definitions, execution loops, and event-specific reminder registration
  - Modular design prevents circular dependencies between core event actions and background jobs

## Prerequisites

Before running Offkai Bot, you need:

1. **Python 3.12 or higher**
   - Check your version: `python --version`

2. **uv (Python package manager)**
   - Install from: https://docs.astral.sh/uv/getting-started/installation/
   - Or quick install: `curl -LsSf https://astral.sh/uv/install.sh | sh`

3. **Discord Bot Token**
   - Create a bot at: https://discord.com/developers/applications
   - Enable required intents:
     - ✅ Server Members Intent
     - ✅ Message Content Intent (if using message features)
   - Copy the bot token for configuration

4. **Discord Server (Guild) ID**
   - Enable Developer Mode in Discord (User Settings > Advanced > Developer Mode)
   - Right-click your server and select "Copy Server ID"

## Monorepo Layout

The repository is organized as a monorepo:
* **`bot/`**: Contains the Python Discord bot code, test files, and its Dockerfile.
* **`frontend/`**: Contains the Next.js frontend application (admin dashboard, check-in scanning, and RSVP cards) and its Dockerfile.
* **`data/`**: Shared runtime databases (JSON files) on the host machine.
* **`logs/`**: Log files from the bot's runtime.

---

## How to Run

### Running with Docker Compose (Recommended)

Running via Docker Compose automatically launches both the Discord bot and the Next.js web application, linking their data directories read-write:

1. **Verify the configuration:**
   The configuration file at `bot/config.json` is tracked in Git and pre-configured. You can edit it if you need to customize your paths, making sure they point to `data/` since they are relative to the `/app` container root.

2. **Configure env variables:**
   In the root `.env` file, set `ADMIN_KEY` to a secure password. This key authorizes staff to view the check-in panel.

3. **Launch the services:**
   ```bash
   docker compose up -d --build
   ```

4. **Verify container status:**
   ```bash
   docker compose ps
   docker compose logs -f
   ```

### Admin registration removal

The admin panel can remove a confirmed attendee or waitlisted user from the selected event.
Confirmed removal frees the whole group's places, runs the existing waitlist promotion,
and removes the user from the Discord thread. Discord cleanup failures are reported after removal.

Compose configures a private bot API on port 8091 without publishing it to the host.
For local development, set `ADMIN_API_PORT=8091` for the bot (host defaults to `127.0.0.1`)
and `BOT_ADMIN_URL=http://127.0.0.1:8091` for the frontend. Both use the same `ADMIN_KEY`.
Without `ADMIN_API_PORT`, the bot API remains disabled. Keep this connection private;
the browser calls the existing frontend admin API, which forwards the action to the bot.

### Admin attendee name corrections

Confirmed attendees have an edit control for their event display name and their existing guest names.
Only the current guest slots are editable; the editor never adds, removes, reorders or compacts guests, and an
untouched legacy unnamed slot is preserved. Waitlisted registrations are out of scope.

The browser captures a compact SHA-256 revision of the original names when the editor opens, not the full raw
snapshot. One editor Save sends the changed attendee and guest names plus the expected revision in a single request.
The maximum JSON body is 4064 UTF-8 bytes, so the existing 4 KiB application limit is unchanged; edited control
characters and invalid Unicode surrogates are rejected by the bot before trimming. The bot resolves the event's
Discord thread to enforce the configured guild boundary, then revalidates the selected event, user ID, registration
timestamp, name revision and guest slots immediately before its existing atomic JSON write. A removed, rejoined or
promoted/demoted registration, a changed guest count, changed surplus legacy names, or another admin's name edit is
rejected with a refresh/retry conflict instead of overwriting current data. Unrelated payment or check-in updates
are preserved. After the atomic write, saving names sends one bilingual DM listing only the fields that actually
changed. It does not change roles or trigger signup, payment, attendance or promotion actions, and previously sent
event messages are not updated. If DM delivery fails, the saved names remain and the admin panel shows a warning.
The admin panel refreshes the roster after every save and treats a lost response as unconfirmed rather than
retrying the write blindly.

### Payment proof by DM

Registered attendees and waitlisted users can DM `payment proof <event name>` with one screenshot.
`proof payment <event name>` and `i paid <event name>` also work. Names ignore case, spaces and punctuation;
unique partial names and clear minor typos are accepted. Ambiguous names require clarification before upload.
Only private DMs are handled. Images use the existing frontend upload/compression/replacement pipeline.
The bot acknowledges successful storage; marking an unpaid user paid in admin sends a confirmation DM.
Notification failure is reported in admin without undoing the saved payment status.

Set the bot's `PROOF_UPLOAD_URL` to the frontend's internal base URL when different from `FRONTEND_URL`.
Compose already sets this. Local development falls back to `FRONTEND_URL` and uses the private bot API above.

### Running the Bot Standalone (Without Frontend)

If you only want to run the Discord bot and do not need the Next.js web application, you have two options:

#### Option A: From the `bot/` directory (Recommended)
Navigate to the `bot/` folder and launch compose. It will start only the bot container, still storing data and logs in the shared parent folders:
```bash
cd bot
docker compose up -d --build
```

#### Option B: From the root directory
You can start only the bot service from the main compose file:
```bash
docker compose up -d --build discord-offkai-bot
```

### Running Locally for Development

#### 1. Running the Bot
Make sure you are in the root directory.
```bash
# Verify uv is installed
uv --version

# Run the bot
uv run offkai-bot --config-path bot/config.json
```

#### 2. Running the Frontend
Navigate to the `frontend/` directory:
```bash
cd frontend

# Install Node dependencies
npm install

# Start the Next.js dev server
npm run dev
```
Open [http://localhost:8090](http://localhost:8090) in your browser. (The API routes will automatically read database files from the sibling `../data/` folder).

---

## Configuration

### 1. Environment Secrets (`.env`)

Create a `.env` file in the root directory. This is where all sensitive credentials and API keys are stored for both the bot and the frontend:

```env
# Frontend Admin Key (for the staff check-in dashboard)
ADMIN_KEY=supersecretadmin123

# Discord Bot Token
DISCORD_TOKEN=YOUR_DISCORD_TOKEN_HERE

# Guild IDs (comma-separated list of server IDs where the bot is active)
GUILDS=123456789012345678
```

### 2. Bot Configuration (`bot/config.json`)

The `bot/config.json` file inside the `bot` folder is tracked in Git and pre-configured for the monorepo layout. You can edit it if you need to customize your database file paths (which are relative to the `/app` container root):

```json
{
    "EVENTS_FILE": "data/events.json",
    "RESPONSES_FILE": "data/responses.json",
    "WAITLIST_FILE": "data/waitlist.json",
    "RANKING_FILE": "data/ranking.json"
}
```

### Configuration Fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `EVENTS_FILE` | string | ✅ | Path to events data file inside container (e.g., `data/events.json`) |
| `RESPONSES_FILE` | string | ✅ | Path to responses data file (includes both attendees and waitlist) |
| `RANKING_FILE` | string | ✅ | Path to ranking data file (e.g., `data/ranking.json`) |
| `WAITLIST_FILE` | string | ❌ | Legacy waitlist file; loaded alongside list-valued legacy responses and migrated on the next successful save |

After checking and saving migrated responses, remove `WAITLIST_FILE` from configuration or archive the old waitlist file. For legacy compatibility, an empty responses object (`{}`) still loads that file; leaving it configured can restore stale waitlist entries after an empty import/reset. The bot cannot distinguish legacy-empty and modern-empty files automatically.

3. **Data Directory:**

   The bot will automatically create the `data/` directory and JSON files if they don't exist.

4. **Log File:**

   The bot logs to both the console and `logs/offkai-bot.log` by default. When using Docker Compose, `./logs` is mounted into the container so the log file appears on the host.

### Running the Bot

**Basic command:**
```bash
uv run offkai-bot
```

**With custom config path:**
```bash
uv run offkai-bot --config-path /path/to/config.json
```

**With Docker Compose:**
```bash
docker compose up -d --build
docker compose logs -f discord-offkai-bot
```

**For development (running main.py):**
```bash
uv run python -m offkai_bot.main --config-path bot/config.json
```

### Running Tests

```bash
# Run all tests
uv run pytest bot/tests

# Run specific test file
uv run pytest bot/tests/test_event_actions.py

# Run with verbose output
uv run pytest bot/tests -v

# Run with coverage
uv run pytest bot/tests --cov=bot/src/offkai_bot
```

### Code Quality Checks

```bash
# Run all pre-commit hooks (via prek)
uv run prek run --all-files

# Format code
uv run ruff format .

# Check linting
uv run ruff check .

# Type checking
uv run ty check
```

## Commands

### Event Management

- `/create_offkai` - Create a new event
  - Parameters: name, venue, address, maps_link, datetime, deadline (optional), drinks (optional), max_capacity (optional), ping_role (optional), create_role (optional, default: False)

- `/modify_offkai` - Modify an existing event
  - Parameters: event_name, update_msg, venue (optional), address (optional), google_maps_link (optional), date_time (optional), deadline (optional), drinks (optional), max_capacity (optional)
  - Note: Capacity can be increased anytime, but can only be decreased if new capacity ≥ current attendee count AND waitlist is empty

- `/close_offkai` - Close event registrations
  - Parameters: event_name, close_msg (optional)

- `/reopen_offkai` - Reopen event registrations
  - Parameters: event_name, reopen_msg (optional)

- `/archive_offkai` - Archive a completed event
  - Parameters: event_name

### Attendance Management

- `/attendance` - View list of attendees
  - Parameters: event_name, sort (optional, default: False), nicknames (optional, default: False; includes `@username` for disambiguation), drinks (optional, default: False; shows per-attendee drink choices)

- `/drinks` - View drinks summary
  - Parameters: event_name

- `/delete_response` - Remove a user's registration
  - Parameters: event_name, member

- `/promote` - Manually promote a user from the waitlist, bypassing capacity limits
  - Parameters: event_name, username (autocomplete)

- `/waitlist` - View waitlisted users
  - Parameters: event_name, sort (optional, default: False), nicknames (optional, default: False; includes `@username` for disambiguation)

- `/export_responses` - DM yourself the raw responses data for this server as JSON, plus an `_export` section that `/import_responses` needs

- `/import_responses` - Replace the responses data with an edited export
  - Parameters: file, apply (optional, default: False; without it the command only previews the changes)
  - Uploads are validated strictly; any error rejects the whole file. Only the events you edited are changed, so registrations made after the export are kept. If an event you edited also changed in the bot after the export, the import is rejected and you need a fresh export. On apply, a file that undoes the import is attached to the reply and the previous data is also backed up on disk. The waitlist is not rebalanced automatically.

- `/resend_confirmation` - DM registrants their current registration details with a note that the organizers updated them (e.g. after `/import_responses`)
  - Parameters: event_name, member (optional; leave empty to DM every attendee and waitlisted user)
  - Reports anyone whose DMs couldn't be delivered.

### Communication

- `/broadcast` - Send message to all attendees
  - Parameters: event_name, message

### Interactive Buttons

Users interact with events through Discord buttons:

- **"Confirm Attendance"** - Register for an event
- **"Withdraw Attendance"** - Cancel registration
- **"Join Waitlist"** - Join the waitlist when event is full or closed
- **"Attendance Count"** - View current registration count

The RSVP and interest-check forms include an optional **Name you'd like us to use** field. The submitted value is trimmed and stored with that event. Leaving it blank uses the current Discord display name (or username if no display name is available). Organizers see the preferred name by default; setting `nicknames=True` on `/attendance` or `/waitlist` adds `@username` so duplicate preferred names can be distinguished.

## Reporting Issues

If you encounter bugs or issues:

1. **Check existing issues:** https://github.com/yourusername/offkai-bot/issues
2. **Create a new issue** with:
   - Clear description of the problem
   - Steps to reproduce
   - Expected vs actual behavior
   - Bot version and environment details
   - Relevant error messages or logs

**Issue Template:**
```markdown
**Describe the bug**
A clear description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Run command '...'
2. Click on '...'
3. See error

**Expected behavior**
What you expected to happen.

**Environment:**
- OS: [e.g. Ubuntu 22.04]
- Python version: [e.g. 3.12.0]
- Discord.py version: [e.g. 2.3.2]

**Additional context**
Any other context about the problem.
```

## Contributing

We welcome contributions! Here's how you can help:

### Getting Started

1. **Fork the repository**
2. **Create a feature branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. **Make your changes**
4. **Run tests and quality checks:**
   ```bash
   uv run pytest bot/tests
   uv run ruff format .
   uv run ruff check .
   uv run ty check
   ```
5. **Commit your changes:**
   ```bash
   git commit -m "Add: your feature description"
   ```
6. **Push to your fork:**
   ```bash
   git push origin feature/your-feature-name
   ```
7. **Create a Pull Request**

### Contribution Guidelines

- **Code Style:** Follow PEP 8, enforced by Ruff
- **Type Hints:** Use type hints for all functions
- **Tests:** Add tests for new features
- **Documentation:** Update README and docstrings
- **Commits:** Use clear, descriptive commit messages

### Development Setup

```bash
# Clone your fork
git clone https://github.com/yourusername/offkai-bot.git
cd offkai-bot

# Install development dependencies
uv sync

# Install git hooks (using prek)
uv run prek install

# Run tests in watch mode
uvx pytest-watch bot/tests

# Check code quality
uv run prek run --all-files
```

### Areas for Contribution

- 🐛 Bug fixes
- ✨ New features
- 📝 Documentation improvements
- 🧪 Test coverage
- 🎨 UI/UX improvements
- 🌐 Localization/translations
- ⚡ Performance optimizations

### Code of Conduct

- Be respectful and inclusive
- Provide constructive feedback
- Focus on the issue, not the person
- Help create a welcoming environment for all contributors

---

## Deployment

After upgrading payment eligibility checks, confirmed registrations with a missing or no-longer-configured payment
method cannot submit proof through the RSVP page or the bot's `I paid` handler. An organizer can run
`/resend_confirmation` for each affected member on a live event to offer a payment choice. Existing choice deadlines
are preserved; resending does not extend them. Historical payment records are retained. Review affected registrations
before a bulk resend, since it sends DMs to registrants, including the waitlist. Legacy records may already have
paid through the former waitlist proof flow: they default to no automatic demotion. Repairing a missing/renamed
method or promoting a legacy entry without a valid method can offer a payment-choice window, but expiry does not
remove their confirmed place. Paid status remains in the frontend store; the bot does not infer nonpayment from
a missing method.

The organizer admin page has an up-arrow beside each waitlisted registration. Confirming it promotes that attendee
and their guests even when the event is full. Events with configured payment methods send a fresh 72-hour
payment-choice window, as do `/promote` and automatic waitlist promotion. Legacy entries with a still-configured
saved method retain that choice and skip the popup. Automatic promotion respects capacity; the arrow and `/promote`
bypass it. If delivery fails, the promotion remains saved and the organizer can resend the confirmation.
Resends preserve active and demoting deadlines. For exempt legacy entries only, resending after a choice window
has expired starts a new 72-hour window; it never makes those registrations eligible for automatic demotion.

New signups that explicitly defer payment selection are marked eligible for automatic demotion. If their window
expires without a selected method, the attendee and all guests move to the end of the waitlist. The bot checks
saved deadlines every minute in a separate loop after it is ready, including deadlines missed while offline. Slow
payment DMs cannot block close/reminder alerts. Existing waitlisted groups can fill the released places, but users
demoted in that pass cannot immediately promote themselves again. Their answers and payment history are retained;
this is not a voluntary withdrawal and does not reduce rank.
The organizer can use the arrow or `/promote` to grant another fresh 72-hour choice. Archived events, interest checks,
events that have started, and events with no configured payment methods are excluded from expiry processing.
The event time is rechecked after slow lookups and before each automatic promotion, including in an ongoing batch.

Organizers cannot mark a new deferred-payment registration paid until the attendee has chosen a configured method.
The admin control explains this requirement, and the payment API rejects the operation before writing paid status.
Method selection already cancels the demotion deadline, so a failed payment-confirmation DM cannot restore it.
Legacy payment correction access is preserved.

Automatic batches only consider the queue entries present when they started. Expiry backfill is saved with the
demotion and retried after failures or restart, using the original attendance target and eligible registrations.
Retries do not extend payment-choice deadlines or send duplicate promotion confirmations for already-saved moves.

A helper deployment script `deploy.sh` is provided in the root directory to automate deploying updates on your production host (e.g. Raspberry Pi):

1. **Host Setup:**
   * Clone the repository into `/home/eyal/offkai-bot/` on your Pi.
   * Create your production `.env` and `bot/config.json` configurations in that folder.
   * Run the deploy script manually:
     ```bash
     ./deploy.sh
     ```

2. **How the Script Works:**
   * It resets the local repository to match `origin/master`.
   * It safely cleans untracked files *without* deleting your active production databases (`data/`), bot logs (`logs/`), the configuration (`bot/config.json`), or your environment secrets (`.env`).
   * It pulls the latest Docker images, stops the old containers, and starts the new ones in detached mode.
   * Finally, it prunes old unused images to save disk space on the Pi.

3. **CI/CD Integration:**
   * If you are using GitHub Actions with a self-hosted runner on the Pi, you can trigger this script automatically on push to `master`. The workflow is located at `.github/workflows/deploy-to-pi.yml`.

---

## License

This project is licensed under the MIT License - see below for details:

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
