from ln_translator import prompts
from ln_translator.claude_bridge import ClaudeResult
from ln_translator.dictionary import Dictionary, WordList, parse_cedict, pinyin_marks, sentence_at
from ln_translator.services import explain_word

SAMPLE = """# CC-CEDICT sample
筑 筑 [zhu4] /five-string lute/Taiwan pr. [zhu2]/
築 筑 [zhu4] /to build/to construct/to ram/to hit/Taiwan pr. [zhu2]/
基 基 [ji1] /base/foundation/
筑基 筑基 [zhu4 ji1] /to lay a foundation/
期 期 [qi1] /period/cycle/
了 了 [le5] /(completed action marker)/(modal particle)/
了 了 [liao3] /to finish/to understand/to know/to be done/
林 林 [Lin2] /surname Lin/
林 林 [lin2] /woods/forest/
天 天 [tian1] /day/sky/heaven/
天無絕人之路 天无绝人之路 [tian1 wu2 jue2 ren2 zhi1 lu4] /Heaven never bars one's way (idiom)/
女 女 [nu:3] /female/woman/
broken line without brackets
"""


def d():
    return Dictionary(parse_cedict(SAMPLE.splitlines()))


def test_pinyin_tone_marks():
    assert pinyin_marks("zhu4 ji1") == "zhù jī"
    assert pinyin_marks("nu:3 lu:4") == "nǚ lǜ"
    assert pinyin_marks("Xiao3 gou3 le5") == "Xiǎo gǒu le"
    assert pinyin_marks("hao3 jiu3 liu2") == "hǎo jiǔ liú"  # last vowel for iu/ui
    assert pinyin_marks("dui4 shou3") == "duì shǒu"  # o in ou
    assert pinyin_marks("r5 xx5 m2") == "r xx m"


def test_parse_skips_comments_and_bad_lines():
    entries = parse_cedict(SAMPLE.splitlines())
    assert len(entries) == 12
    assert entries[3].definitions == ("to lay a foundation",)


def test_ranking_prefers_common_sense_particles_and_non_names():
    dic = d()
    assert dic.lookup("筑")[0].definitions[0] == "to build"
    assert dic.lookup("了")[0].pinyin == "le"
    assert dic.lookup("林")[0].pinyin == "lín"
    assert dic.lookup("築")[0].traditional == "築"  # traditional characters are indexed too
    assert dic.guess_pinyin("林女") == "lín nǚ"  # names not in the dictionary, character by character


def test_longest_match_shorter_and_container():
    dic = d()
    text = "终于突破到了筑基期。"
    r = dic.look_up_at(text, text.index("筑"))
    assert r.word == "筑基" and [m.word for m in r.shorter] == ["筑"]
    assert [c.word for c in r.chars] == ["筑", "基"]
    # hovering the second character still offers the word that covers it
    r2 = dic.look_up_at(text, text.index("基"))
    assert r2.word == "基" and r2.container.word == "筑基"
    assert dic.look_up_at(text, text.index("。")) is None  # punctuation
    unknown = dic.look_up_at("突破", 0)
    assert unknown.word == "突" and unknown.primary.entries == []
    idiom = "真是天无绝人之路！"
    assert dic.look_up_at(idiom, idiom.index("天")).word == "天无绝人之路"


def test_sentence_at():
    text = "他笑了。“你来了？”她说：“筑基期而已。”然后走了"
    assert sentence_at(text, text.index("筑")) == "她说：“筑基期而已。”"
    assert sentence_at(text, 0) == "他笑了。"
    assert sentence_at(text, len(text) - 1) == "然后走了"
    long = "啊" * 300 + "筑" + "啊" * 300
    s = sentence_at(long, 300, limit=40)
    assert "筑" in s and s.startswith("…") and s.endswith("…")


def test_wordlist_roundtrip_and_ordering(tmp_path):
    path = tmp_path / "wordlist.json"
    wl = WordList(path)
    wl.record_lookup("筑基", "zhù jī", "foundation", "突破到了筑基期。", "Novel · 0001")
    wl.record_lookup("筑基", sentence="又是筑基。")
    wl.record_lookup("灵力")
    wl.set_saved("道心", True, "dào xīn", "", "道心坚定。")
    wl.set_explanation("灵力", {"pinyin": "líng lì", "meaning": "spiritual power", "examples": []}, "灵力运转。")
    wl.save()

    wl2 = WordList(path)
    w = wl2.get("筑基")
    assert w.lookups == 2 and w.pinyin == "zhù jī" and w.sentence == "又是筑基。"
    assert wl2.get("灵力").gloss == "spiritual power"
    assert wl2.get("灵力").explanation["context"] == "灵力运转。"
    assert [x.word for x in wl2.sorted()] == ["道心", "筑基", "灵力"]  # starred first, then by lookups
    assert [x.word for x in wl2.sorted(saved_only=True)] == ["道心"]
    wl2.remove("道心")
    assert wl2.get("道心") is None


class ExplainBridge:
    def __init__(self):
        self.prompt = ""

    def run(self, prompt, system_prompt, schema, progress=None, cancel=None, effort=None, label=""):
        self.prompt, self.effort, self.schema = prompt, effort, schema
        return ClaudeResult({"pinyin": "zhù jī", "meaning": "Foundation Establishment", "explanation_zh": "打基础的境界",
                             "explanation_en": "A cultivation realm.", "breakdown": "筑 build + 基 foundation",
                             "examples": [{"zh": "他筑基了。", "en": "He reached Foundation Establishment."}, {"zh": ""},
                                          {"zh": "a", "en": "b"}, {"zh": "c", "en": "d"}],
                             "is_story_term": False})


def test_explain_word_builds_prompt_and_cleans_examples():
    b = ExplainBridge()
    data, stats = explain_word(b, "筑基", "终于突破到了筑基期。", lambda m: None, None, novel="DarkWorld",
                               dictionary_hint="zhù jī: to lay a foundation", glossary_hint="筑基 → Foundation Establishment")
    assert "## Word\n筑基" in b.prompt and "终于突破到了筑基期。" in b.prompt
    assert "DarkWorld" in b.prompt and "to lay a foundation" in b.prompt and "Foundation Establishment" in b.prompt
    assert b.effort == "low" and b.schema is prompts.EXPLAIN_SCHEMA
    assert len(data["examples"]) == 2 and data["examples"][0]["zh"] == "他筑基了。"
    assert stats.calls == 1
