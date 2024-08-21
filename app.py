import asyncio
import re
import subprocess
from dotenv import load_dotenv
import os
import json
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_REMOVED
from apscheduler.triggers.cron import CronTrigger
from typing import Any, Optional, Union
import discord
from discord import Intents, DMChannel, Embed, Color
from discord.ext import commands, tasks
from utils import read_json, write_json, extract_json_objects

load_dotenv()
DATA_PATH = "data"
STATIC_PATH = "static"
USERNAME = os.environ.get("USERNAME", "root")
HOST = os.environ.get("HOST", "localhost")
PORT = os.environ.get("PORT", "22")
SSH = f"ssh {USERNAME}@{HOST} -p {PORT}"
PLAYERS_DATA_PATH = os.path.join(DATA_PATH, "players.json")

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


# ========= FUNCTIONS ==========

def build_errors_string(errors: list, indent: int = 0):
	"""
	Build a string with the errors recursively, adding indentation for each level.
	"""
	string = ""
	indentation = "\t" * indent
	for (function, message, error) in errors:
		string += f"{indentation}{function}: \"{message}\"\n"
		if isinstance(error, str):
			if error:
				string += f"{indentation}{error}\n"
		else:
			string += build_errors_string(error, indent + 1)
	return string

def log_errors(errors: list):
	"""
	Log the errors recursively.
	"""
	logging.error(build_errors_string(errors))

async def get_players(errors: list=[]) -> dict:
	"""
	Get the players usernames and uuids from the server.
	"""
	# Run the command
	command = "cat minecraft_server/usernamecache.json"
	result = subprocess.run(f"{SSH} -v {command}", shell=True, capture_output=True, text=True)
	
	# Parse the result
	if result.returncode != 0:
		errors.append((get_players.__name__, "SSH Command Error when reading usernames", result.stderr))
		return {}
	try:
		players = json.loads(result.stdout)
	except json.JSONDecodeError:
		errors.append((get_players.__name__, "Invalid JSON output when reading usernames", result.stdout))
		return {}
	return players

async def get_player_stats(uuids: Union[str, list], errors: list=[]) -> dict:
	"""
	Get the stats of a player or a list of players.
	"""
	# Build and run the command
	files = " ".join([f"minecraft_server/world/stats/{uuid}.json" for uuid in uuids])
	command = f"cat {files}"
	result = subprocess.run(f"{SSH} -v {command}", shell=True, capture_output=True, text=True)
	if result.returncode != 0:
		errors.append((get_player_stats.__name__, "SSH Command Error when reading player stats", result.stderr))
		return {}
	
	# Find each JSON object in the output
	json_objects = extract_json_objects(result.stdout)
	
	try:
		players_stats: list[dict] = [json.loads(stats) for stats in json_objects]
	except json.JSONDecodeError as e:
		errors.append((get_player_stats.__name__, "Invalid JSON output when reading player stats", str(e)))
		return {}
	
	# Extract stats for each player
	stats_dict = {}
	for uuid, player_stats in zip(uuids, players_stats):
		stats_dict[uuid] = player_stats.get("stats", {})
	
	return stats_dict

async def update_players_data(errors: list=[]):
	"""
	Update the players data.
	"""
	# Read the players data
	players_data = await read_json(PLAYERS_DATA_PATH)
	old_players_data: dict = players_data.get("players", {})
	players_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	get_players_errors = []
	players = await get_players(get_players_errors)
	if not players:
		errors.append((update_players_data.__name__, "Failed to get players data", get_players_errors))
		return
	
	# Update the players data
	uuids, usernames = zip(*players.items())
	players_data["players"] = {}

	get_player_stats_errors = []
	all_players_stats = await get_player_stats(list(uuids), get_player_stats_errors)
	
	if not all_players_stats:
		errors.append((update_players_data.__name__, "Failed to get player stats", get_player_stats_errors))
		return
	
	for uuid, username in zip(uuids, usernames):
		player_data = old_players_data.get(uuid, {})
		player_stats = all_players_stats.get(uuid, {})
		player_data["username"] = username
		player_data["playtime"] = player_stats.get("minecraft:custom", {}).get("minecraft:play_time", 0) // 20 # ticks -> seconds
		players_data["players"][uuid] = player_data


# ========= DISCORD EVENTS ==========

@bot.event
async def on_ready():
	"""
	Start processes when the bot is ready.
	"""
	logging.info(f"We have logged in as {bot.user}")
	scheduler.start()
	scheduler.add_job(daily_update, CronTrigger(hour=0, minute=0))

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

async def daily_update():
	"""
	Run every day updating the tracked server data.
	"""
	logging.info("Running daily update...")
	errors = []

	await update_players_data(errors)

	logging.info("Daily update complete")


# ========= DISCORD COMMANDS ==========

@bot.group(
	brief="Manage the minecraft server",
	description="Manage the minecraft server",
	usage="`%mine [command]`"
)
async def mine(
	ctx: commands.Context
):
	"""
	Group of commands to manage the minecraft server.
	"""
	if ctx.invoked_subcommand is None:
		await ctx.send("Invalid command. Use `%mine help` to see available commands.")
		return

# Create a new help command
bot.remove_command("help") # Remove the default
@mine.command(
	brief="Shows this help message.",
	description="Shows a list of available commands.",
	usage="`%mine help (command)`"
)
async def help(
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


@mine.command(
		brief="Brief description of the `mine test` command.",
		description="Detailed description of the `mine test` command.",
		usage="`%mine test [arg1] (arg2)`"
)
async def test(
	ctx: commands.Context,
	arg0: str,
	arg1: str=0
):
	logging.info(f"Test command executed by {ctx.author}")
	await ctx.send(f"Hello there! {arg0} + {arg1} = {arg0 + arg1}")


@mine.command(
	brief="List all players on the server.",
	description="List all players on the server.",
	usage="`%mine list_players`"
)
async def list_players(
	ctx: commands.Context
):
	"""
	List all players on the server.
	"""
	logging.info(f"list_players command executed by {ctx.author}")

	players = await get_players()
	usernames = [f"`{players[uuid]}`" for uuid in players]
	await ctx.send(f"Players on the server: {', '.join(usernames)}")


@mine.command(
	brief="Show the playtime of a player.",
	description="Show the playtime of a player.",
	usage="`%mine playtime (username)`"
)
async def playtime(
	ctx: commands.Context,
	tgt_username: Optional[str]=None
):
	"""
	Show the playtime of a player.
	"""
	logging.info(f"playtime command executed by {ctx.author}")
	players = await get_players()
	
	if tgt_username is None:
		# Get all players
		players_lst = [uuid for uuid, username_ in players.items()]
	else:
		# Get the player/s with the given username
		players_lst = [uuid for uuid, username_ in players.items() if username_ == tgt_username] # can be multiple
	if not players_lst:
		# No player with the given username found
		msg = f"No player with username `{tgt_username}` found."
		logging.info(msg)
		await ctx.send(msg)
		return
	
	# Get the playtime of the player/s
	get_player_stats_errors = []
	all_players_stats = await get_player_stats(players_lst, get_player_stats_errors)
	if not all_players_stats:
		msg = "Failed to get player stats."
		log_errors([(get_player_stats.__name__, msg, get_player_stats_errors)])
		await ctx.send(msg)
		return
	
	# Extract the playtime of the player/s
	playtime_dict = {}
	for uuid, stats in all_players_stats.items():
		playtime = stats.get("minecraft:custom", {}).get("minecraft:play_time", 0) // 20 # ticks -> seconds
		playtime_dict[players[uuid]] = playtime
	
	if not playtime_dict:
		msg = "No playtime data available."
		log_errors([(playtime.__name__, msg, "minecraft:play_time entry not found for any player")])
		await ctx.send(msg)
		return
	
	# Format the playtime string
	playtime_str = "\n".join([f"`{username}`: {playtime} seconds" for username, playtime in playtime_dict.items()])
	await ctx.send(f"Playtime:\n{playtime_str}")


if __name__ == "__main__":
	bot.run(os.environ.get("DISCORD_TOKEN"))