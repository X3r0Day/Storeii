from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from Storeii.codec import (
    DEFAULT_VIDEO_EXTENSION,
    StoreiiError,
    decode_video_to_file,
    encode_file_to_video,
)


def _format_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _video_name_for(filename: str) -> str:
    stem = Path(filename).stem or "encoded"
    return f"{stem}{DEFAULT_VIDEO_EXTENSION}"


class StoreiiBot(discord.Client):
    def __init__(self, *, guild_id: int | None = None) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            return

        await self.tree.sync()


def _check_send_size(file_path: Path, limit: int) -> None:
    file_size = file_path.stat().st_size
    if file_size > limit:
        raise StoreiiError(
            f"The generated file is {_format_bytes(file_size)}, which exceeds "
            f"Discord's upload limit of {_format_bytes(limit)} for this chat."
        )


def build_bot(*, guild_id: int | None = None) -> StoreiiBot:
    bot = StoreiiBot(guild_id=guild_id)

    @bot.tree.command(name="encode", description="Turn an uploaded file into a Storeii video.")
    @app_commands.describe(file="The file to convert into a video.")
    async def encode(interaction: discord.Interaction, file: discord.Attachment) -> None:
        await interaction.response.defer(thinking=True)

        try:
            with tempfile.TemporaryDirectory(prefix="storeii-encode-") as tmpdir:
                tmpdir_path = Path(tmpdir)
                input_path = tmpdir_path / Path(file.filename).name
                output_path = tmpdir_path / _video_name_for(file.filename)

                await file.save(input_path)
                result = await asyncio.to_thread(encode_file_to_video, input_path, output_path)
                _check_send_size(result.output_path, interaction.filesize_limit)

                await interaction.followup.send(
                    content=(
                        f"Encoded `{result.input_name}` into `{result.output_path.name}` "
                        f"({result.frame_count} frames, {_format_bytes(result.video_bytes)})."
                    ),
                    file=discord.File(result.output_path, filename=result.output_path.name),
                )
        except Exception as exc:
            message = str(exc) if isinstance(exc, StoreiiError) else f"{type(exc).__name__}: {exc}"
            await interaction.followup.send(f"Encode failed: {message}")

    @bot.tree.command(name="decode", description="Recover the original file from a Storeii video.")
    @app_commands.describe(file="The Storeii video to decode.")
    async def decode(interaction: discord.Interaction, file: discord.Attachment) -> None:
        await interaction.response.defer(thinking=True)

        try:
            with tempfile.TemporaryDirectory(prefix="storeii-decode-") as tmpdir:
                tmpdir_path = Path(tmpdir)
                input_path = tmpdir_path / Path(file.filename).name

                await file.save(input_path)
                result = await asyncio.to_thread(decode_video_to_file, input_path, tmpdir_path)
                _check_send_size(result.output_path, interaction.filesize_limit)

                note = " Legacy video detected; filename metadata was not available." if result.legacy_format else ""
                await interaction.followup.send(
                    content=(
                        f"Decoded `{file.filename}` into `{result.output_path.name}` "
                        f"({_format_bytes(result.recovered_bytes)}).{note}"
                    ),
                    file=discord.File(result.output_path, filename=result.output_path.name),
                )
        except Exception as exc:
            message = str(exc) if isinstance(exc, StoreiiError) else f"{type(exc).__name__}: {exc}"
            await interaction.followup.send(f"Decode failed: {message}")

    return bot


def run_bot(token: str, *, guild_id: int | None = None) -> None:
    if not token:
        raise StoreiiError("DISCORD_BOT_TOKEN is required.")

    bot = build_bot(guild_id=guild_id)
    bot.run(token)


def run_bot_from_env() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw else None
    run_bot(token, guild_id=guild_id)
