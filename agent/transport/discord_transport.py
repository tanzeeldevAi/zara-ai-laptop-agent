"""Discord transport: the original front end, behind the Transport seam.

Everything Discord-shaped lives here now — the client, message chunking,
button confirmations, the allowlist gate, the back-online announcement and
the connect/retry loop. Behaviour is the pre-Phase-0 behaviour; the session
(agent/session.py) handles each accepted message transport-neutrally.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import discord

from .. import netrecover, voice
from ..config import CONFIG, effective_brain
from . import IncomingMessage

AUDIO_EXTS = (".ogg", ".oga", ".mp3", ".wav", ".m4a", ".webm", ".opus", ".flac", ".aac")


def chunks(text, size):
    """Split text for Discord's 2000-character limit, preferring line breaks."""
    text = text.rstrip()
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        if cut < size // 3:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


class DiscordChannel:
    """What the brains are allowed to do to a Discord channel."""

    def __init__(self, channel, user_id):
        self.channel = channel
        self.user_id = user_id
        self.last_spoken = ""      # last substantive text, used for the voice reply
        self.spoke = False         # set True if the `speak` tool already sent a voice message

    @property
    def channel_id(self):
        return self.channel.id

    @property
    def session_key(self):
        return f"discord:{self.channel.id}"

    def typing(self):
        return self.channel.typing()

    async def send_text(self, text):
        text = str(text)
        if text.strip():
            self.last_spoken = text
        for part in chunks(text, CONFIG.discord_msg_limit):
            try:
                await self.channel.send(part)
            except discord.HTTPException as exc:
                print(f"[discord] send failed: {exc}", file=sys.stderr)

    async def note(self, text):
        try:
            await self.channel.send(f"-# ⚙️ {str(text)[:400]}")
        except discord.HTTPException:
            pass

    async def send_file(self, path, caption=None):
        target = Path(path)
        if not target.exists():
            return
        try:
            await self.channel.send(
                content=(caption or None),
                file=discord.File(str(target), filename=target.name),
            )
        except discord.HTTPException as exc:
            await self.send_text(f"Couldn't upload `{target.name}`: {exc}")

    async def send_voice(self, path, caption=None):
        await self.send_file(path, caption)

    async def confirm(self, tool_name, reason, detail):
        view = ConfirmView(self.user_id)
        embed = discord.Embed(
            title="Confirm before I run this",
            description=f"**{tool_name}** — {reason}",
            colour=discord.Colour.orange(),
        )
        embed.add_field(name="What will run", value=f"```\n{detail[:1000]}\n```", inline=False)
        embed.set_footer(text="Auto-cancels in 3 minutes if you don't answer.")
        try:
            message = await self.channel.send(embed=embed, view=view)
        except discord.HTTPException:
            return False

        approved = await view.wait_for_answer()
        try:
            await message.edit(
                embed=embed.set_footer(text="✅ Approved" if approved else "❌ Cancelled"),
                view=None,
            )
        except discord.HTTPException:
            pass
        return approved


class ConfirmView(discord.ui.View):
    def __init__(self, user_id, timeout=180):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.answer = None
        self._done = asyncio.Event()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your agent.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Run it", style=discord.ButtonStyle.danger)
    async def approve(self, interaction, button):
        self.answer = True
        await interaction.response.defer()
        self._done.set()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def reject(self, interaction, button):
        self.answer = False
        await interaction.response.defer()
        self._done.set()
        self.stop()

    async def on_timeout(self):
        self._done.set()

    async def wait_for_answer(self):
        await self._done.wait()
        return bool(self.answer)


class DiscordClient(discord.Client):
    """Thin Discord shell: gates the sender, packages messages, delegates."""

    def __init__(self, session):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.session = session
        self._net_monitor_started = False   # on_ready fires again on every reconnect

    async def on_ready(self):
        brain = self.session.brain
        try:
            (CONFIG.runtime_dir / "status.txt").write_text(
                f"ONLINE as {self.user} (id {self.user.id})\n"
                f"allowed_users={sorted(CONFIG.allowed_users)}\n"
                f"guilds={[g.name for g in self.guilds]}\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        print(f"\n  {CONFIG.product_name} — {CONFIG.assistant_name} is online as {self.user}")
        print(f"  brain: {self.session.brain_label}")
        if effective_brain() == "subscription":
            print(f"  model: {CONFIG.subscription_model or 'default'}")
        else:
            print(f"  model: {self.session.model_label}")
        print(f"  confirmations: {'ON' if CONFIG.confirm_risky else 'OFF - full autonomy'}")
        print(f"  voice replies: {CONFIG.voice_replies}")
        print(f"  authorised user IDs: {', '.join(str(i) for i in CONFIG.allowed_users)}")
        print("  DM the bot from your phone to start.\n")

        # Warm both slow paths in the background so your FIRST message is fast.
        asyncio.create_task(asyncio.to_thread(voice.warmup))
        if hasattr(brain, "prewarm"):
            asyncio.create_task(brain.prewarm())
        asyncio.create_task(self._announce_back())
        if not self._net_monitor_started:
            self._net_monitor_started = True
            asyncio.create_task(self._watch_network())

    async def _watch_network(self):
        """Keep the laptop online without anyone touching it.

        Discord's own reconnect handles brief blips. This handles the real problem:
        the router died in a power cut and Windows never rejoins the Wi-Fi. We watch the
        charger - when it starts charging again, the electricity (and router) are back,
        so that's the moment to push hard on reconnecting.
        """
        # A profile set to "connect manually" is never rejoined by Windows on its own -
        # the single most common reason a laptop sits offline after a router reboot.
        try:
            await asyncio.to_thread(netrecover.ensure_auto_connect, print)
        except Exception as exc:  # noqa: BLE001
            print(f"[net] auto-connect check failed: {exc}")

        was_plugged = netrecover.power_plugged()
        while not self.is_closed():
            try:
                await asyncio.sleep(30)
                plugged = netrecover.power_plugged()
                power_returned = bool(plugged) and was_plugged is False
                was_plugged = plugged

                if power_returned:
                    print("[net] charger reconnected - checking the network")

                online = await asyncio.to_thread(netrecover.internet_up)
                if online and not power_returned:
                    continue
                if online:
                    continue

                # Offline: actively rejoin Wi-Fi (runs in a thread, netsh is blocking).
                await asyncio.to_thread(netrecover.repair)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the monitor die
                print(f"[net] monitor error: {exc}")

    async def _announce_back(self):
        """Tell the owner she's live again after a reboot or outage.

        Throttled so a crash-loop can't spam, and silent on the very first install.
        """
        stamp = CONFIG.runtime_dir / "last_online.txt"
        now = time.time()
        try:
            last = float(stamp.read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001
            last = 0.0
        try:
            stamp.write_text(str(now), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

        # Only speak up if she'd been gone a while (a real outage/reboot, not a quick restart).
        if last and (now - last) < 300:
            return

        gap = ""
        if last:
            minutes = int((now - last) // 60)
            gap = f" (was offline about {minutes // 60}h {minutes % 60}m)" if minutes >= 60 else f" (was offline about {minutes} min)"

        try:
            _, ssid = await asyncio.to_thread(netrecover.wlan_state)
        except Exception:  # noqa: BLE001
            ssid = ""
        via = f" via **{ssid}**" if ssid else ""

        for guild in self.guilds:
            for channel in guild.text_channels:
                try:
                    await channel.send(
                        f"✅ {CONFIG.assistant_name} is back online{gap}{via} — ready when you are."
                    )
                    return
                except discord.HTTPException:
                    continue

    async def on_message(self, message):
        if message.author.bot:
            return
        # diagnostic breadcrumb: record who last messaged, so a wrong allowlist is easy to spot
        try:
            allowed = message.author.id in CONFIG.allowed_users
            (CONFIG.runtime_dir / "last_sender.txt").write_text(
                f"id={message.author.id}\nname={message.author}\nallowed={allowed}\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        if message.author.id not in CONFIG.allowed_users:
            if isinstance(message.channel, discord.DMChannel):
                await message.channel.send("You are not authorised to control this machine.")
            print(f"[blocked] {message.author} ({message.author.id}): {message.content[:80]}")
            return

        chan = DiscordChannel(message.channel, message.author.id)

        # Voice notes are downloaded here (transport work); the session only
        # transcribes. Only the first audio attachment, as before.
        audio_paths = []
        audio_failed = False
        other = []
        for attachment in message.attachments:
            if self._is_audio(attachment):
                if not audio_paths and not audio_failed:
                    path = CONFIG.voice_dir / f"in-{int(time.time() * 1000)}-{attachment.filename}"
                    try:
                        await attachment.save(str(path))
                        audio_paths.append(path)
                    except Exception as exc:  # noqa: BLE001
                        audio_failed = True
                        await chan.send_text(f"Couldn't download the voice note: {exc}")
            else:
                other.append(attachment.filename)

        await self.session.handle_message(
            IncomingMessage(
                channel=chan,
                text=(message.content or "").strip(),
                audio_paths=audio_paths,
                audio_failed=audio_failed,
                other_attachments=other,
            )
        )

    @staticmethod
    def _is_audio(attachment):
        ctype = (attachment.content_type or "").lower()
        if ctype.startswith("audio") or "ogg" in ctype or "opus" in ctype or "webm" in ctype:
            return True
        name = (attachment.filename or "").lower()
        if name.endswith(AUDIO_EXTS):
            return True
        # Discord voice messages are named voice-message.ogg and carry a waveform
        if getattr(attachment, "waveform", None):
            return True
        return "voice-message" in name


def _sleep(seconds):
    time.sleep(seconds)


class DiscordTransport:
    """Owns the Discord connection lifecycle, incl. the never-give-up loop."""

    name = "discord"

    def __init__(self, session):
        self.session = session

    def run(self):
        handler = logging.FileHandler(CONFIG.runtime_dir / "discord.log", mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

        # Never give up on a network problem. The laptop may boot before the router does, or the
        # line may drop for hours during a power cut - she just keeps retrying until it returns.
        delay = 5
        while True:
            client = DiscordClient(self.session)
            try:
                client.run(CONFIG.discord_token, log_handler=handler, log_level=logging.INFO)
                return  # clean shutdown (Ctrl+C / window closed)
            except discord.LoginFailure:
                print("\n  Discord rejected the token. Check DISCORD_BOT_TOKEN in .env.\n")
                sys.exit(1)
            except discord.PrivilegedIntentsRequired:
                print(
                    "\n  Discord needs the MESSAGE CONTENT INTENT.\n"
                    "  Go to https://discord.com/developers/applications -> your app -> Bot\n"
                    "  -> Privileged Gateway Intents -> turn ON 'Message Content Intent' -> Save.\n"
                )
                sys.exit(1)
            except KeyboardInterrupt:
                return
            except Exception as exc:  # noqa: BLE001 - network/gateway trouble
                print(f"  Connection lost ({type(exc).__name__}: {exc}). Retrying in {delay}s...")
                _sleep(delay)
                netrecover.wait_until_online()
                delay = min(delay * 2, 60)   # back off, but never longer than a minute
