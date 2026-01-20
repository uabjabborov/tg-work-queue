# Telegram Work Queue Bot

A Telegram bot that manages task queues for GitLab/GitHub merge requests in channels/groups.

## Commands

| Command | Description |
|---------|-------------|
| `!wadd <MR/PR URL> @username` | Add a merge request and assign it to a user |
| `!w` | List all tasks in the queue |
| `!wdone <N or task_id>` | Remove a task by sequence number or task ID |
| `!whelp` | Show help instructions |

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

### 4. Run the Bot

```bash
python bot.py
```

### 5. Add Bot to Channel/Group

1. Add the bot to your Telegram channel or group
2. Make the bot an **admin** with "Post Messages" permission
3. Start using commands!

## Usage Examples

```
# Add a task
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice

# Response:
# [#1] monorepo/merge_requests/120 → @alice

# List all tasks
!w

# Response:
# [#1] monorepo/merge_requests/120 → @alice (by @bob)
# [#2] backend/pull/45 → @charlie (by @bob)

# Mark task as done (by number or task ID)
!wdone 1

# Response:
# Removed [#1] monorepo/merge_requests/120 (added by @bob)

# Or by task ID:
!wdone backend/pull/45
```

## Supported URLs

- **GitLab**: `http://host/group/project/-/merge_requests/N`
- **GitHub**: `https://github.com/owner/repo/pull/N`

## Notes

- Each channel/group has its own isolated task queue
- Task IDs are unique per channel (same MR can't be added twice)
- Tasks are displayed as clickable links
- The bot stores data in `workqueue.db` (SQLite)
