"""Run the real Stop hook against small, hand-authored desktop transcripts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "skill_stats.py"


def event(payload, kind="event_msg"):
    return {"type": kind, "payload": payload}


def start(turn):
    return event({"type": "task_started", "turn_id": turn})


def catalog(entries=None):
    entries = entries if entries is not None else [
        ("skill A", "r0/alpha/SKILL.md"), ("skill B", "r0/beta/SKILL.md")]
    text = "<skills_instructions>\n### Skill roots\n- `r0` = `C:/skills`\n### Available skills\n"
    text += "\n".join(f"- {name}: Example. (file: {path})" for name, path in entries)
    text += "\n</skills_instructions>"
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def read(path, turn="current", **changes):
    item = {"type": "CommandExecution", "id": "call-1", "command": ["pwsh", "-Command", "Get-Content example"],
            "cwd": "C:/skills", "parsed_cmd": [{"type": "read", "path": path}],
            "status": "completed", "exit_code": 0, "aggregated_output": "Instructions returned by the read.\n",
            "stdout": "Instructions returned by the read.\n", "stderr": ""}
    item.update(changes)
    return event({"type": "item_completed", "thread_id": "session-1", "turn_id": turn, "item": item})


class SkillStatsTests(unittest.TestCase):
    def run_hook(self, records, turn="current", payload=None, tail=""):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n" + tail, encoding="utf-8")
            data = payload if payload is not None else {"hook_event_name": "Stop", "session_id": "session-1",
                                                       "turn_id": turn, "transcript_path": str(transcript)}
            proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(data),
                                  text=True, encoding="utf-8", capture_output=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(list(Path(folder).iterdir()), [transcript], "The hook must remain read-only")
            return json.loads(proc.stdout)

    def test_catalog_and_references_are_two_deduplicated_top_level_lists(self):
        result = self.run_hook([event(catalog(), "response_item"), start("current"),
                                read("C:/skills/alpha/SKILL.md"), read("c:\\SKILLS\\alpha\\references\\guide.md"),
                                read("C:/skills/alpha/SKILL.md")])
        self.assertEqual(result, {"systemMessage": "Available skills (2)\n- skill A\n- skill B\n\nRead this turn (1)\n- skill A"})

    def test_failed_search_empty_output_and_other_turns_do_not_count(self):
        records = [event(catalog(), "response_item"), start("previous"), read("C:/skills/beta/SKILL.md", "previous"),
                   start("current"), read("C:/skills/beta/SKILL.md", exit_code=1),
                   read("C:/skills/beta/SKILL.md", status="inProgress"),
                   read("C:/skills/beta/SKILL.md", aggregated_output="", stdout=""),
                   read("C:/skills/beta/SKILL.md", parsed_cmd=[{"type": "search", "path": "C:/skills/beta/SKILL.md"}]),
                   read("C:/skills/beta/SKILL.md", stderr="Get-Content: file missing"),
                   read("C:/skills/beta-extra/SKILL.md"), read("alpha/references/a.md")]
        self.assertEqual(self.run_hook(records)["systemMessage"].split("\n\n")[1], "Read this turn (1)\n- skill A")

    def test_llm_statements_and_user_catalog_cannot_create_usage(self):
        fake = catalog([("made up", "r0/fake/SKILL.md")]); fake["role"] = "user"
        result = self.run_hook([event(catalog(), "response_item"), start("current"), event(fake, "response_item"),
                               event({"type": "message", "role": "assistant", "content": [{"type": "output_text",
                                      "text": "I used skill B and read C:/skills/beta/SKILL.md."}]}, "response_item")])
        self.assertEqual(result["systemMessage"], "Available skills (2)\n- skill A\n- skill B\n\nRead this turn (0)\n- None")

    def test_later_turn_cannot_change_catalog_or_used(self):
        records = [event(catalog(), "response_item"), start("current"), read("C:/skills/alpha/SKILL.md"),
                   start("future"), event(catalog([("future skill", "r0/future/SKILL.md")]), "response_item"),
                   read("C:/skills/future/SKILL.md", "future")]
        self.assertEqual(self.run_hook(records)["systemMessage"], "Available skills (2)\n- skill A\n- skill B\n\nRead this turn (1)\n- skill A")

    def test_compaction_catalog_is_kept_without_replaying_old_reads(self):
        compact = event({"replacement_history": [catalog(), {"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "Previously used skill B"}]}]}, "compacted")
        result = self.run_hook([compact, start("current"), read("C:/skills/alpha/references/a.md")])
        self.assertEqual(result["systemMessage"], "Available skills (2)\n- skill A\n- skill B\n\nRead this turn (1)\n- skill A")

    def test_subagent_read_is_not_parent_turn_usage(self):
        child = read("C:/skills/beta/SKILL.md"); child["payload"]["thread_id"] = "child-session"
        result = self.run_hook([event(catalog(), "response_item"), start("current"), child])
        self.assertIn("Read this turn (0)\n- None", result["systemMessage"])

    def test_relative_read_uses_decoded_file_uri_and_preserves_plugin_name(self):
        entry = catalog([("plugin:skill A", "C:/work dir/alpha/SKILL.md")])
        result = self.run_hook([event(entry, "response_item"), start("current"),
                               read("alpha/references/guide.md", cwd="file:///C:/work%20dir")])
        self.assertEqual(result["systemMessage"], "Available skills (1)\n- plugin:skill A\n\nRead this turn (1)\n- plugin:skill A")

    def test_unavailable_data_is_not_reported_as_an_empty_list(self):
        for records in ([start("current")], [event(catalog(), "response_item"), start("other")]):
            with self.subTest(records=records):
                result = self.run_hook(records)
                self.assertIn("Skill data unavailable.", result["systemMessage"])
                self.assertNotIn("Read this turn (0)", result["systemMessage"])

    def test_empty_catalog_is_valid(self):
        result = self.run_hook([event(catalog([]), "response_item"), start("current")])
        self.assertEqual(result["systemMessage"], "Available skills (0)\n- None\n\nRead this turn (0)\n- None")

    def test_unrecognized_catalog_entry_is_not_reported_as_zero(self):
        entry = catalog()
        entry["content"][0]["text"] = entry["content"][0]["text"].replace("(file:", "(location:")
        result = self.run_hook([event(entry, "response_item"), start("current")])
        self.assertIn("Skill data unavailable.", result["systemMessage"])

    def test_superseded_catalog_format_does_not_break_current_turn(self):
        old = catalog()
        old["content"][0]["text"] = old["content"][0]["text"].replace("(file:", "(location:")
        result = self.run_hook([event(old, "response_item"), start("old"),
                               event(catalog(), "response_item"), start("current"),
                               read("C:/skills/alpha/SKILL.md")])
        self.assertIn("Read this turn (1)\n- skill A", result["systemMessage"])

    def test_current_catalog_after_start_supersedes_older_malformed_catalog(self):
        old = catalog()
        old["content"][0]["text"] = old["content"][0]["text"].replace("(file:", "(location:")
        for earlier in ([event(old, "response_item"), start("old"), start("current")],
                        [start("current"), event(old, "response_item")]):
            with self.subTest(earlier=earlier):
                result = self.run_hook(earlier + [event(catalog(), "response_item"),
                                                 read("C:/skills/alpha/SKILL.md")])
                self.assertEqual(result["systemMessage"], "Available skills (2)\n- skill A\n- skill B\n\nRead this turn (1)\n- skill A")

    def test_null_or_empty_turn_id_is_unavailable(self):
        for turn in (None, ""):
            with self.subTest(turn=turn):
                result = self.run_hook([event(catalog(), "response_item"), start(turn),
                                        read("C:/skills/alpha/SKILL.md", turn)], turn=turn)
                self.assertIn("Skill data unavailable.", result["systemMessage"])
                self.assertNotIn("Read this turn", result["systemMessage"])

    def test_long_unicode_record_and_partial_tail_preserve_current_reads(self):
        noise = event({"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "\N{SNOWMAN}" * 40000}]}, "response_item")
        result = self.run_hook([event(catalog(), "response_item"), start("current"), noise,
                               read("C:/skills/alpha/SKILL.md")], tail='{"type":"event_msg"')
        self.assertIn("Read this turn (1)\n- skill A", result["systemMessage"])

    def test_mid_turn_compaction_does_not_drop_reads_before_repeated_context(self):
        records = [event(catalog(), "response_item"), start("current"),
                   read("C:/skills/alpha/SKILL.md"),
                   event({"replacement_history": [catalog()]}, "compacted"),
                   event({"turn_id": "current"}, "turn_context"), read("C:/skills/beta/SKILL.md")]
        result = self.run_hook(records)
        self.assertIn("Read this turn (2)\n- skill A\n- skill B", result["systemMessage"])

    def test_incorrect_event_and_missing_transcript_fail_open(self):
        self.assertEqual(self.run_hook([], payload={"hook_event_name": "SessionStart"}), {})
        result = self.run_hook([], payload={"hook_event_name": "Stop", "turn_id": "current", "transcript_path": ""})
        self.assertIn("Skill data unavailable.", result["systemMessage"])


if __name__ == "__main__":
    unittest.main()
