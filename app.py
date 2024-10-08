import asyncio
from dotenv import load_dotenv
import os
import json
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_REMOVED
from apscheduler.triggers.cron import CronTrigger
from typing import Any, Literal, Optional, Union
import discord
from discord import Intents, DMChannel, Embed, Color
from discord.ext import commands
from functools import wraps
from utils import read_json, read_from_file, write_to_file, extract_json_objects, parse_log_time, format_timedelta, time_since

load_dotenv()
DATA_PATH = "data"
STATIC_PATH = "static"
USERNAME = os.environ.get("USERNAME", "root")
HOST = os.environ.get("HOST", "localhost")
PORT = os.environ.get("PORT", "22")
SSH = f"ssh {USERNAME}@{HOST} -p {PORT}"
SCRIPTS_PATH = os.environ.get("SCRIPTS_PATH", ".").rstrip("/")
MINECRAFT_LOGS_PATH = os.environ.get("MINECRAFT_LOGS_PATH", ".").rstrip("/")
PLAYERS_DATA_PATH = os.path.join(DATA_PATH, "players.json")
PRIVILEGED_USERS_PATH = os.path.join(DATA_PATH, "privileged_users.txt")
OWNER_ID = os.environ.get("OWNER_ID", None)

cache = {}

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
	description="A bot to manage a self-hosted minecraft server. Start all commands with `%mine`. Type `%mine help` to see available commands.",
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

async def load_privileged_users() -> list[str]:
	content = await read_from_file(PRIVILEGED_USERS_PATH)
	return content.split("\n")

async def save_privileged_users(users: list[str]):
	await write_to_file(PRIVILEGED_USERS_PATH, "\n".join(users))

async def is_owner_user(username: str) -> bool:
	return username == str(await bot.fetch_user(OWNER_ID))

async def is_privileged_user(username: str) -> bool:
	privileged_users = await load_privileged_users()
	user_is_owner = await is_owner_user(username)
	if user_is_owner or username in privileged_users:
		return True
	return False

def owner_command():
	def decorator(func):
		@wraps(func)
		async def wrapper(ctx, *args, **kwargs):
			if await is_owner_user(str(ctx.author)):
				return await func(ctx, *args, **kwargs)
			else:
				await ctx.send("You do not have permission to use this command.")
				return False
		wrapper.is_owner = True
		return wrapper
	return decorator

def privileged_command():
	def decorator(func):
		@wraps(func)
		async def wrapper(ctx, *args, **kwargs):
			if await is_privileged_user(str(ctx.author)):
				return await func(ctx, *args, **kwargs)
			else:
				await ctx.send("You do not have permission to use this command.")
				return False
		wrapper.is_privileged = True
		return wrapper
	return decorator

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
	proc = await asyncio.create_subprocess_shell(
		f"{SSH} -v {command}",
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE
	)
	
	stdout, stderr = await proc.communicate()
	stdout = stdout.decode("utf-8")
	stderr = stderr.decode("utf-8")
	
	# Parse the result
	if proc.returncode != 0:
		errors.append((get_players.__name__, "SSH Command Error when reading usernames", stderr))
		return {}
	try:
		players = json.loads(stdout)
	except json.JSONDecodeError:
		errors.append((get_players.__name__, "Invalid JSON output when reading usernames", stdout))
		return {}
	return players

async def get_player_stats(uuids: Union[str, list], errors: list=[]) -> dict:
	"""
	Get the stats of a player or a list of players.
	"""
	# Build and run the command
	files = " ".join([f"minecraft_server/world/stats/{uuid}.json" for uuid in uuids])
	command = f"cat {files}"
	proc = await asyncio.create_subprocess_shell(
		f"{SSH} -v {command}",
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE
	)
	
	stdout, stderr = await proc.communicate()
	stdout = stdout.decode("utf-8")
	stderr = stderr.decode("utf-8")

	if proc.returncode != 0:
		errors.append((get_player_stats.__name__, "SSH Command Error when reading player stats", stderr))
		return {}
	
	# Find each JSON object in the output
	json_objects = extract_json_objects(stdout)
	
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

	# TODO: finish

async def run_script(
	script: str,
	args: list[str],
	errors: list=[]
) -> bool:
	"""
	Run a script on the server.
	"""
	command = f"bash {SCRIPTS_PATH}/{script}{' ' if args else ''}{' '.join(args)}"
	logging.info(f"Running script: {command}")
	proc = await asyncio.create_subprocess_shell(
		f"{SSH} -v {command}",
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE
	)
	
	stdout, stderr = await proc.communicate()
	stdout = stdout.decode("utf-8")
	stderr = stderr.decode("utf-8")
	
	if proc.returncode != 0:
		errors.append((run_script.__name__, "SSH Command Error when running script", f"Error when running {command}: {stderr}"))
		return False
	return True

async def run_command(ctx: commands.Context, command_arg: str):
	# Run the script
	errors = []
	command_str = f"\"\\\"{command_arg}\\\"\"" # like "\"command\"", otherwise it doesn not work
	success = await run_script("run_mc_command.sh", [command_str], errors)
	user_msg = await ctx.fetch_message(ctx.message.id)
	if not success:
		# Log errors and reply
		error_msg = "Failed to run script."
		log_errors([("command", error_msg, errors)])
		await user_msg.reply(error_msg)
		await user_msg.add_reaction("❌")
	else:
		# React with a checkmark
		await user_msg.add_reaction("✅")

async def read_log_file(file_path: str, errors: list=[]):
	"""
	Read a compressed or not log file.
	"""
	# Read from cache if available
	if file_path in cache:
		return cache[file_path]
	
	# Run ssh command to read file
	command = f"zcat {file_path}" if file_path.endswith(".gz") else f"cat {file_path}"
	proc = await asyncio.create_subprocess_shell(
		f"{SSH} -v {command}",
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE
	)
	
	stdout, stderr = await proc.communicate()
	stdout = stdout.decode("utf-8")
	stderr = stderr.decode("utf-8")

	if proc.returncode != 0:
		errors.append((read_log_file.__name__, "SSH Command Error when reading log file", f"Error when reading {file_path}: {stderr}"))
		return ""
	
	# Cache the result
	cache[file_path] = stdout
	return stdout

async def list_log_files(sort_by: Literal["name", "date"], errors: list=[]):
	"""
	List log files in the MINECRAFT_LOGS_PATH directory, sorted by name or date.
	"""
	# Read from cache if available
	key = "log_files_" + sort_by
	if key in cache:
		return cache[key]
	
	# Run ssh command to list log files
	sort_option = "-t" if sort_by == "date" else ""
	command = f"ls {sort_option} {MINECRAFT_LOGS_PATH}/*.log* | grep -v debug"
	
	proc = await asyncio.create_subprocess_shell(
		f"{SSH} -v {command}",
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE
	)
	
	stdout, stderr = await proc.communicate()
	stdout = stdout.decode("utf-8")
	stderr = stderr.decode("utf-8")

	if proc.returncode != 0:
		errors.append((list_log_files.__name__, "SSH Command Error when listing log files", f"Error: {stderr}"))
		return []
	
	log_files = stdout.strip().split('\n')
	log_files = [file.strip() for file in log_files if file.strip()]
	
	# Cache the result
	cache[key] = log_files
	return log_files

async def search_string_in_logs(string: str, k: int=-1, max_search_lines: int=-1, errors: list=[]) -> tuple[list[str], int]:
	"""
	Search for a string in the log files and return the first k lines that contain it.
	"""
	# Get the log files paths
	list_log_files_errors = []
	log_files_paths = await list_log_files(sort_by="date", errors=list_log_files_errors)
	if not log_files_paths:
		errors.append((search_string_in_logs.__name__, "Failed to list log files", list_log_files_errors))
		return [], -1
	
	# Read the log files one by one until k strings are found
	matched_lines = []
	line_count = 0
	for log_file_path in log_files_paths:
		if (k != -1 and len(matched_lines) >= k) or (max_search_lines != -1 and line_count >= max_search_lines):
			break
		read_log_file_errors = []
		lines = await read_log_file(log_file_path, read_log_file_errors)
		lines = lines.split('\n')
		if read_log_file_errors:
			errors.append((search_string_in_logs.__name__, "Failed to read log file", read_log_file_errors))
			return [], -1
		# check if the string is in the lines
		for line in lines[::-1]:
			line_count += 1
			if string in line:
				matched_lines.append(line)
			if (k != -1 and len(matched_lines) >= k) or (max_search_lines != -1 and line_count >= max_search_lines):
				break
	return matched_lines, line_count

async def last_time_joined(username: str, errors: list=[]) -> (str, str):
	"""
	Get the last time a player joined and left the server.
	"""
	# Search for the "joined the game" pattern
	joined_pattern = f"{username} joined the game"
	search_string_in_logs_errors = []
	joined_lines, joined_line_count = await search_string_in_logs(joined_pattern, k=1, errors=search_string_in_logs_errors)
	if search_string_in_logs_errors:
		errors.append((last_time_joined.__name__, "Error searching for joined pattern", search_string_in_logs_errors))
		return ("", "")
	if not joined_lines:
		errors.append((last_time_joined.__name__, "No joined pattern found", f"No log lines found with pattern: {joined_pattern}"))
		return ("No data", "No data")

	# Extract the joined time
	joined_time = joined_lines[0].split(']')[0][1:-4]

	# Search for the "left the game" pattern until joined_line_count lines are searched
	left_pattern = f"{username} left the game"
	search_string_in_logs_errors = []
	left_lines, _ = await search_string_in_logs(left_pattern, k=1, max_search_lines=joined_line_count, errors=search_string_in_logs_errors)
	if search_string_in_logs_errors:
		errors.append((last_time_joined.__name__, "Error searching for left pattern", search_string_in_logs_errors))
		return (joined_time, "Error")
	if not left_lines:
		return (joined_time, "Still playing")

	# Extract the left time
	left_time = left_lines[0].split(']')[0][1:-4]

	return (joined_time, left_time)


# ========= DISCORD EVENTS ==========

@bot.event
async def on_ready():
	"""
	Start processes when the bot is ready.
	"""
	logging.info(f"We have logged in as {bot.user}")
	scheduler.start()
	scheduler.add_job(daily_update, CronTrigger(hour=0, minute=0))
	if not os.path.exists(PRIVILEGED_USERS_PATH):
		os.makedirs(os.path.dirname(PRIVILEGED_USERS_PATH), exist_ok=True)
		await write_to_file(PRIVILEGED_USERS_PATH, "")

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
		command = mine.get_command(arg0)
		if command:
			embed = Embed(title=command.name, description=command.description, color=color)
			embed.add_field(name="use", value=f"`{command.usage}`")
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
		
		is_privileged = await is_privileged_user(str(ctx.author))
		is_owner = await is_owner_user(str(ctx.author))
		for command in sorted(mine.commands, key=lambda command: command.name):
			if command.name != "help":
				command_is_privileged = getattr(command.callback, "is_privileged", False)
				command_is_owner = getattr(command.callback, "is_owner", False)
				can_execute = not ((command_is_privileged and not is_privileged) or (command_is_owner and not is_owner))
				if can_execute:
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
    brief="Grant privileges to a user.",
    description="Grant privileges to a user.",
    usage="`%mine grant_privileges [username]`"
)
@owner_command()
async def grant_privileges(
	ctx: commands.Context,
	username: str
):
	"""
	Grant privileges to a user.
	"""
	privileged_users = await load_privileged_users()
	if username not in privileged_users:
		privileged_users.append(username)
		await save_privileged_users(privileged_users)
		await ctx.send(f"Granted privileges to {username}.")
	else:
		await ctx.send(f"{username} already has privileges.")


@mine.command(
	brief="Revoke privileges from a user.",
	description="Revoke privileges from a user.",
	usage="`%mine revoke_privileges [username]`"
)
@owner_command()
async def revoke_privileges(
	ctx: commands.Context,
	username: str
):
	"""
	Revoke privileges from a user.
	"""
	privileged_users = await load_privileged_users()
	if username in privileged_users:
		privileged_users.remove(username)
		await save_privileged_users(privileged_users)
		await ctx.send(f"Revoked privileges from {username}.")
	else:
		await ctx.send(f"{username} does not have privileges.")


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

	async with ctx.typing():
		errors = []
		players = await get_players(errors)
		if not players:
			msg = "Failed to get players data."
			log_errors([("get_players", msg, errors)])
			await ctx.send(msg)
			return
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
	async with ctx.typing():
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
			log_errors([(playtime.__name__, msg, get_player_stats_errors)])
			await ctx.send(msg)
			return
		
		# Extract the playtime of the player/s
		playtime_dict = {}
		for uuid, stats in all_players_stats.items():
			playtime_int = stats.get("minecraft:custom", {}).get("minecraft:play_time", 0) // 20 # ticks -> seconds
			playtime_dict[players[uuid]] = playtime_int
		
		if not playtime_dict:
			msg = "No playtime data available."
			log_errors([("playtime", msg, "minecraft:play_time entry not found for any player")])
			await ctx.send(msg)
			return
		
		# Format the playtime string
		playtime_dict = dict(sorted(playtime_dict.items(), key=lambda item: item[1], reverse=True))
		playtime_str = "\n".join([f"`{username}`: {playtime // 3600}h {(playtime % 3600) // 60}min" for username, playtime in playtime_dict.items()])
		await ctx.send(f"Playtime:\n{playtime_str}")


@mine.command(
	brief="Runs a command on the Minecraft server.",
	description="Runs a command on the Minecraft server. Type command inside \"\".",
	usage="`%mine command [\"command\"]`",
)
@owner_command()
async def command(
	ctx: commands.Context,
	command_arg: str
):
	"""
	Runs a command on the server.
	"""
	logging.info(f"command command executed by {ctx.author}")
	async with ctx.typing():
		await run_command(ctx, command_arg)


@mine.command(
	brief="Send a message to players on the Minecraft server.",
	description="Send a message to players on the Minecraft server by running `\say \"message\"`.",
	usage="`%mine say [\"message\"]`",
)
@privileged_command()
async def say(
	ctx: commands.Context,
	message: str
):
	"""
	Send a message to players on the Minecraft server.
	"""
	logging.info(f"say command executed by {ctx.author}")
	async with ctx.typing():
		# Run the script with /say command
		command_str = f"/say {message}"
		await run_command(ctx, command_str)


@mine.command(
	brief="Show the last time players joined and left the server.",
	description="Show the last time players joined and left the server.",
	usage="`%mine last_joined (username)`"
)
async def last_joined(
	ctx: commands.Context,
	username: Optional[str]=None
):
	"""
	Show the last time players joined and left the server.
	"""
	global cache
	logging.info(f"last_joined command executed by {ctx.author}")
	async with ctx.typing():
		message = ""

		if username is None:
			players = await get_players()
			usernames_lst = list(players.values())
		else:
			usernames_lst = [username]

		errors = []
		cache = {}
		last_joined_lst = await asyncio.gather(*(last_time_joined(username, errors) for username in usernames_lst))
		cache = {}
		if errors:
			msg = "Failed to get last joined time."
			log_errors([("last_joined", msg, errors)])
			await ctx.send(msg)
			return

		for username, (last_joined_time, last_left_time) in zip(usernames_lst, last_joined_lst):
			joined_time = parse_log_time(last_joined_time)
			if last_left_time != "Still playing":
				left_time = parse_log_time(last_left_time)
				time_since_joined = time_since(joined_time)
				time_transcurred = format_timedelta(left_time - joined_time)
				message += f"`{username}`: {joined_time} - {left_time} [{time_transcurred}] ({time_since_joined} ago)\n"
			else:
				time_since_joined = time_since(joined_time)
				message += f"`{username}`: {joined_time} - Still playing ({time_since_joined} ago)\n"

		await ctx.send(message) 


if __name__ == "__main__":
	bot.run(os.environ.get("DISCORD_TOKEN"))
