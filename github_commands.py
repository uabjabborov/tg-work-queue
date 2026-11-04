"""Admin-only GitHub App onboarding; sensitive replies always use verified private delivery."""

from html import escape

import httpx
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError

from github_app import GitHubConnectionError, get_github_app, normalize_repository
from presentation import split_messages

USAGE = (
    "<b>GitHub connections</b>\n"
    "<code>!wgithub</code> — list this chat's connections\n"
    "<code>!wgithub connect owner/repo</code> — authorize one repository\n"
    "<code>!wgithub finish</code> — finish GitHub authorization\n"
    "<code>!wgithub cancel</code> — cancel your pending authorization\n"
    "<code>!wgithub disconnect owner/repo</code> — remove this chat's access\n"
    "Connecting a repository lets every member of this chat see its PR metadata, including future members. "
    "You must administer both this Telegram group and the GitHub repository."
)


async def is_chat_admin(update, context) -> bool:
    user = update.effective_user
    if not user or user.is_bot or getattr(update.message, "sender_chat", None):
        return False
    if update.effective_chat.type == ChatType.PRIVATE:
        return True
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except TelegramError:
        return False


async def handle_github_command(update, context, db, reply_privately) -> None:
    if not await is_chat_admin(update, context):
        await update.message.reply_text("Only an identified chat administrator can manage GitHub connections.")
        return
    chat_id, user_id = update.effective_chat.id, update.effective_user.id

    async def reply(text, public_outcome=None):
        return await reply_privately(update, context, [text], verify_private=True,
                                    public_outcome=public_outcome)

    parts = update.message.text.strip().split()
    command = parts[1].lower() if len(parts) > 1 else "status"
    repository = normalize_repository(parts[2]) if len(parts) == 3 else None
    if command in ("connect", "disconnect") and repository is None:
        await reply(USAGE)
        return
    if command not in ("connect", "disconnect") and (command not in ("status", "finish", "cancel") or len(parts) > 2):
        await reply(USAGE)
        return

    if command == "status":
        connections = db.get_github_connections(chat_id)
        blocks = ["<b>GitHub repositories connected to this chat</b>"]
        blocks += [f"<code>{escape(item['repository'])}</code>" for item in connections]
        if not connections:
            blocks.append("No repositories connected. Public PRs still work without a connection.")
        blocks.append(USAGE)
        await reply_privately(update, context, split_messages(blocks), verify_private=True)
        return

    app = get_github_app()
    if command == "disconnect":
        removed = db.disconnect_github(chat_id, repository)
        if app:
            app.cancel_repository(chat_id, repository)
        outcome = "GitHub repository disconnected from this chat." if removed else "That GitHub repository was already disconnected."
        await reply(f"<code>{escape(repository)}</code> is disconnected from this chat. "
                    "Public PRs remain accessible without a connection. Previously sent messages are not removed.",
                    public_outcome=outcome)
        return
    if app is None:
        await reply("The bot operator hasn't configured the GitHub App yet. Public PRs still work.")
        return
    if command == "cancel":
        app.cancel(chat_id, user_id)
        await reply("Your pending GitHub authorization has been cancelled.",
                    public_outcome="Your pending GitHub authorization has been cancelled.")
        return

    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as http:
            if command == "connect":
                install_url, code = await app.start(http, chat_id, user_id, repository)
                chat_name = escape(getattr(update.effective_chat, "title", None) or "this private chat")
                delivered = await reply(
                    f"<b>Connect {escape(repository)} to {chat_name}</b>\n"
                    f"1. <a href=\"{escape(install_url)}\">Install the GitHub App</a> on the selected repository.\n"
                    "2. Open <a href=\"https://github.com/login/device\">GitHub authorization</a> and enter "
                    f"<code>{escape(code)}</code>.\n"
                    "3. Send <code>!wgithub finish</code> here from this same Telegram account.\n"
                    "Use a GitHub account that administers this repository. The code expires in at most 15 minutes.\n"
                    "<b>Finishing grants every member of this chat access to this repository's PR titles and descriptions.</b>"
                )
                if not delivered:
                    app.cancel(chat_id, user_id)
                return

            connection = await app.finish(http, chat_id, user_id)
            # Authorization took network round trips; do not bind if Telegram admin rights changed.
            if not await is_chat_admin(update, context):
                raise GitHubConnectionError("Your chat administrator access could not be verified. The repository was not connected.")
            db.connect_github(chat_id, **connection, connected_by=user_id)
            await reply(f"Connected <code>{escape(connection['repository'])}</code> to this chat. "
                        "Use <code>!wd</code> for the full queue with descriptions, or <code>!wme</code> for your reviews.",
                        public_outcome="GitHub repository connected to this chat. Use <code>!wgithub</code> to check its status.")
    except GitHubConnectionError as error:
        await reply(escape(str(error)))
