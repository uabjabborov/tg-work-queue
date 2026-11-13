# Telegram Work Queue Bot

A Telegram bot that manages task queues for GitLab/GitHub merge requests in channels/groups with automatic cron-based reminders and weekly contributor and reviewer leaderboards.

## Commands

### Task Management

| Command | Description |
|---------|-------------|
| `!wadd <MR/PR URL> [...] [@username ...]` | Add one or more merge requests (optionally assign to one or more users) |
| `!w` | List all tasks in the queue |
| `!wme` | Privately show reviews assigned to you, and your PRs/MRs that were approved (ready to merge) or bounced |
| `!wmerged <PR/MR references>` | Mark your approved PRs/MRs as merged, so they leave `!wme` |
| `!wclear <PR/MR references>` / `!wclear all` | Clear your approved or bounced PRs/MRs from `!wme` (privately lists what was cleared) |
| `!wdone <PR/MR references>` | Remove completed reviews |
| `!wbounce <PR/MR references>` | Remove reviews with a "Changes required" comment in the chat |
| `!wassign <PR/MR references> @username [...]` | Assign or reassign reviews (replaces all existing assignees) |
| `!whelp` | Show help instructions |

### Reminders

| Command | Description |
|---------|-------------|
| `!wreminder-set <cron_expression>` | Set automatic reminder with cron schedule (UTC time) |
| `!wreminder` | Show current reminder configuration |
| `!wreminder-off` | Disable reminder (keeps configuration) |
| `!wreminder-remove` | Delete reminder configuration |

### Leaderboards

| Command | Description |
|---------|-------------|
| `!wleaderboard` | Show this week's contributor and reviewer standings |
| `!wleaderboard last` | Show the previous Monday–Sunday |
| `!wleaderboard-off` | Disable automatic weekly recaps; keep tracking activity |
| `!wleaderboard-on` | Enable recaps starting with the next scheduled delivery |

Each chat has separate rankings. Both show the top five people, counts, date range, and **Asia/Tashkent** timezone, ordered by count descending and then name alphabetically. On-demand standings include empty states.

| Action | Contributor credit | Reviewer credit |
|--------|--------------------|-----------------|
| `!wdone` | The person who submitted the queue entry | The command sender, unless also the submitter |
| `!wbounce` | None | The command sender, unless also the submitter |

Each person earns at most **one point per PR/MR per ranking per week**, including when a review is re-added or bounced multiple times. Different reviewers can each earn a point for the same PR/MR. Assignees do not receive credit automatically. Points belong to the completion week, regardless of when the review was submitted. PR/MR identities use the URL's host, full project path, and normalized number, ignoring scheme, query, fragment, and trailing slashes.

Automatic recaps run every **Monday at 09:00 Asia/Tashkent**, covering the previous Monday–Sunday. Existing chats are enrolled during migration; new chats are enrolled on their first successful submission. Any chat member can toggle recaps, and an opt-out persists across submissions and restarts. Recaps are skipped if both rankings are empty. After a restart, the bot attempts only the most recent overdue recap that was enabled when due; successful deliveries are saved to prevent duplicates. Re-enabling does not send an overdue recap immediately.

Telegram user IDs keep credit together when a username changes. Existing queue entries retain their saved submitter names; legacy names match known IDs only when unambiguous. Otherwise credit stays under the legacy name. Unknown or anonymous senders receive no individual entry. History starts at rollout, including later completions of currently queued reviews; previously deleted reviews cannot be reconstructed. Scoring reflects bot commands and identifies the queue submitter as the contributor.

## PR/MR References

PR/MR references are derived from the merge request or pull request URL:

| URL | PR/MR reference |
|-----|---------|
| `http://gitlab.example.com/group/monorepo/-/merge_requests/120` | `monorepo/120` |
| `https://github.com/owner/repo/pull/45` | `repo/45` |

Use `repo/N`, the full PR/MR URL, or a bare PR/MR number such as `120` with `!wdone`, `!wbounce`, `!wassign`, `!wmerged`, and `!wclear`. A bare number works only when one review in the current chat has that number. If several reviews have it, use a qualified reference such as `monorepo/120`. Queue sequence numbers and legacy `#N` commands are not accepted; use `!w` to see current references.

`!wdone`, `!wbounce`, `!wassign`, `!wmerged`, and `!wclear` also take several references at once, separated by spaces, commas, or new lines, and PR/MR number ranges such as `1-4`, `1..4`, or `1...4`:

```
!wdone 1 2 3 4
!wdone 1...4
!wbounce repo/55, repo/67
!wassign 55 67 88-90 @alice
!wmerged 55-60
```

A range matches every review numbered within it. Because `!wdone`, `!wbounce`, and `!wassign` change the shared queue, a range there is refused if it includes the same number in several repos (use qualified references such as `api/7 web/7` instead), and if the ranges in one command end up covering more than 20 reviews in total, the whole command is rejected. References that match nothing or match several reviews are reported, and the rest are still processed in one transaction. If the database fails, nothing is changed.

`!wassign` gives each run of references to the `@username`s that follow it, so one message can assign different reviews to different people:

```
!wassign 55 67 @alice
88 @bob @carol
```

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

Optionally, let Claude write the [reviewer cheers](#reviewer-cheers-wdone--wbounce) (a paid API; without a key, cheers come from the free built-in library):

```
ANTHROPIC_API_KEY=sk-ant-...
# Optional: another Claude model (default claude-haiku-5-5), or CHEERS_AI=off to keep the key but use templates
CHEERS_MODEL=claude-haiku-5-5
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This includes:
- `python-telegram-bot` - Telegram Bot API wrapper
- `python-dotenv` - Environment variable management
- `APScheduler` - Cron-based reminder scheduling
- `anthropic` - Claude API client for optional AI-written reviewer cheers (only used when `ANTHROPIC_API_KEY` is set)

### 4. Run the Bot

#### Option A: Run Directly

```bash
python bot.py
```

#### Option B: Run with Docker

```bash
# Build the image
docker-compose build

# Start the bot
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the bot
docker-compose down
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
# monorepo/120 (clickable link)

# Add a task with single assignee
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice

# Response:
# monorepo/120 → @alice

# Add a task with multiple assignees
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/120 @alice @bob @charlie

# Response:
# monorepo/120 → @alice, @bob, @charlie

# Add several tasks at once: @usernames assign the links before them on the same line
!wadd http://gitlab.example.com/group/monorepo/-/merge_requests/121 http://gitlab.example.com/group/backend/-/merge_requests/45 @alice
http://gitlab.example.com/group/backend/-/merge_requests/46 @bob
http://gitlab.example.com/group/backend/-/merge_requests/47

# Response:
# Added 4
# monorepo/121 → @alice
# backend/45 → @alice
# backend/46 → @bob
# backend/47

# List all tasks
!w

# Response:
# monorepo/120 → @alice, @bob (by @dave)
# backend/45 (by @dave)

# Show reviews assigned to you, and your own PRs/MRs to merge or fix (only you see the reply)
!wme

# Response (visible only to @alice):
# Assigned to you (1)
# monorepo/120 → @alice, @bob (by @dave)
#
# Approved, ready to merge (1)
# backend/48 — approved by @bob on Oct 28
# After merging, send !wmerged <PR/MR reference> in its chat.
#
# Your PRs/MRs that need changes (1)
# backend/52 — bounced by @bob on Oct 28
# Re-add one with !wadd once it's fixed.
# Approvals and bounces older than 14 days are hidden. Clear them sooner with !wclear <PR/MR references> or !wclear all.

# After merging an approved PR/MR, take it off your list
!wmerged backend/48

# Response:
# ✅ Marked backend/48 as merged.

# Clear several approved or bounced PRs/MRs from your !wme list at once
!wclear 52, 55, 60-100

# Private response (only you see it in a group):
# 🧹 Cleared 3 from your !wme list
# backend/52 — bounced by @bob on Oct 28
# ...

# Or clear everything approved or bounced
!wclear all

# Assign or reassign a task (replaces all existing assignees)
!wassign monorepo/120 @eve
# Or use the PR/MR URL:
!wassign http://gitlab.example.com/group/monorepo/-/merge_requests/120 @eve @frank

# Response:
# monorepo/120 → @eve, @frank

# Mark a review as done (by PR/MR number when unambiguous)
!wdone 120

# Response:
# Removed monorepo/120 (added by @dave)

# Or by qualified PR/MR reference:
!wdone backend/45

# Remove a review that needs changes (by PR/MR reference or URL)
!wbounce monorepo/120
# Or by URL:
!wbounce http://gitlab.example.com/group/monorepo/-/merge_requests/120

# Response:
# Removed monorepo/120 (added by @dave)
# Changes required.
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
!wreminder-set 0 9,17 * * 0-4

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

Reminders use 5-part cron expressions in UTC timezone:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── Day of week (0-6, 0=Monday, 1=Tuesday, ..., 6=Sunday)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

**Note:** Day of week uses 0=Monday (not Sunday like standard cron).

### Examples:

| Expression | Description |
|------------|-------------|
| `0 9 * * *` | Daily at 9:00 AM UTC |
| `0 9,17 * * *` | Daily at 9:00 AM and 5:00 PM UTC |
| `0 9 * * 0-4` | Weekdays (Mon-Fri) at 9:00 AM UTC |
| `0 9 * * 5,6` | Weekends (Sat, Sun) at 9:00 AM UTC |
| `0 */4 * * *` | Every 4 hours |
| `*/30 9-17 * * *` | Every 30 minutes between 9 AM-5 PM UTC |
| `0 0 * * 6` | Every Sunday at midnight UTC |

**Timezone Note:** Calculate your local time to UTC. For example, if you're in GMT+5 and want 9 AM local time, use `0 4 * * *` (4 AM UTC).

## Reminder Behavior

- Reminders are sent only when there are **pending tasks** in the queue
- Each channel/group can have its own independent reminder schedule
- Reminders persist across bot restarts
- You can temporarily disable reminders without losing the configuration

## Features

- **Multiple Assignees**: Assign tasks to multiple team members
- **Isolated Queues**: Each channel/group has its own independent task queue
- **Unique Tasks**: PR/MR references are unique per channel (the same MR can't be added twice)
- **Clickable Links**: Tasks are displayed as clickable links to the MR/PR
- **Private Assignment View**: `!wme` replies with an ephemeral message only the sender can see, including your PRs/MRs that are ready to merge or need changes
- **Custom Reminders**: Each channel can configure its own reminder schedule
- **Weekly Leaderboards**: Separate contributor and reviewer rankings and Monday recaps for each chat
- **Reviewer Cheers**: After `!wdone` or `!wbounce`, the reviewer privately gets a playful message about their speed or bounce round, and the chat celebrates when the queue becomes empty
- **Persistent Storage**: Data is stored in SQLite database (`workqueue.db`)
- **Flexible Assignment**: Reassign tasks at any time, replacing all existing assignees

## Adding Several PRs/MRs (`!wadd`)

Put several links in one `!wadd`, separated by spaces, commas, or new lines. `@username`s assign the links before them on the same line, back to the previous `@username`s, so each line can have its own reviewers and links with no `@username` after them stay unassigned. A line of only `@username`s assigns the line before it. If any link or username is invalid, nothing is added. Links already in the queue are skipped and listed in the reply.

## Private Replies (`!wme`)

`!wme` lists the reviews in the current chat whose assignees include your `@username`. Matching ignores case. Long lists are split across several messages.

It also lists the PRs/MRs you added that a reviewer finished, with who finished them and when (Asia/Tashkent date):

- **Approved, ready to merge:** a reviewer ran `!wdone`. The bot can't see when you merge, so after merging send `!wmerged <PR/MR reference>` in the same chat to take it off your list. `!wmerged` only works on your own approved PRs/MRs. Running `!wdone` on your own PR/MR doesn't count as an approval. Marking a PR/MR as merged doesn't change the leaderboard.
- **Need changes:** a reviewer ran `!wbounce`. It stays on your list until it's added to the queue again with `!wadd`, so re-add it once the changes are made.
- **Clearing:** `!wclear` takes approved or bounced PRs/MRs off your list without touching the queue or the leaderboard. List references separated by spaces or commas (`!wclear 55, 67, 88`), give a PR/MR number range (`!wclear 55-100` or `!wclear 55..100`), or use `!wclear all`. In a group it clears that chat's entries; in a direct message with the bot it clears entries from every group you're still in. If a cleared PR/MR is approved or bounced again, it comes back. Reviews assigned to you can't be cleared; finish them with `!wdone` or `!wbounce`. Like `!wme`, the list of what was cleared is a private reply.

Only each PR/MR's latest outcome counts: re-adding one takes it off the list, and bouncing an approved one moves it to "need changes". Approvals and bounces older than 14 days are hidden, since by then the PR/MR was probably merged, closed, or replaced. They're matched to you by your Telegram account, so they appear even without a username. Approvals from before `!wmerged` existed are treated as already merged.

- In groups the reply is a Telegram [ephemeral message](https://core.telegram.org/bots/api#ephemeral-messages-and-commands) (Bot API 10.2+): only you and the bot see it. Telegram may drop it if you are offline or after an app restart, so just send `!wme` again.
- The bot must be a group **admin** to send ephemeral messages. Otherwise it replies with a short notice instead of posting your list publicly. If Telegram ever posts the reply publicly (for example, a self-hosted Bot API server without ephemeral support), the bot deletes it immediately.
- `!wclear` clears before it replies, so if the private reply fails, the public notice still says how many PRs/MRs were cleared (without naming them). Sending `!wclear` again won't show them.
- Your `!wme` or `!wclear` message itself stays visible to the group; only the reply is private. Usage and invalid-reference errors from `!wclear` are still public, since they don't list your PRs/MRs. When `!wmerged` matches several of your approved PRs/MRs, that list is private too.
- In a direct message with the bot, `!wme` lists your reviews from all groups you're still a member of, as a normal reply, under each group's name. If Telegram can't confirm your membership in a group right now (for example, a timeout), the reply says so and you can send `!wme` again.

## Reviewer Cheers (`!wdone` / `!wbounce`)

After `!wdone` or `!wbounce`, the reviewer gets a short, playful message that only they can see (an ephemeral message, like `!wme`). Nothing changes for the rest of the chat.

The first line states the facts: the review, and for `!wdone` how long it waited in the queue since `!wadd`, or for `!wbounce` how many times that PR/MR has been bounced in this chat. Commands covering several reviews show the count, plus the fastest approval or the most-bounced PR/MR. The second line is a quip in a mix of English, Uzbek and Russian:

| Situation | Tier | Example |
|-----------|------|---------|
| `!wdone` under 10 minutes | ⚡ | "Is your real name Barry Allen? ⚡" |
| `!wdone` under 1 hour | 🚀 | "Спидран ревью засчитан 🏁" |
| `!wdone` under 4 hours | 💪 | "Hech kim eslatmasdan oldin bajardingiz. Hurmat!" |
| `!wdone` under 24 hours | 🐢 | "Хорошее ревью как плов — требует времени 🍚" |
| `!wdone` after a day or more | 🦖 | "Archaeologists have officially carbon-dated this PR 🦖" |
| First `!wbounce` of a PR/MR | 🛡️ | "You shall not pass! 🧙 Quality gate held." |
| Second `!wbounce` | 🥊 | "2-raund! Jang! 🥊" |
| Third or later `!wbounce` | 🔁 | "Раунд 3... Этот PR уже как сериал 📺 Может, созвон на 5 минут?" |

- **Free by default:** quips are mixed from a library of about 280 lines in `cheer_lines.py`: an opener, a tier line, a combo line for three or more reviews, and a closer, each in any of the three languages. Many lines have fill-ins, such as "Faster than {a cheetah on espresso / gossip in the office kitchen / …}", so the same line reads differently each time. That gives 12,000–110,000 different messages per tier, and a person doesn't get the same tier line again within their last 12 cheers (kept in memory until restart). To add lines, edit `cheer_lines.py`; the tests check every line for placeholders, slot languages, and Russian number agreement.
- **Optional, paid: Claude-written quips.** With `ANTHROPIC_API_KEY` set, Claude writes every quip from scratch instead. This is billed per message by the Anthropic API. Claude only gets the tier, the wait or bounce round, the number of reviews, and the person's last few quips (so it avoids them). It never gets names, chats, or PR/MR links. It uses the smallest model, Claude Haiku 5.5, at low effort with at most 1,024 tokens per quip, so each cheer costs a fraction of a cent. If Claude doesn't answer within 8 seconds, errors, or declines, the bot falls back to the free library without retrying. Set `CHEERS_MODEL` to use a bigger model, or `CHEERS_AI=off` to keep the key but use the library.
- Reviewing your own PR/MR earns no cheer, using the same rules as the leaderboard (including entries saved under an old username). Anonymous admins get none either. Bounce rounds only count reviewers sending the PR/MR back, not its author bouncing it themselves.
- Cheers are sent in the background after the public "Removed …" reply, so a slow Claude response never holds up the bot. If the private message can't be delivered (for example, the bot isn't a group admin), the cheer is dropped and only logged. The bot posts no notice in the chat. If the Bot API server posts a private message publicly (for example, a self-hosted server without ephemeral support), the bot deletes it and stops sending cheers until it restarts, so later commands don't flash public messages. If it can't delete that message, it asks the chat to.
- Queue time is wall-clock time, so a PR/MR added on Friday evening and reviewed on Monday counts as slow. Slow reviews are teased gently, never shamed.

### Empty queue

Every public message about an empty queue has a funny line and ends with a call to action, such as "👉 Got a PR waiting? Feed the queue: `!wadd <PR/MR link>`". There are three of them:

- **The last review leaves the queue** (`!wdone` or `!wbounce`): the reply celebrates who closed it.

  ```
  Removed repo/55 (added by @bob)

  🎉 Queue is empty! @alice closed the last one.
  Reviewers 1, queue 0. ⚽
  👉 Следующий PR, прошу: !wadd <PR/MR link>
  ```

  To keep quiet chats from getting one after every review, a chat is congratulated at most once an hour (tracked in memory until restart).
- **`!wdone` or `!wbounce` when the queue is already empty**: instead of "not found", the bot says there's nothing to mark done (or send back) and nudges the chat to add PRs. Usage and invalid-reference errors are still shown as before.

  ```
  📭 The queue is empty, so there's nothing to mark done.
  404: ревью не найдено 🔍
  👉 Birinchi bo'lib navbatga turing: !wadd <PR/MR link>
  ```

- **`!w` on an empty queue**: "📭 No tasks in the queue." plus a funny line and a call to action.

These lines come only from the free library in `cheer_lines.py` (`EMPTY_QUEUE`, `NOTHING_TO_REVIEW`, `CALL_TO_ACTION`), never Claude, and a chat doesn't get the same line again within its last 12.

## Notes

- **Task Assignment**: Use `!wassign` to change assignees - this replaces all existing assignees with the new ones
- **Task Removal**: When a task is removed, all its assignees are automatically cleaned up
- **Migration**: Existing single-assignee tasks are automatically migrated to support multiple assignees on first run
- **Completion History**: Review outcomes and identities are archived in the same transaction that removes a review and its assignees
