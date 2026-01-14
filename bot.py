import os
import sys
import discord
from discord.ext import commands, tasks
import asyncio
import logging
import random

# ------------------------------------------------------------------
# LOGGING (same style as your CDA Admin)
# ------------------------------------------------------------------

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_errors.log")
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s:%(levelname)s:%(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# ------------------------------------------------------------------
# TOKEN
# ------------------------------------------------------------------

TOKEN = ""

# ------------------------------------------------------------------
# BOT SETUP
# ------------------------------------------------------------------

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="noah ", intents=intents, help_command=None)

# ------------------------------------------------------------------
# COG DISCOVERY / LOADING (from ./COGS)
# ------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COGS_DIR = os.path.join(BASE_DIR, "COGS")

# Ensure imports like "COGS.SomeCog" work regardless of cwd
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

def discover_extensions():
    if not os.path.isdir(COGS_DIR):
        raise FileNotFoundError(f"COGS folder not found: {COGS_DIR}")

    return [
        file[:-3] for file in os.listdir(COGS_DIR)
        if file.endswith(".py") and file != "__init__.py" and not file.startswith("_")
    ]

async def load_cogs():
    extensions = discover_extensions()
    for extension in extensions:
        try:
            await bot.load_extension(f"COGS.{extension}")
            print(f"[LOADED] - COGS.{extension}")
        except Exception as e:
            logging.error(f"Failed to load cog COGS.{extension}: {e}")
            print(f"--- !!! [FAILED] !!! --- - COGS.{extension}: {e}")
    print("All Cogs Loaded")

# ------------------------------------------------------------------
# HELP COMMAND (same behaviour as your CDA Admin file)
# ------------------------------------------------------------------

@bot.command(name="help")
async def custom_help(ctx):
    if ctx.author.id != 298121351871594497:
        embed = discord.Embed(
            title="Support",
            description="Message this bot, and a message will be sent to Noah's Discord Server.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed, delete_after=10)
        return

    cog_commands = {}
    for command in bot.commands:
        if command.hidden:
            continue
        cog_name = command.cog_name or "Uncategorized"
        cog_commands.setdefault(cog_name, []).append(command)

    embeds = []
    for cog_name, commands_list in cog_commands.items():
        embed = discord.Embed(
            title=f"Help - {cog_name}",
            description=f"Commands in the `{cog_name}` category",
            color=discord.Color.blue()
        )
        for cmd in commands_list:
            embed.add_field(
                name=f"`{ctx.prefix}{cmd.name}`",
                value=cmd.help or "No description provided.",
                inline=False
            )
        embeds.append(embed)

    if not embeds:
        await ctx.send("No commands available.", delete_after=5)
        return

    current_page = 0
    message = await ctx.send(embed=embeds[current_page])
    reactions = ["\u2B05\uFE0F", "\u27A1\uFE0F"]  # ⬅️ ➡️

    for reaction in reactions:
        await message.add_reaction(reaction)

    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) in reactions
            and reaction.message.id == message.id
        )

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=30.0, check=check)
            if str(reaction.emoji) == "\u2B05\uFE0F":
                current_page = (current_page - 1) % len(embeds)
            elif str(reaction.emoji) == "\u27A1\uFE0F":
                current_page = (current_page + 1) % len(embeds)

            await message.edit(embed=embeds[current_page])
            await message.remove_reaction(reaction.emoji, user)

        except asyncio.TimeoutError:
            try:
                await message.clear_reactions()
            except Exception as e:
                logging.error(f"Failed to clear reactions: {e}")
            break

# ------------------------------------------------------------------
# LOAD / UNLOAD / RELOAD COMMANDS
# ------------------------------------------------------------------

@bot.command(name="load")
@commands.is_owner()
async def load(ctx, extension: str):
    try:
        ext = extension if extension.startswith("COGS.") else f"COGS.{extension}"
        await bot.load_extension(ext)
        await ctx.send(f"Loaded `{ext}` successfully.", delete_after=2.5)
    except Exception as e:
        logging.error(f"Failed to load cog {extension}: {e}")
        await ctx.send(f"Failed to load `{extension}`: {e}", delete_after=2.5)

@bot.command(name="unload")
@commands.is_owner()
async def unload(ctx, extension: str):
    try:
        ext = extension if extension.startswith("COGS.") else f"COGS.{extension}"
        await bot.unload_extension(ext)
        await ctx.send(f"Unloaded `{ext}` successfully.", delete_after=2.5)
    except Exception as e:
        logging.error(f"Failed to unload cog {extension}: {e}")
        await ctx.send(f"Failed to unload `{extension}`: {e}", delete_after=2.5)

@bot.command(name="rc")
@commands.is_owner()
async def reload(ctx, extension: str):
    try:
        ext = extension if extension.startswith("COGS.") else f"COGS.{extension}"
        await bot.reload_extension(ext)
        await ctx.send(f"Reloaded `{ext}` successfully.", delete_after=2.5)
    except Exception as e:
        logging.error(f"Failed to reload cog {extension}: {e}")
        await ctx.send(f"Failed to reload `{extension}`: {e}", delete_after=2.5)

@bot.command(name="reload")
@commands.is_owner()
async def reload_all(ctx):
    try:
        await ctx.message.delete()
        extensions = discover_extensions()
        for extension in extensions:
            await asyncio.sleep(1)
            await bot.reload_extension(f"COGS.{extension}")
        await ctx.send("All cogs reloaded successfully.", delete_after=2.5)
    except Exception as e:
        logging.error(f"Failed to reload all cogs: {e}")
        await ctx.send(f"Failed to reload cogs: {e}", delete_after=2.5)

# ------------------------------------------------------------------
# RESTART / STOP / SYNC
# ------------------------------------------------------------------

@bot.command(name="restart")
@commands.is_owner()
async def restart(ctx):
    try:
        await ctx.send("Restarting the bot... Please wait!", delete_after=2.5)
        print("Bot is restarting...")
        await bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logging.error(f"Failed to restart the bot: {e}")
        await ctx.send(f"Failed to restart the bot: {e}", delete_after=5)

@bot.command(name="sync")
@commands.is_owner()
async def sync(ctx):
    await bot.tree.sync()

@bot.command(name="stop")
@commands.is_owner()
async def stop(ctx):
    await bot.close()

# ------------------------------------------------------------------
# STATUS LOOP (relative to bot folder)
# ------------------------------------------------------------------

def load_statuses(file_path=None):
    file_path = file_path or os.path.join(BASE_DIR, "statuses.txt")
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            statuses = [line.strip() for line in file if line.strip()]
            if not statuses:
                raise ValueError("Status file is empty.")
            return statuses
    except Exception as e:
        logging.error(f"Error loading statuses: {e}")
        return ["Default status message."]

@tasks.loop(minutes=0.25)
async def update_status():
    statuses = load_statuses()
    current_status = discord.Activity(
        type=discord.ActivityType.watching,
        name=random.choice(statuses)
    )
    await bot.change_presence(activity=current_status)

# ------------------------------------------------------------------
# ERROR LOGGING HOOKS
# ------------------------------------------------------------------

@bot.event
async def on_error(event_method, *args, **kwargs):
    logging.error(f"Unhandled exception in event: {event_method}", exc_info=True)

@bot.event
async def on_command_error(ctx, error):
    logging.error(f"Command error: {error}", exc_info=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.CheckFailure):
        return
    logging.error(f"Unhandled app command error: {error}", exc_info=True)

# ------------------------------------------------------------------
# READY
# ------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

    try:
        await load_cogs()
    except Exception as e:
        logging.error(f"Failed during load_cogs(): {e}", exc_info=True)

    if not update_status.is_running():
        update_status.start()
    print("Status update task started.")

    for command in bot.tree.walk_commands():
        print(f"Command: {command.name} (Group: {command.parent})")

# ------------------------------------------------------------------
# RUN
# ------------------------------------------------------------------

bot.run(TOKEN)
