"""Test doubles for the transport seam: no Discord, no network, no API keys."""


class FakeChannel:
    """A TransportChannel that records everything instead of sending it."""

    def __init__(self, user_id=42, channel_id=7, confirm_answer=True):
        self.user_id = user_id
        self.channel_id = channel_id
        self.texts = []
        self.notes = []
        self.files = []
        self.confirm_calls = []
        self.last_spoken = ""
        self.spoke = False
        self._confirm_answer = confirm_answer

    async def send_text(self, text):
        text = str(text)
        if text.strip():
            self.last_spoken = text
        self.texts.append(text)

    async def note(self, text):
        self.notes.append(str(text))

    async def send_file(self, path, caption=None):
        self.files.append((str(path), caption))

    async def send_voice(self, path, caption=None):
        await self.send_file(path, caption)

    async def confirm(self, tool_name, reason, detail):
        self.confirm_calls.append((tool_name, reason, detail))
        return self._confirm_answer
