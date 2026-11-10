"""
Prompt storage and management.
CRUD operations for system prompts stored in JSON.
"""
import asyncio
import json
import aiofiles
from pathlib import Path
from typing import Optional
from loguru import logger

from config import PROMPTS_FILE

# Module-level lock prevents concurrent read-modify-write corruption
_prompts_lock = asyncio.Lock()


async def ensure_prompts_file():
    """Ensure prompts file exists with defaults."""
    if not PROMPTS_FILE.exists():
        PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        default_prompts = {
            "default": {
                "name": "default",
                "description": "Default helpful assistant",
                "content": "You are a helpful AI phone assistant. Be conversational and concise."
            }
        }
        async with aiofiles.open(PROMPTS_FILE, "w") as f:
            await f.write(json.dumps(default_prompts, indent=2))


async def load_prompts() -> dict:
    """Load all prompts from storage (locked to prevent reading mid-write)."""
    await ensure_prompts_file()
    async with _prompts_lock:
        async with aiofiles.open(PROMPTS_FILE, "r") as f:
            raw = await f.read()
            return json.loads(raw)


async def save_prompts(prompts: dict):
    """Save prompts to storage (must be called under _prompts_lock)."""
    async with aiofiles.open(PROMPTS_FILE, "w") as f:
        await f.write(json.dumps(prompts, indent=2))


async def _load_unlocked() -> dict:
    """Internal load — caller must already hold _prompts_lock."""
    await ensure_prompts_file()
    async with aiofiles.open(PROMPTS_FILE, "r") as f:
        raw = await f.read()
        return json.loads(raw)


async def get_prompt(name: str) -> Optional[dict]:
    """Get a specific prompt by name."""
    prompts = await load_prompts()
    return prompts.get(name)


async def list_prompts() -> list[dict]:
    """List all available prompts."""
    prompts = await load_prompts()
    return list(prompts.values())


async def add_prompt(name: str, description: str, content: str) -> bool:
    """Add a new prompt. Returns False if already exists."""
    async with _prompts_lock:
        prompts = await _load_unlocked()
        if name in prompts:
            return False

        prompts[name] = {
            "name": name,
            "description": description,
            "content": content
        }
        await save_prompts(prompts)
        logger.info(f"Added prompt: {name}")
        return True


async def update_prompt(name: str, content: str, description: Optional[str] = None) -> bool:
    """Update an existing prompt. Returns False if not found."""
    async with _prompts_lock:
        prompts = await _load_unlocked()
        if name not in prompts:
            return False

        prompts[name]["content"] = content
        if description:
            prompts[name]["description"] = description

        await save_prompts(prompts)
        logger.info(f"Updated prompt: {name}")
        return True


async def delete_prompt(name: str) -> bool:
    """Delete a prompt. Returns False if not found or if it's 'default'."""
    if name == "default":
        return False  # Can't delete default

    async with _prompts_lock:
        prompts = await _load_unlocked()
        if name not in prompts:
            return False

        del prompts[name]
        await save_prompts(prompts)
        logger.info(f"Deleted prompt: {name}")
        return True


async def get_prompt_names() -> list[str]:
    """Get list of all prompt names for autocomplete."""
    prompts = await load_prompts()
    return list(prompts.keys())

