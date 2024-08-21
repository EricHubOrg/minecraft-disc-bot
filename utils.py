import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()
DATA_PATH = "data"
STATIC_PATH = "static"

def read_from_file_sync(filename: str) -> str:
	try:
		with open(filename, "r") as f:
			return f.read()
	except FileNotFoundError:
		print(f"Error: The file {filename} does not exist.")
		return ""

async def read_from_file(filename: str) -> str:
	loop = asyncio.get_event_loop()
	content = await loop.run_in_executor(None, read_from_file_sync, filename)
	return content

def write_to_file_sync(filename: str, content: str):
	with open(filename, "w") as f:
		f.write(content)

async def write_to_file(filename: str, content: str):
	loop = asyncio.get_event_loop()
	await loop.run_in_executor(None, write_to_file_sync, filename, content)

def read_json_sync(filename: str) -> dict:
	try:
		content = read_from_file_sync(filename)
		return json.loads(content)
	except json.JSONDecodeError:
		return {}

async def read_json(filename: str) -> dict:
	return json.loads(await read_from_file(filename))

def write_json_sync(filename: str, content: dict):
	write_to_file_sync(filename, json.dumps(content))

async def write_json(filename: str, content: dict):
	await write_to_file(filename, json.dumps(content))

def extract_json_objects(text: str) -> list[str]:
	"""
	Extract JSON objects from a string.
	"""
	json_objects = []
	open_brackets = 0
	start_index = 0
	for i, char in enumerate(text):
		if char == '{':
			if open_brackets == 0:
				start_index = i
			open_brackets += 1
		elif char == '}':
			open_brackets -= 1
			if open_brackets == 0:
				json_objects.append(text[start_index:i+1])
	return json_objects
