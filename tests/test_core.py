import json
import subprocess
from pathlib import Path

import pytest

from ln_translator import claude_bridge
from ln_translator.claude_bridge import ClaudeBridge, ClaudeError
from ln_translator.glossary import Glossary, apply_changes, check_consistency, diff_proposals
from ln_translator.models import GlossaryEntry
from ln_translator.project import Project
from ln_translator.services import UNTRANSLATED_MARK, Translator
from ln_translator.text_utils import (
    chunk_indices,
    clean_raw,
    detect_language,
    is_aligned,
    paragraph_index_at_line,
    replace_paragraphs,
    split_paragraphs,
)


def g(*entries):
    return Glossary([GlossaryEntry(zh=z, en=e, aliases=list(a)) for z, e, *a in entries])


# ------------------------------------------------------------------ text utils
def test_paragraphs_and_alignment():
    raw = "第一段\n\n第二段\n第三段\n"
    assert split_paragraphs(raw) == ["第一段", "第二段", "第三段"]
    assert paragraph_index_at_line(raw, 2) == 1
    assert paragraph_index_at_line(raw, 1) is None
    assert is_aligned(raw, "One\n\nTwo\n\nThree")
    assert not is_aligned(raw, "One\n\nTwo")
    assert replace_paragraphs("A\n\nB\n\nC", {1: "b!"}) == "A\n\nb!\n\nC"


def test_detect_language():
    assert detect_language("林动走了进来。") == "zh"
    assert detect_language("Lin Dong walked in.") == "en"
    assert detect_language("他说：OK，我们走吧。") == "zh"
    assert detect_language("Lin Dong (林动) walked into the hall and sat down.") == "en"
    assert detect_language("") == "en"


def test_clean_raw_removes_watermarks():
    text = "　　林动走了进来。\n请收藏本站：www.biquge.com\n笔趣阁最新章节\n他笑了。"
    out, changed = clean_raw(text, [r"www\.\S+", "请收藏本站：?", "笔趣阁", "最新章节"])
    assert out == "林动走了进来。\n他笑了。"
    assert changed >= 2


def test_chunking_keeps_order_and_limits():
    paras = ["a" * 400] * 10
    chunks = chunk_indices(paras, 1000)
    assert [i for c in chunks for i in c] == list(range(10))
    assert all(len(c) <= 2 for c in chunks)


# ------------------------------------------------------------------ glossary
def test_longest_match_wins_and_aliases():
    gl = g(("王", "King"), ("王林", "Wang Lin", "小林"))
    found = [(s, e, entry.en) for s, e, entry in gl.matches("王林看着王。小林笑了")]
    assert found == [(0, 2, "Wang Lin"), (4, 5, "King"), (6, 8, "Wang Lin")]
    assert {e.en for e in gl.relevant_entries("小林")} == {"Wang Lin"}


def test_consistency_per_paragraph():
    gl = g(("青云宗", "Azure Cloud Sect"), ("林动", "Lin Dong"))
    raw = "林动来到青云宗。\n他走了。"
    good = "Lin Dong arrived at the Azure Cloud Sect.\n\nHe left."
    bad = "Lin Dong arrived at the Qingyun Sect.\n\nHe left."
    assert check_consistency(gl, raw, good) == []
    issues = check_consistency(gl, raw, bad)
    assert [(w.zh, w.paragraph) for w in issues] == [("青云宗", 0)]
    # Unaligned texts fall back to a whole-chapter check.
    issues = check_consistency(gl, raw, "Lin Dong arrived at the Qingyun Sect. He left.\n\nExtra.\n\nMore.")
    assert [(w.zh, w.paragraph) for w in issues] == [("青云宗", None)]


def test_diff_and_apply_respects_confirmed():
    gl = g(("林动", "Lin Dong"), ("青云宗", "Qingyun Sect"))
    gl.entries[1].confirmed = True
    proposals = [
        GlossaryEntry(zh="林动", en="Lin Dong", aliases=["小动"], gender="male"),
        GlossaryEntry(zh="青云宗", en="Azure Cloud Sect", category="organization"),
        GlossaryEntry(zh="炎帝", en="Flame Emperor", category="title"),
    ]
    changes = diff_proposals(gl, proposals)
    by = {c.proposal.zh: c for c in changes}
    assert by["青云宗"].action == "conflict" and by["青云宗"].accept is False
    assert by["林动"].action == "update"
    assert by["炎帝"].action == "add"
    apply_changes(gl, changes, "0002")
    assert gl.find_by_key("青云宗").en == "Qingyun Sect"  # confirmed: unticked by default
    assert gl.find_by_key("小动").en == "Lin Dong"  # alias merged
    assert gl.find_by_key("林动").gender == "male"
    assert gl.find_by_key("炎帝").first_seen == "0002"


def test_csv_roundtrip():
    gl = g(("林动", "Lin Dong", "小动", "林少"))
    gl.entries[0].confirmed = True
    back = Glossary.parse_csv(gl.to_csv())
    assert back[0].zh == "林动" and back[0].aliases == ["小动", "林少"] and back[0].confirmed


# ------------------------------------------------------------------ project
def test_project_roundtrip_and_history(tmp_path: Path):
    p = Project.create(tmp_path / "novel", "Novel")
    ch = p.new_chapter("Start")
    ch.raw, ch.translation = "林动", "Lin Dong v1"
    p.save_chapter(ch)
    ch.translation = "Lin Dong v2"
    p.save_chapter(ch, history_label="before-translate")
    p.glossary.add(GlossaryEntry(zh="林动", en="Lin Dong"))
    p.save_glossary()

    p2 = Project(tmp_path / "novel")
    assert p2.chapter_ids() == ["0001"]
    assert p2.load_chapter("0001").translation == "Lin Dong v2"
    hist = p2.chapter_history("0001")
    assert len(hist) == 1 and Project.read_history(hist[0]) == "Lin Dong v1"
    assert p2.glossary.entries[0].en == "Lin Dong"
    assert p2.next_chapter_id() == "0002"


# ------------------------------------------------------------------ services with a fake bridge
class FakeBridge:
    """Echoes translations as 'EN:<chinese>' and skips paragraphs listed in drop_once."""

    def __init__(self, drop_once=()):
        self.calls = []
        self.drop_once = set(drop_once)

    def run(self, prompt, system_prompt, schema, progress=None, cancel=None, effort=None, label=""):
        from ln_translator.claude_bridge import ClaudeResult

        self.calls.append(prompt)
        section = prompt.split("## Paragraphs to translate\n", 1)[1].split("\n\n", 1)[0]
        paras = []
        for line in section.splitlines():
            idx, text = line[1:].split("] ", 1)
            idx = int(idx)
            if idx in self.drop_once:
                self.drop_once.discard(idx)
                continue
            paras.append({"index": idx, "en": "EN:" + text})
        return ClaudeResult({"paragraphs": paras, "warnings": [
            {"zh": "炎帝", "suggested_en": "Flame Emperor", "category": "title", "reason": "new", "paragraph": paras[0]["index"] if paras else 0}
        ]})


def test_translate_chapter_chunks_retries_and_glossary_filtering():
    import threading

    raw = "\n".join(f"第{i}段林动" if i == 0 else f"第{i}段" for i in range(6))
    gl = g(("林动", "Lin Dong"), ("萧炎", "Xiao Yan"))
    bridge = FakeBridge(drop_once={3})
    t = Translator(bridge, chunk_chars=8)
    chunks_seen = []
    out = t.translate_chapter(raw, gl, "style", "summary", lambda m: None, threading.Event(), chunks_seen.append)
    assert [out.paragraphs[i] for i in range(6)] == ["EN:" + p for p in split_paragraphs(raw)]
    assert "林动 = Lin Dong" in bridge.calls[0] and "萧炎" not in bridge.calls[0]  # only relevant terms sent
    assert any("[3]" in c and "[2]" not in c.split("## Paragraphs to translate")[1].split("\n\n")[0] for c in bridge.calls)  # retry
    assert len([w for w in out.warnings if w.zh == "炎帝"]) == 1  # deduped
    assert chunks_seen and len(chunks_seen[-1]) == 6


def test_retranslate_requires_alignment():
    import threading

    t = Translator(FakeBridge())
    with pytest.raises(ValueError):
        t.retranslate("甲\n乙", "A", [0], Glossary(), "", "", "", lambda m: None, threading.Event())
    out = t.retranslate("甲\n乙", "A\n\nB", [1], Glossary(), "", "", "", lambda m: None, threading.Event())
    assert out.paragraphs == {1: "EN:乙"}
    assert UNTRANSLATED_MARK not in out.paragraphs[1]


# ------------------------------------------------------------------ bridge parsing with a fake CLI
def test_bridge_parses_stream_json(monkeypatch, tmp_path):
    events = [
        {"type": "system", "subtype": "init", "model": "test"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "{}", "structured_output": {"ok": True},
         "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 3}},
    ]
    script = tmp_path / "fake.py"
    script.write_text("import sys,json\nsys.stdin.read()\n" + "".join(f"print(json.dumps({e!r}))\n" for e in events))
    import sys

    bridge = ClaudeBridge()
    monkeypatch.setattr(bridge, "build_command", lambda *a, **k: [sys.executable, str(script)])
    res = bridge.run("hi", "sys", {})
    assert res.data == {"ok": True}
    assert res.stats.five_hour_utilization == 0.1 and res.stats.output_tokens == 3


def test_bridge_reports_errors(monkeypatch, tmp_path):
    script = tmp_path / "fake.py"
    err = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}
    script.write_text(f"import json\nprint(json.dumps({err!r}))\n")
    import sys

    bridge = ClaudeBridge()
    monkeypatch.setattr(bridge, "build_command", lambda *a, **k: [sys.executable, str(script)])
    with pytest.raises(ClaudeError, match="boom"):
        bridge.run("hi", "sys", {})


def test_resolve_prefers_real_exe_behind_npm_shim(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off")
    real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_text("")
    monkeypatch.setattr(claude_bridge.shutil, "which", lambda _n: None)
    assert claude_bridge.resolve_claude_executable(str(shim)) == str(real)


def test_provisional_terms_carry_across_chunks_and_retranslate():
    import threading

    from ln_translator.models import Warning

    raw = "第0段炎帝\n第1段炎帝\n第2段炎帝"
    bridge = FakeBridge()
    t = Translator(bridge, chunk_chars=6)
    t.translate_chapter(raw, Glossary(), "", "", lambda m: None, threading.Event())
    assert "炎帝" not in bridge.calls[0].split("## Glossary")[1].split("##")[0]
    assert "炎帝 = Flame Emperor" in bridge.calls[1]  # chunk 2 reuses chunk 1's choice

    bridge2 = FakeBridge()
    prov = [Warning(kind="uncertain", zh="炎帝", suggested_en="Flame Emperor", category="title")]
    Translator(bridge2).retranslate("甲炎帝\n乙", "A\n\nB", [0], Glossary(), "", "", "", lambda m: None,
                                    threading.Event(), prov)
    assert "炎帝 = Flame Emperor" in bridge2.calls[0]


def test_naming_policy_persists_and_reaches_prompt(tmp_path):
    import threading

    from ln_translator.models import DEFAULT_NAMING

    p = Project.create(tmp_path / "n", "N")
    assert p.naming == DEFAULT_NAMING
    p.naming = {**DEFAULT_NAMING, "place": "meaning", "character": "bogus"}
    again = Project(tmp_path / "n")
    assert again.naming["place"] == "meaning" and again.naming["character"] == DEFAULT_NAMING["character"]

    bridge = FakeBridge()
    Translator(bridge, naming=again.naming).translate_chapter("蓝月城", Glossary(), "", "", lambda m: None, threading.Event())
    assert "## Name rendering" in bridge.calls[0] and "- place: translate by meaning" in bridge.calls[0]
