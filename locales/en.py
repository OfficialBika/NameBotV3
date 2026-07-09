from config import settings

START_TEXT = f"""🇬🇧 Welcome to {settings.bot_display_name}.

How to use:
• Send photo/video in an approved group for auto lookup.
• Reply to media with /waifu /w .wa .w /name .name for manual lookup."""

FORCE_JOIN_TEXT = """🇬🇧 To use this bot, please join the required channel/group below.

After joining, press ✅ Joined / Check Again."""
GROUP_FORCE_JOIN_TEXT = "🇬🇧 Please open Bot DM to complete force-join verification."
NOT_FOUND = "🇬🇧 Name not found! This media may not exist in the database yet or the source/media did not match."
NO_MEDIA = "🇬🇧 Please reply to a photo/video with the command."
NOT_APPROVED = "🇬🇧 Auto lookup is not enabled in this group. Owner/Sudo must use /gapprove."
JOIN_OK = "🇬🇧 Verification complete. You can use the bot now."
JOIN_FAIL = "🇬🇧 You have not joined all required channels/groups yet. Please join and check again."
BLOCKED_SOURCE = "🇬🇧 This Bot has been blocked."
