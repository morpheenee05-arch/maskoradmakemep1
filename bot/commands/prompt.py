"""
/prompt commands - Manage system prompts.
Telegram ConversationHandler implementation.
"""
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    ConversationHandler, filters
)
from loguru import logger

from storage.prompts import (
    list_prompts, get_prompt, add_prompt,
    update_prompt, delete_prompt, get_prompt_names
)


# Conversation states for /prompt_add
ADD_NAME, ADD_DESCRIPTION, ADD_CONTENT = range(10, 13)

# Conversation states for /prompt_edit
EDIT_DESCRIPTION, EDIT_CONTENT = range(20, 22)


# ── /prompt_list ──

async def prompt_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all prompts."""
    prompts = await list_prompts()

    if not prompts:
        await update.message.reply_text("📋 No prompts found. Use /prompt\\_add to create one.")
        return

    text = "📋 *Available Prompts*\n━━━━━━━━━━━━━━━━━━\n\n"
    for p in prompts:
        content_preview = p["content"][:80] + "..." if len(p["content"]) > 80 else p["content"]
        text += f"🔹 `{p['name']}` — _{p['description']}_\n"
        text += f"```\n{content_preview}\n```\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ── /prompt_view <name> ──

async def prompt_view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View full prompt content."""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/prompt_view <name>`", parse_mode="Markdown")
        return

    name = context.args[0].lower()
    prompt_data = await get_prompt(name)
    if not prompt_data:
        await update.message.reply_text(f"❌ Prompt `{name}` not found.", parse_mode="Markdown")
        return

    text = (
        f"📝 *Prompt: {name}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📄 _{prompt_data['description']}_\n\n"
        f"*Content:*\n"
        f"```\n{prompt_data['content']}\n```"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /prompt_add conversation ──

async def prompt_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding a new prompt."""
    await update.message.reply_text("📝 *Enter the prompt name* (e.g. `customer_support`):", parse_mode="Markdown")
    return ADD_NAME


async def prompt_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received name, ask for description."""
    name = update.message.text.strip().lower().replace(" ", "_")
    context.user_data["new_prompt_name"] = name
    await update.message.reply_text("📄 *Enter a brief description:*", parse_mode="Markdown")
    return ADD_DESCRIPTION


async def prompt_add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received description, ask for content."""
    context.user_data["new_prompt_description"] = update.message.text.strip()
    await update.message.reply_text(
        "✏️ *Now enter the system prompt content:*\n"
        "(Use `{name}` and `{email}` as placeholders)",
        parse_mode="Markdown"
    )
    return ADD_CONTENT


async def prompt_add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received content, save the prompt."""
    name = context.user_data.pop("new_prompt_name")
    description = context.user_data.pop("new_prompt_description")
    content = update.message.text.strip()

    success = await add_prompt(name, description, content)
    if success:
        await update.message.reply_text(f"✅ Prompt `{name}` created successfully!", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"❌ Prompt `{name}` already exists. Use /prompt\\_edit to modify it.",
            parse_mode="Markdown"
        )

    return ConversationHandler.END


async def prompt_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel prompt creation."""
    for key in ("new_prompt_name", "new_prompt_description"):
        context.user_data.pop(key, None)
    await update.message.reply_text("❌ Prompt creation cancelled.")
    return ConversationHandler.END


# ── /prompt_edit <name> conversation ──

async def prompt_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start editing a prompt."""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/prompt_edit <name>`", parse_mode="Markdown")
        return ConversationHandler.END

    name = context.args[0].lower()
    prompt_data = await get_prompt(name)
    if not prompt_data:
        await update.message.reply_text(f"❌ Prompt `{name}` not found.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["edit_prompt_name"] = name
    await update.message.reply_text(
        f"📄 *Current description:* _{prompt_data['description']}_\n\n"
        f"Enter new description (or send `.` to keep current):",
        parse_mode="Markdown"
    )
    return EDIT_DESCRIPTION


async def prompt_edit_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received new description, ask for content."""
    text = update.message.text.strip()
    if text != ".":
        context.user_data["edit_prompt_description"] = text
    else:
        context.user_data["edit_prompt_description"] = None

    name = context.user_data["edit_prompt_name"]
    prompt_data = await get_prompt(name)

    await update.message.reply_text(
        f"✏️ *Current content:*\n```\n{prompt_data['content'][:200]}\n```\n\n"
        f"Enter new content (or send `.` to keep current):",
        parse_mode="Markdown"
    )
    return EDIT_CONTENT


async def prompt_edit_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received new content, save."""
    name = context.user_data.pop("edit_prompt_name")
    new_description = context.user_data.pop("edit_prompt_description", None)

    text = update.message.text.strip()
    if text == ".":
        # Keep current content — just update description if changed
        prompt_data = await get_prompt(name)
        text = prompt_data["content"]

    success = await update_prompt(name, text, new_description)
    if success:
        await update.message.reply_text(f"✅ Prompt `{name}` updated!", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Failed to update prompt `{name}`.", parse_mode="Markdown")

    return ConversationHandler.END


async def prompt_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel editing."""
    for key in ("edit_prompt_name", "edit_prompt_description"):
        context.user_data.pop(key, None)
    await update.message.reply_text("❌ Prompt editing cancelled.")
    return ConversationHandler.END


# ── /prompt_delete <name> ──

async def prompt_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a prompt."""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/prompt_delete <name>`", parse_mode="Markdown")
        return

    name = context.args[0].lower()

    if name == "default":
        await update.message.reply_text("❌ Cannot delete the default prompt.")
        return

    success = await delete_prompt(name)
    if success:
        await update.message.reply_text(f"🗑️ Prompt `{name}` deleted.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Prompt `{name}` not found.", parse_mode="Markdown")


# ── Register handlers ──

def register_prompt_handlers(application):
    """Register all prompt-related handlers."""
    # Simple commands
    application.add_handler(CommandHandler("prompt_list", prompt_list_command))
    application.add_handler(CommandHandler("prompt_view", prompt_view_command))
    application.add_handler(CommandHandler("prompt_delete", prompt_delete_command))

    # Add conversation
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("prompt_add", prompt_add_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_add_name)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_add_description)],
            ADD_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_add_content)],
        },
        fallbacks=[CommandHandler("cancel", prompt_add_cancel)],
    )
    application.add_handler(add_conv)

    # Edit conversation
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("prompt_edit", prompt_edit_start)],
        states={
            EDIT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_edit_description)],
            EDIT_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_edit_content)],
        },
        fallbacks=[CommandHandler("cancel", prompt_edit_cancel)],
    )
    application.add_handler(edit_conv)

    logger.info("Registered prompt handlers")
