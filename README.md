# Telegram Work Queue Bot

A Telegram bot that manages task queues for GitLab/GitHub merge requests in channels/groups with automatic cron-based reminders.

## Commands

### Task Management

| Command | Description |
|---------|-------------|
| `!wadd <MR/PR URL> [@username]` | Add a merge request (optionally assign to a user) |
| `!w` | List all tasks in the queue |
| `!wdone <N or task_id>` | Remove a task by sequence number or task ID |
| `!whelp` | Show help instructions |

### Reminders

| Command | Description |
|---------|-------------|
| `!wreminder-set <cron_expression>` | Set automatic reminder with cron schedule (UTC time) |
| `!wreminder` | Show current reminder configuration |
| `!wreminder-off` | Disable reminder (keeps configuration) |
| `!wreminder-remove` | Delete reminder configuration |

## Task IDs

Task IDs are derived from the merge request URL:

| URL | Task ID |
|-----|---------|
| `http://gitlab.example.com/group/monorepo/-/merge_requests/120` | `monorepo/merge_requests/120` |
| `https://github.com/owner/repo/pull/45` | `repo/pull/45` |

## Setup

### 1. Create a Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token provided

### 2. Configure Environment

Create a `.env` file with your bot token:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This includes:
- `python-telegram-bot` - Telegram Bot API wrapper
- `python-dotenv` - Environment variable management
- `APScheduler` - Cron-based reminder scheduling

### 4. Run the Bot

```bash
python bot.py
```

### 5. Add Bot to Channel/Group

1. Add the bot to your Telegram channel or group
2. Make the bot an **admin** with "Post Messages" permission
3. Start using commands!

## Usage Examples

### Task Management

```
# Add a task without assignee
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120

# Response:
# [#1] monorepo/merge_requests/120

# Add a task with assignee
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice

# Response:
# [#1] monorepo/merge_requests/120 → @alice

# List all tasks
!w

# Response:
# [#1] monorepo/merge_requests/120 → @alice (by @bob)
# [#2] backend/pull/45 (by @bob)

# Mark task as done (by number or task ID)
!wdone 1
# Or with # prefix:
!wdone #1

# Response:
# Removed [#1] monorepo/merge_requests/120 (added by @bob)

# Or by task ID:
!wdone backend/pull/45
```

### Reminders

```
# Set daily reminder at 9 AM UTC
!wreminder-set 0 9 * * *

# Response:
# ✅ Reminder set successfully!
# Schedule: 0 9 * * *
# Timezone: UTC

# Set weekday reminders at 9 AM and 5 PM UTC
!wreminder-set 0 9,17 * * 1-5

# Check current configuration
!wreminder

# Response:
# Reminder Configuration
# Status: ✅ Enabled
# Schedule: 0 9 * * *
# Timezone: UTC

# Disable reminder temporarily
!wreminder-off

# Delete reminder configuration
!wreminder-remove
```

## Supported URLs

- **GitLab**: `http://host/group/project/-/merge_requests/N`
- **GitHub**: `https://github.com/owner/repo/pull/N`

## Cron Expression Format

Reminders use standard 5-part cron expressions in UTC timezone:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── Day of week (0-6, Sunday=0)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

### Examples:

| Expression | Description |
|------------|-------------|
| `0 9 * * *` | Daily at 9:00 AM UTC |
| `0 9,17 * * *` | Daily at 9:00 AM and 5:00 PM UTC |
| `0 9 * * 1-5` | Weekdays (Mon-Fri) at 9:00 AM UTC |
| `0 */4 * * *` | Every 4 hours |
| `*/30 9-17 * * *` | Every 30 minutes between 9 AM-5 PM UTC |
| `0 0 * * 0` | Every Sunday at midnight UTC |

**Note:** Calculate your local time to UTC. For example, if you're in GMT+5 and want 9 AM local time, use `0 4 * * *` (4 AM UTC).

## Reminder Behavior

- Reminders are sent only when there are **pending tasks** in the queue
- Each channel/group can have its own independent reminder schedule
- Reminders persist across bot restarts
- You can temporarily disable reminders without losing the configuration

## Notes

- Each channel/group has its own isolated task queue
- Task IDs are unique per channel (same MR can't be added twice)
- Tasks are displayed as clickable links
- Each channel can configure its own reminder schedule
- The bot stores data in `workqueue.db` (SQLite)
