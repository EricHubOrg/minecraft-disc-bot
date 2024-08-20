import asyncio
import subprocess
from dotenv import load_dotenv
import os
import json
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_REMOVED
from typing import Any, Optional
import discord
from discord import Intents, DMChannel, Embed, Color
from discord.ext import commands, tasks

load_dotenv()
STATIC_PATH = "static"
USERNAME = os.environ.get("USERNAME", "root")
HOST = os.environ.get("HOST", "localhost")
PORT = os.environ.get("PORT", "22")

# Set up logging
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s %(levelname)s     %(message)s',
	datefmt="%Y-%m-%d %H:%M:%S"
)

# Set up the bot
intents = Intents.default()
intents.message_content = True
intents.messages = True
intents.reactions = True
bot = commands.Bot(
	command_prefix="%",
	description="A bot to manage a self-hosted minecraft server",
	intents=intents,
)

# Set up the scheduler
scheduler = AsyncIOScheduler()
async def on_job_removed(event: Any):
	"""
	Remove the job from the scheduler when it is removed.
	"""
	job = scheduler.get_job(event.job_id)
	if not job:
		return
scheduler.add_listener(on_job_removed, EVENT_JOB_REMOVED)


# ========= DISCORD EVENTS ==========

@bot.event
async def on_ready():
	"""
	Start processes when the bot is ready.
	"""
	logging.info(f"We have logged in as {bot.user}")
	keep_alive.start()
	scheduler.start()

@bot.event
async def on_message(message: discord.Message):
	"""
	Process messages sent by users.
	"""
	if message.author.bot:
		# ignore messages from other bots
		return

	if isinstance(message.channel, DMChannel) or message.guild is None:
		# ignore private messages and messages outside of a server
		await message.channel.send("Sorry you can't talk to me in private")
		return

	# process commands normally
	await bot.process_commands(message)

@tasks.loop(minutes=1.0)
async def keep_alive():
	logging.info("Life signal")

@keep_alive.before_loop
async def before_keep_alive():
	await bot.wait_until_ready()


# ========= DISCORD COMMANDS ==========

# Create a new help command
bot.remove_command("help") # Remove the default
@bot.command(
	brief="Shows this help message.",
	description="Shows a list of available commands.",
	usage="`%help_mine (command)`"
)
async def help_mine(
	ctx: commands.Context,
	arg0: str=None
):
	"""
	Displays information about the available commands.
	"""
	logging.info(f"Help command executed by {ctx.author}")
	color = Color.blue()
	if arg0:
		# Give info about the command
		command = bot.get_command(arg0)
		if command:
			embed = Embed(title=command.name, description=command.description, color=color)
			embed.add_field(name="us", value=f"`{command.usage}`")
			await ctx.send(embed=embed)
		else:
			await ctx.send(f"There is no command with name `{arg0}`.")
	else:
		# List all commands
		filename = "minecraft.png"
		file = discord.File(os.path.join(STATIC_PATH, filename), filename=filename)
		embed = Embed(title="Minecraft Bot", description=bot.description, color=color)
		embed.set_thumbnail(url=f"attachment://{filename}")
		embed.set_author(name="Eric Lopez", url="https://github.com/Pikurrot", icon_url="https://avatars.githubusercontent.com/u/90217719?v=4")
		for command in sorted(bot.commands, key=lambda command: command.name):
			if command.name != "help_mine":
				embed.add_field(name=command.name, value=command.brief, inline=False)
		await ctx.send(embed=embed, file=file)


@bot.command(
		brief="Brief description of the `test_mine` command.",
		description="Detailed description of the `test_mine` command.",
		usage="`%test_mine [arg1] (arg2)`"
)
async def test_mine(
	ctx: commands.Context,
	arg0: str,
	arg1: str=0
):
	logging.info(f"Test command executed by {ctx.author}")
	await ctx.send(f"Hello there! {arg0} + {arg1} = {arg0 + arg1}")


@bot.command(
	brief="List all players on the server.",
	description="List all players on the server.",
	usage="`%list_players`"
)
async def list_players(
	ctx: commands.Context
):
	"""
	List all players on the server.
	"""
	logging.info(f"list_players command executed by {ctx.author}")

	# Run the command
	ssh = f"ssh {USERNAME}@{HOST} -p {PORT}"
	command = "cat minecraft_server/usernamecache.json"
	result = subprocess.run(f"{ssh} -v {command}", shell=True, capture_output=True, text=True)
	
	# Parse the result
	if result.returncode != 0:
		await ctx.send(f"Error: {result.stderr[:100]}")
		print(result.stderr)
		return
	try:
		players = json.loads(result.stdout)
	except json.JSONDecodeError:
		await ctx.send("Error: Invalid JSON output.")
		return
	
	# Send the players
	usernames = [f"`{players[uuid]}`" for uuid in players]
	await ctx.send(f"Players on the server: {usernames}")

if __name__ == "__main__":
	bot.run(os.environ.get("DISCORD_TOKEN"))
