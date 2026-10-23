"""EnvFile: round-trip fidelity — comments, order, unknown keys survive."""

from agent.envfile import EnvFile

SAMPLE = """# ---- Discord ----
# A comment that must survive.
DISCORD_BOT_TOKEN=abc123

UNKNOWN_FUTURE_KEY=keep me
SPACED = value with spaces
QUOTED="hello world"
SINGLE='single quoted'
EMPTY_LINE_ABOVE=1
export EXPORTED=yes
# trailing comment
"""


def _write(tmp_path, text=SAMPLE):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_values(tmp_path):
    env = EnvFile.load(_write(tmp_path))
    assert env.get("DISCORD_BOT_TOKEN") == "abc123"
    assert env.get("SPACED") == "value with spaces"
    assert env.get("QUOTED") == "hello world"
    assert env.get("SINGLE") == "single quoted"
    assert env.get("EXPORTED") == "yes"
    assert env.get("MISSING") is None
    assert env.get("MISSING", "dflt") == "dflt"


def test_round_trip_is_byte_identical_when_unchanged(tmp_path):
    path = _write(tmp_path)
    env = EnvFile.load(path)
    env.save()
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_set_existing_preserves_comments_and_order(tmp_path):
    path = _write(tmp_path)
    env = EnvFile.load(path)
    env.set("DISCORD_BOT_TOKEN", "newtoken")
    env.save()
    out = path.read_text(encoding="utf-8")
    assert "# A comment that must survive." in out
    assert "DISCORD_BOT_TOKEN=newtoken" in out
    assert "UNKNOWN_FUTURE_KEY=keep me" in out
    # order unchanged: discord key still appears before the unknown key
    assert out.index("DISCORD_BOT_TOKEN") < out.index("UNKNOWN_FUTURE_KEY")


def test_set_new_key_appends(tmp_path):
    path = _write(tmp_path)
    env = EnvFile.load(path)
    env.set("BRAND_NEW", "x")
    env.save()
    out = path.read_text(encoding="utf-8")
    assert out.rstrip().endswith("BRAND_NEW=x")
    assert EnvFile.load(path).get("BRAND_NEW") == "x"


def test_set_quotes_values_that_need_it(tmp_path):
    path = _write(tmp_path)
    env = EnvFile.load(path)
    env.set("GREETING", "hello world #1")
    env.save()
    assert EnvFile.load(path).get("GREETING") == "hello world #1"


def test_set_empty_clears_value(tmp_path):
    path = _write(tmp_path)
    env = EnvFile.load(path)
    env.set("DISCORD_BOT_TOKEN", "")
    env.save()
    assert EnvFile.load(path).get("DISCORD_BOT_TOKEN") == ""


def test_missing_file_loads_empty(tmp_path):
    env = EnvFile.load(tmp_path / "nope.env")
    assert env.existed is False
    assert env.keys() == []


def test_bootstrap_copies_example(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("A=1\nB=two\n", encoding="utf-8")
    target = tmp_path / ".env"
    env, created = EnvFile.bootstrap(target, example)
    assert created is True
    assert target.exists()
    assert env.get("B") == "two"
    env2, created2 = EnvFile.bootstrap(target, example)
    assert created2 is False
