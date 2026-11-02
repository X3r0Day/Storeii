# Storeii

Storeii turns any file into a video and can decode that video back into the original file.

Discord bot commands:

- `/encode` takes an uploaded file and returns a Storeii video
- `/decode` takes a Storeii video and returns the original file

## Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the Discord Bot

Set your bot token:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
```

Optional: set a guild ID while testing so slash commands sync immediately in one server.

```bash
export DISCORD_GUILD_ID="123456789012345678"
```

Start the bot:

```bash
python3 main.py
```

When you invite the bot to Discord, include both the `bot` and `applications.commands` scopes. If you do not set `DISCORD_GUILD_ID`, global slash-command sync can take a little while to appear.

## Discord Usage

1. Upload any file with `/encode`
2. The bot returns an `.avi` file
3. Upload that `.avi` with `/decode`
4. The bot returns the original file with its original filename

---

### You can still use it from the CLI itself if you dont have discord!

## CLI Usage

Encode a file:

```bash
python3 Storeii/enc.py ./example.zip ./example.avi
```

Decode a video:

```bash
python3 Storeii/dec.py ./example.avi ./decoded
```

## Notes

- Discord caps uploads at 10MB per server or DM.
- The video is lossless, so the .avi usually ends up bigger than the file you started with.
- `python3 Storeii/selftest.py` runs a quick round-trip check.
