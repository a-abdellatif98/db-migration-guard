import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def all_hooks():
    config = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    return [h for groups in config["hooks"].values() for group in groups for h in group["hooks"]]


def test_plugin_ships_no_command_hooks():
    assert all_hooks()
    assert all(h["type"] == "prompt" for h in all_hooks())


def test_prompt_hook_gets_input_and_has_no_stray_dollar():
    for h in all_hooks():
        assert "$ARGUMENTS" in h["prompt"]
        assert re.findall(r"(?<!\\)\$\w*", h["prompt"]) == ["$ARGUMENTS"]
        assert h.get("continueOnBlock") is True


def test_prompt_hook_has_confirmation_escape_hatch():
    prompt = all_hooks()[0]["prompt"]
    assert "migration-guard: confirmed" in prompt
    assert '{"ok": true}' in prompt and '"ok": false' in prompt


def test_no_preapproved_shell_access():
    for path in list((ROOT / "commands").glob("*.md")) + list((ROOT / "agents").glob("*.md")):
        front = path.read_text().split("---")[1]
        assert "Bash" not in front, path.name


def test_manifest_version_matches_skill():
    version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    skill = (ROOT / "skills" / "migrate-check" / "SKILL.md").read_text()
    assert f"version: {version}" in skill
