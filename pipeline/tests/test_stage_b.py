"""Unit tests for pipeline.stage_b — plan normalization, boundary validation,
timing reconciliation, caption carding, watermark/compress guards, and zip
packaging. These tests never invoke ffmpeg/Edge TTS/Whisper; media steps are
exercised through their pure-Python decision logic only.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.stage_b import captions, common, render, run as stage_b_run  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _valid_plan_document(**overrides) -> dict:
    doc = {
        "version": 2,
        "job_id": "manual-1787692652625",
        "title": "A Test Video",
        "video_duration_seconds": 300,
        "target_total_duration_seconds": 60,
        "cuts": [
            {"start_seconds": 10, "end_seconds": 40, "voiceover_text": "First line of narration."},
            {"start_seconds": 50, "end_seconds": 90, "voiceover_text": "Second line, with a clause. And more."},
        ],
        "hashtags": ["#one", "#two", "#three", "#four", "#five"],
        "youtube_tags": [f"tag{i}" for i in range(10)],
    }
    doc.update(overrides)
    return doc


def _write_wav(path: Path, *, seconds: float = 1.0, rate: int = 48000, channels: int = 1) -> None:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames * channels)


# --------------------------------------------------------------------------- #
# common.normalize_plan / load_production_plan                                 #
# --------------------------------------------------------------------------- #

class NormalizePlanTests(unittest.TestCase):
    def test_voiceover_text_passthrough(self) -> None:
        plan = common.normalize_plan(_valid_plan_document())
        self.assertEqual(plan["cuts"][0]["voiceover_text"], "First line of narration.")
        self.assertEqual(plan["title"], "A Test Video")
        self.assertEqual(len(plan["cuts"]), 2)

    def test_raw_narration_fallback(self) -> None:
        doc = _valid_plan_document()
        for cut in doc["cuts"]:
            cut["raw_narration"] = cut.pop("voiceover_text")
        plan = common.normalize_plan(doc)
        self.assertEqual(plan["cuts"][0]["voiceover_text"], "First line of narration.")

    def test_flat_series_siblings_normalized(self) -> None:
        doc = _valid_plan_document(
            series_id="abc", series_part=2, series_start_seconds=5,
            series_end_seconds=95, series_final=False, series_summary="prior part",
        )
        plan = common.normalize_plan(doc)
        self.assertEqual(plan["series"]["series_id"], "abc")
        self.assertEqual(plan["series"]["part"], 2)
        self.assertEqual(plan["series"]["start_seconds"], 5)
        self.assertEqual(plan["series"]["end_seconds"], 95)
        self.assertFalse(plan["series"]["is_final"])
        self.assertEqual(plan["series"]["summary"], "prior part")

    def test_nested_series_wins_per_field(self) -> None:
        doc = _valid_plan_document(
            series={"series_id": "nested-id", "part": 3, "start_seconds": 7,
                    "end_seconds": 100, "is_final": True, "summary": "nested"},
            series_id="flat-id", series_part=9,
        )
        plan = common.normalize_plan(doc)
        self.assertEqual(plan["series"]["series_id"], "nested-id")
        self.assertEqual(plan["series"]["part"], 3)
        self.assertTrue(plan["series"]["is_final"])

    def test_cuts_sorted_by_start(self) -> None:
        doc = _valid_plan_document()
        doc["cuts"] = list(reversed(doc["cuts"]))
        plan = common.normalize_plan(doc)
        self.assertEqual(plan["cuts"][0]["start_seconds"], 10)
        self.assertEqual(plan["cuts"][1]["start_seconds"], 50)

    def test_unknown_top_level_fields_preserved_in_extra(self) -> None:
        doc = _valid_plan_document(some_future_field={"x": 1})
        plan = common.normalize_plan(doc)
        self.assertEqual(plan["_extra"]["some_future_field"], {"x": 1})


class LoadProductionPlanTests(unittest.TestCase):
    def test_valid_plan_loads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "production.json"
            p.write_text(json.dumps(_valid_plan_document()), encoding="utf-8")
            plan = common.load_production_plan(p)
            self.assertEqual(plan["video_duration_seconds"], 300)

    def test_invalid_plan_refused_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "production.json"
            bad = _valid_plan_document()
            bad["cuts"] = []  # no cuts -> invalid
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(common.StageBError):
                common.load_production_plan(p)

    def test_unparseable_json_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "production.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(common.StageBError):
                common.load_production_plan(p)

    def test_missing_file_refused(self) -> None:
        with self.assertRaises(common.StageBError):
            common.load_production_plan("/nonexistent/production.json")

    def test_overlapping_cuts_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "production.json"
            doc = _valid_plan_document()
            doc["cuts"][1]["start_seconds"] = 20  # overlaps cut 0 (10-40)
            p.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(common.StageBError):
                common.load_production_plan(p)


# --------------------------------------------------------------------------- #
# render.reconcile_cuts / coverage guard                                       #
# --------------------------------------------------------------------------- #

class ReconcileTests(unittest.TestCase):
    def test_exact_match_needs_no_stretch(self) -> None:
        cuts = [{"start_seconds": 0, "end_seconds": 10}]
        plan = render.reconcile_cuts(cuts, [10.0], 300.0)
        self.assertEqual(plan[0]["stretch"], 1.0)
        self.assertEqual(plan[0]["video_seconds"], 10.0)

    def test_voiceover_longer_slows_video(self) -> None:
        cuts = [{"start_seconds": 0, "end_seconds": 10}]
        plan = render.reconcile_cuts(cuts, [20.0], 300.0)
        self.assertAlmostEqual(plan[0]["stretch"], 2.0)
        self.assertAlmostEqual(plan[0]["video_seconds"], 20.0)

    def test_voiceover_shorter_speeds_video(self) -> None:
        cuts = [{"start_seconds": 0, "end_seconds": 20}]
        plan = render.reconcile_cuts(cuts, [10.0], 300.0)
        self.assertAlmostEqual(plan[0]["stretch"], 0.5)

    def test_boundaries_never_move(self) -> None:
        cuts = [{"start_seconds": 7, "end_seconds": 13}]
        plan = render.reconcile_cuts(cuts, [3.0], 300.0)
        self.assertEqual(plan[0]["start_seconds"], 7.0)
        self.assertEqual(plan[0]["end_seconds"], 13.0)

class FinalWavTimingTests(unittest.TestCase):
    def test_timing_from_wav_frames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "vo.wav"
            _write_wav(wav_path, seconds=2.0, rate=24000)
            duration, out_frames = render.final_wav_timing(wav_path)
            self.assertAlmostEqual(duration, 2.0, places=3)
            self.assertEqual(out_frames, 2 * 48000)

    def test_unreadable_wav_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.wav"
            bad.write_bytes(b"not a wav")
            with self.assertRaises(common.StageBError):
                render.final_wav_timing(bad)


# --------------------------------------------------------------------------- #
# captions: sentence carding + keyword colors + ASS escaping                   #
# --------------------------------------------------------------------------- #

def _w(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


class CaptionCardTests(unittest.TestCase):
    def test_sentence_split_on_terminal_punctuation(self) -> None:
        events = [_w("Hello.", 0.0, 0.4), _w("World.", 0.5, 0.9)]
        cards = captions.split_sentences(events)
        self.assertEqual(len(cards), 2)

    def test_long_sentence_chunked_at_clause_break(self) -> None:
        words = "one two three four, five six seven eight nine ten.".split()
        events = [_w(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)]
        cards = captions.split_sentences(events)
        self.assertTrue(all(len(c["words"]) <= captions.MAX_CAPTION_WORDS for c in cards))
        self.assertGreater(len(cards), 1)
        # Reassembly preserves every word in order.
        flat = [w["word"] for c in cards for w in c["words"]]
        self.assertEqual(flat, words)

    def test_trailing_single_word_merged_back(self) -> None:
        events = [_w("Done.", 0.0, 0.4), _w("wait", 0.5, 0.8)]
        cards = captions.split_sentences(events)
        self.assertEqual(len(cards), 1)

    def test_validate_timeline_rejects_internal_gap(self) -> None:
        sentences = [
            {"words": [_w("a", 0.0, 0.5)], "start": 0.0, "speak_end": 0.5},
            {"words": [_w("b", 10.0, 10.5)], "start": 10.0, "speak_end": 10.5},
        ]
        with self.assertRaises(common.StageBError):
            captions.validate_caption_timeline(sentences, 12.0)

    def test_validate_timeline_rejects_uncaptained_tail(self) -> None:
        sentences = [{"words": [_w("a", 0.0, 0.5)], "start": 0.0, "speak_end": 0.5}]
        with self.assertRaises(common.StageBError):
            captions.validate_caption_timeline(sentences, 30.0)

    def test_internal_coverage_holds_card_across_gap(self) -> None:
        sentences = [
            {"words": [_w("a", 0.0, 0.5)], "start": 0.0, "speak_end": 0.5},
            {"words": [_w("b", 6.0, 6.5)], "start": 6.0, "speak_end": 6.5},
        ]
        captions.normalize_caption_internal_coverage(sentences)
        self.assertEqual(sentences[0]["speak_end"], 6.0)


class KeywordColorTests(unittest.TestCase):
    def test_list_and_dict_keyword_shapes(self) -> None:
        plan = _valid_plan_document()
        plan["cuts"][0]["keywords"] = [{"word": "First", "color": "#ff5c5c"}]
        plan["cuts"][1]["keywords"] = {"Second": "00FF00"}
        texts, kw = captions.script_texts_and_keywords(common.normalize_plan(plan))
        self.assertEqual(kw["first"], "#FF5C5C")
        self.assertEqual(kw["second"], "#00FF00")
        self.assertEqual(len(texts), 2)

    def test_invalid_color_rejected(self) -> None:
        plan = _valid_plan_document()
        plan["cuts"][0]["keywords"] = [{"word": "First", "color": "red"}]
        with self.assertRaises(common.StageBError):
            captions.script_texts_and_keywords(common.normalize_plan(plan))

    def test_ass_escaping(self) -> None:
        self.assertEqual(captions._ass_escape("a{b}\\c"), "a\\{b\\}\\\\c")
        self.assertEqual(captions._ass_time(65.25), "0:01:05.25")


class AlignWordsTests(unittest.TestCase):
    def test_alignment_preserves_script_wording(self) -> None:
        timed = [_w("the", 0.0, 0.2), _w("quick", 0.2, 0.5), _w("fox", 0.5, 0.9)]
        events = captions.align_words_to_script(timed, ["The quick fox!"])
        self.assertEqual([e["word"] for e in events], ["The", "quick", "fox!"])

    def test_real_cut_boundaries_partition_words(self) -> None:
        timed = [_w(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(6)]
        scripts = ["w0 w1 w2", "w3 w4 w5"]
        events = captions.align_words_to_script(timed, scripts, [1.5, 1.5])
        self.assertEqual(len(events), 6)
        # Words of cut 2 must start at/after the first cut boundary.
        self.assertGreaterEqual(events[3]["start"], 1.5)


# --------------------------------------------------------------------------- #
# reframe: crop filter graph                                                   #
# --------------------------------------------------------------------------- #

class ReframeFilterTests(unittest.TestCase):
    def test_single_scene_filter(self) -> None:
        plan = {
            "target_width": 1080, "target_height": 1200,
            "scenes": [{"start_seconds": 0.0, "end_seconds": 5.0,
                        "crop_offset_x": 0.5, "crop_offset_y": 0.5}],
        }
        filters, out = __import__("pipeline.stage_b.reframe", fromlist=["scene_crop_filter"]).scene_crop_filter(plan)
        self.assertEqual(out, "reframed")
        self.assertTrue(any("crop=1080:1200" in f for f in filters))

    def test_multi_scene_filter_uses_concat(self) -> None:
        from pipeline.stage_b import reframe as reframe_mod
        plan = {
            "target_width": 1080, "target_height": 1200,
            "scenes": [
                {"start_seconds": 0.0, "end_seconds": 2.0, "crop_offset_x": 0.3, "crop_offset_y": 0.5},
                {"start_seconds": 2.0, "end_seconds": 5.0, "crop_offset_x": 0.8, "crop_offset_y": 0.5},
            ],
        }
        filters, out = reframe_mod.scene_crop_filter(plan)
        joined = ";".join(filters)
        self.assertIn("split=2", joined)
        self.assertIn("concat=n=2", joined)

    def test_empty_plan_rejected(self) -> None:
        from pipeline.stage_b import reframe as reframe_mod
        with self.assertRaises(common.StageBError):
            reframe_mod.scene_crop_filter({"scenes": []})

    def test_shot_index_covers_full_duration(self) -> None:
        from pipeline.stage_b import reframe as reframe_mod
        shots = reframe_mod.build_shot_index([4.0, 9.5], 12.0)
        self.assertEqual(shots[0]["start_seconds"], 0.0)
        self.assertEqual(shots[-1]["end_seconds"], 12.0)
        self.assertEqual(len(shots), 3)
        self.assertEqual(shots[0]["cause"], "video_start")


# --------------------------------------------------------------------------- #
# reframe: detection engine tiers (bug-36 anime, bug-40 animals)               #
# --------------------------------------------------------------------------- #

def _blank_engine():
    """A FaceCenterEngine with no CNN (insightface not needed) and no YOLO,
    suitable for exercising cascade tiers and fallback behaviour."""
    import cv2
    from pipeline.stage_b import reframe as reframe_mod
    eng = object.__new__(reframe_mod.FaceCenterEngine)
    eng._cv2 = cv2
    eng._stylised_cascades = reframe_mod._load_stylised_cascades(cv2)
    eng._animal_cascades = reframe_mod._load_animal_cascades(cv2)
    eng._yolo = None
    eng._yolo_tried = True  # never try to construct a real YOLO in tests

    class _NoCNN:
        def get(self, image):
            return []
    eng.app = _NoCNN()
    return eng


class ReframeDetectionTierTests(unittest.TestCase):
    def test_vendored_anime_cascade_loads(self) -> None:
        """bug-40: lbpcascade_animeface.xml is vendored in the repo and must
        resolve even though opencv-python-headless does not ship it."""
        import cv2
        from pipeline.stage_b import reframe as reframe_mod
        self.assertTrue(
            os.path.isfile(os.path.join(reframe_mod._BUNDLED_CASCADE_DIR, "lbpcascade_animeface.xml"))
        )
        cascades = reframe_mod._load_stylised_cascades(cv2)
        self.assertGreaterEqual(len(cascades), 1)

    def test_cat_face_cascades_load_from_opencv_bundle(self) -> None:
        import cv2
        from pipeline.stage_b import reframe as reframe_mod
        cascades = reframe_mod._load_animal_cascades(cv2)
        self.assertGreaterEqual(len(cascades), 1)

    def test_analyze_missing_image_returns_empty(self) -> None:
        eng = _blank_engine()
        self.assertEqual(eng.analyze("/definitely/not/here.png"), [])

    def test_analyze_blank_image_no_detectors_no_crash(self) -> None:
        import numpy as np
        eng = _blank_engine()
        eng._stylised_cascades = []
        eng._animal_cascades = []
        img = np.full((64, 64, 3), 127, np.uint8)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            import cv2
            cv2.imwrite(path, img)
            self.assertEqual(eng.analyze(path), [])
        finally:
            os.unlink(path)

    def test_yolo_tier_keeps_animals_drops_people(self) -> None:
        """bug-40: YOLO detections are filtered to COCO animal classes only;
        the surviving animal box drives the crop centre and confidence."""
        import cv2
        import numpy as np
        eng = _blank_engine()
        eng._stylised_cascades = []
        eng._animal_cascades = []

        class _Boxes:
            cls = type("T", (), {"tolist": lambda s: [0, 16]})()      # person, dog
            conf = type("T", (), {"tolist": lambda s: [0.9, 0.8]})()
            xyxy = type("T", (), {"tolist": lambda s: [[10, 10, 100, 100], [200, 200, 400, 400]]})()

        class _Res:
            boxes = _Boxes()

        class _FakeYOLO:
            names = {0: "person", 16: "dog"}
            def predict(self, image, verbose=False):
                return [_Res()]

        eng._yolo = _FakeYOLO()
        img = np.full((480, 640, 3), 127, np.uint8)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            cv2.imwrite(path, img)
            dets = eng.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(dets), 1)
        self.assertAlmostEqual(dets[0]["center_x"], (200 + 400) / (2 * 640))
        self.assertAlmostEqual(dets[0]["center_y"], (200 + 400) / (2 * 480))
        self.assertEqual(dets[0]["confidence"], 0.8)

    def test_yolo_tier_respects_confidence_threshold(self) -> None:
        import cv2
        import numpy as np
        eng = _blank_engine()
        eng._stylised_cascades = []
        eng._animal_cascades = []

        class _Boxes:
            cls = type("T", (), {"tolist": lambda s: [16]})()          # dog
            conf = type("T", (), {"tolist": lambda s: [0.2]})()        # below 0.4
            xyxy = type("T", (), {"tolist": lambda s: [[0, 0, 50, 50]]})()

        class _Res:
            boxes = _Boxes()

        class _FakeYOLO:
            names = {16: "dog"}
            def predict(self, image, verbose=False):
                return [_Res()]

        eng._yolo = _FakeYOLO()
        img = np.full((64, 64, 3), 127, np.uint8)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            cv2.imwrite(path, img)
            self.assertEqual(eng.analyze(path), [])
        finally:
            os.unlink(path)

    def test_yolo_load_failure_is_swallowed(self) -> None:
        """If ultralytics is absent/broken the engine must still work."""
        eng = _blank_engine()
        eng._yolo_tried = False  # force a real load attempt
        import builtins
        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name == "ultralytics":
                raise RuntimeError("no ultralytics in test")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = _boom
        try:
            self.assertIsNone(eng._yolo_model())
            self.assertTrue(eng._yolo_tried)
        finally:
            builtins.__import__ = real_import


# --------------------------------------------------------------------------- #
# run: metadata + zip packaging                                                #
# --------------------------------------------------------------------------- #

class PackagingTests(unittest.TestCase):
    def test_metadata_txt_sections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = common.normalize_plan(_valid_plan_document())
            out = Path(td) / "metadata.txt"
            stage_b_run.write_metadata_txt(plan, out)
            text = out.read_text(encoding="utf-8")
            self.assertIn("TITLE:\nA Test Video", text)
            self.assertIn("#one #two #three #four #five", text)
            self.assertIn("tag0, tag1", text)

    def test_metadata_blank_sections_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            doc = _valid_plan_document()
            del doc["hashtags"]
            del doc["youtube_tags"]
            del doc["title"]
            plan = common.normalize_plan(doc)
            out = Path(td) / "metadata.txt"
            stage_b_run.write_metadata_txt(plan, out)
            text = out.read_text(encoding="utf-8")
            # Structure stays predictable: each section header is followed by
            # its (blank) content line and a separator blank line.
            self.assertIn("TITLE:", text)
            self.assertIn("HASHTAGS:", text)
            self.assertIn("YOUTUBE TAGS:", text)
            lines = text.splitlines()
            self.assertEqual(lines[lines.index("TITLE:") + 1], "")
            self.assertEqual(lines[lines.index("HASHTAGS:") + 1], "")
            self.assertEqual(lines[lines.index("YOUTUBE TAGS:") + 1], "")

    def test_zip_packaging_verified(self) -> None:
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp4 = td / "final.mp4"
            mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            meta = td / "metadata.txt"
            meta.write_text("TITLE:\nx\n", encoding="utf-8")
            prod = td / "production.json"
            prod.write_text("{}", encoding="utf-8")
            out_zip = td / "final.zip"
            stage_b_run.package_final_zip(mp4, meta, prod, out_zip)
            with zipfile.ZipFile(out_zip) as zf:
                self.assertEqual(set(zf.namelist()), {"final.mp4", "metadata.txt", "production.json"})

    def test_zip_refuses_missing_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with self.assertRaises(common.StageBError):
                stage_b_run.package_final_zip(
                    td / "nope.mp4", td / "metadata.txt", td / "production.json", td / "z.zip"
                )


# --------------------------------------------------------------------------- #
# branding helpers                                                             #
# --------------------------------------------------------------------------- #

class BrandingTests(unittest.TestCase):
    def test_watermark_name_missing_file(self) -> None:
        self.assertEqual(common.load_creator_watermark_name(Path("/nonexistent/wm.json")), "")

    def test_watermark_name_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "creator_watermark.json"
            p.write_text(json.dumps({"creator_name": "  Jane   Doe  "}), encoding="utf-8")
            self.assertEqual(common.load_creator_watermark_name(p), "Jane Doe")

    def test_sanitize_job_id(self) -> None:
        self.assertEqual(common.sanitize_job_id(" manual-123 "), "manual-123")
        self.assertEqual(common.sanitize_job_id("job/with space"), "job-with-space")
        with self.assertRaises(common.StageBError):
            common.sanitize_job_id("!!!")
        with self.assertRaises(common.StageBError):
            common.sanitize_job_id("x" * 121)


# --------------------------------------------------------------------------- #
# constant loudness-matched background gain (bug-59)                          #
# --------------------------------------------------------------------------- #

class MusicGainForRatioTests(unittest.TestCase):
    """bug-59: the background bed must sit at a CONSTANT, loudness-matched
    level for the whole clip (no per-moment ducking layered on top — see
    produce_merged_video, which mixes music_reduced directly rather than
    through sidechaincompress). These tests cover the gain math that
    determines that constant level; they intentionally do not invoke ffmpeg.
    """

    def test_ratio_constant_is_the_halved_target(self) -> None:
        # remove-paste-feature-and-double-voice-ratio: the operator asked for
        # the voice to count double relative to the bed. With the music
        # target formula (voice_lufs + 10*log10(ratio)) unchanged, "voice 2x"
        # is exactly ratio 0.30 / 2 = 0.15. Pin the constant so an accidental
        # edit (e.g. the literal "2.0" misreading, which would put the music
        # LOUDER than the vocals) fails loudly here.
        self.assertEqual(render.MUSIC_TO_VOICE_LOUDNESS_RATIO, 0.15)
        # 10*log10(0.15) = -8.2391 dB offset below the voiceover.
        self.assertAlmostEqual(
            10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO),
            -8.2391,
            places=4,
        )

    def test_quiet_music_is_boosted_to_target_ratio(self) -> None:
        # A much quieter upload (-40 LUFS) under -16 LUFS vocals must be
        # boosted, not left at its own (much lower) natural level.
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=-40.0)
        self.assertGreater(gain_db, 0.0)
        # Hand-computed for ratio 0.15: target = -16 + 10*log10(0.15)
        # = -24.2391 LUFS, so gain = -24.2391 - (-40) = +15.7609 dB.
        self.assertAlmostEqual(gain_db, 15.7609, places=4)
        # Applying the gain should land the music at exactly
        # MUSIC_TO_VOICE_LOUDNESS_RATIO's perceived level relative to voice.
        target_lufs = -16.0 + 10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO)
        self.assertAlmostEqual(target_lufs, -24.2391, places=4)
        self.assertAlmostEqual(-40.0 + gain_db, target_lufs, places=6)

    def test_loud_music_is_cut_down_to_target_ratio(self) -> None:
        # A louder-than-target upload (-10 LUFS) under -16 LUFS vocals must
        # be attenuated down to the same target ratio, not left blaring.
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=-10.0)
        self.assertLess(gain_db, 0.0)
        # Hand-computed for ratio 0.15: gain = -16 - 8.2391 - (-10)
        # = -14.2391 dB.
        self.assertAlmostEqual(gain_db, -14.2391, places=4)
        target_lufs = -16.0 + 10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO)
        self.assertAlmostEqual(-10.0 + gain_db, target_lufs, places=6)

    def test_new_ratio_is_quieter_than_old_030_for_same_inputs(self) -> None:
        # Direct regression test for the operator's goal: for the SAME
        # voice/music LUFS pair, the new 0.15 ratio must attenuate the bed
        # MORE (a lower, more negative gain) than the old 0.30 ratio would
        # have. The exact delta is
        #     10*log10(0.15) - 10*log10(0.30) = -3.0103 dB
        # for every measurement pair, because the offset enters linearly.
        for voice_lufs, music_lufs in ((-16.0, -10.0), (-20.0, -8.0), (-23.0, -6.0), (-16.0, -40.0)):
            old_gain = (voice_lufs + 10.0 * math.log10(0.30)) - music_lufs
            new_gain = render.music_gain_db_for_ratio(
                voice_lufs=voice_lufs, music_lufs=music_lufs
            )
            self.assertLess(new_gain, old_gain)
            self.assertAlmostEqual(new_gain - old_gain, -3.0103, places=4)
            # The resulting bed loudness is also exactly 3.0103 dB lower.
            self.assertAlmostEqual(
                (music_lufs + new_gain) - (music_lufs + old_gain),
                -3.0103,
                places=4,
            )

    def test_gain_clamped_to_sane_bounds(self) -> None:
        # A pathologically quiet upload should be boosted heavily but never
        # past MUSIC_GAIN_MAX_DB (so the bed can't blast).
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=-90.0)
        self.assertEqual(gain_db, render.MUSIC_GAIN_MAX_DB)
        # A pathologically loud reading (tens of dB above digital full scale
        # — physically impossible for real audio) should be cut heavily but
        # never past MUSIC_GAIN_MIN_DB (so a corrupt measurement can't mute
        # the bed by a physically meaningless amount).
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=80.0)
        self.assertEqual(gain_db, render.MUSIC_GAIN_MIN_DB)

    def test_loud_music_minus8_lufs_reaches_exact_ratio(self) -> None:
        # Regression anchor for the operator-reported failure: a loud
        # commercial-style upload (-8 LUFS, well inside the -6..-14 LUFS
        # range such masters measure at) under -16 LUFS vocals must land
        # EXACTLY on the ratio target — never floored by MUSIC_GAIN_MIN_DB.
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=-8.0)
        # Hand-computed for ratio 0.15: gain = -16 - 8.2391 - (-8) = -16.2391 dB.
        self.assertAlmostEqual(gain_db, -16.2391, places=4)
        target_lufs = -16.0 + 10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO)
        self.assertAlmostEqual(-8.0 + gain_db, target_lufs, places=6)
        self.assertGreater(gain_db, render.MUSIC_GAIN_MIN_DB)

    def test_cut_beyond_old_floor_still_reaches_exact_ratio(self) -> None:
        # The old -40 dB floor would clamp any required cut deeper than
        # -40 dB, leaving the bed audibly above the ratio. The recalibrated
        # floor must let a -45 dB-class requirement through unclamped. This
        # test FAILS against the old bound (which returned exactly -40.0
        # here) and PASSES with the recalibrated floor.
        voice_lufs, music_lufs = -23.0, 12.0
        required_db = (
            voice_lufs
            + 10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO)
            - music_lufs
        )
        # Hand-computed for ratio 0.15: -23 - 8.2391 - 12 = -43.2391 dB.
        self.assertAlmostEqual(required_db, -43.2391, places=4)
        self.assertLess(required_db, -40.0)  # old floor would have clamped this
        gain_db = render.music_gain_db_for_ratio(voice_lufs=voice_lufs, music_lufs=music_lufs)
        self.assertAlmostEqual(gain_db, required_db, places=6)
        # Applying the returned gain lands the music on the exact target.
        target_lufs = voice_lufs + 10.0 * math.log10(render.MUSIC_TO_VOICE_LOUDNESS_RATIO)
        self.assertAlmostEqual(music_lufs + gain_db, target_lufs, places=6)

    def test_pathological_measurement_still_clamped_at_new_floor(self) -> None:
        # The protection must still exist at the recalibrated bound: a
        # measurement tens of dB above digital full scale (impossible for
        # real audio) demands a cut beyond MUSIC_GAIN_MIN_DB and must be
        # clamped rather than producing an absurd gain.
        gain_db = render.music_gain_db_for_ratio(voice_lufs=-16.0, music_lufs=80.0)
        self.assertEqual(gain_db, render.MUSIC_GAIN_MIN_DB)
        # -60 dB of cut still leaves a nonzero (if tiny) bed — the clamp
        # prevents a meaningless full mute from a corrupt reading.
        self.assertGreater(10.0 ** (gain_db / 20.0), 0.0)

    def test_gain_is_constant_regardless_of_voiceover_dynamics(self) -> None:
        # bug-59: the whole point of this function is that it returns ONE
        # gain value for the ENTIRE clip, computed once from each track's
        # single integrated-loudness measurement. It takes no per-sample or
        # per-moment signal, so calling it twice with the same two
        # measurements must always return the identical constant gain —
        # there is nothing in this function that could vary by playback
        # position, unlike the sidechaincompress stage this bug removed.
        first = render.music_gain_db_for_ratio(voice_lufs=-18.5, music_lufs=-27.2)
        second = render.music_gain_db_for_ratio(voice_lufs=-18.5, music_lufs=-27.2)
        self.assertEqual(first, second)

    def test_no_ducking_constants_remain(self) -> None:
        # bug-59: sidechain ducking was removed entirely; these names must
        # no longer exist as module attributes so nothing can accidentally
        # reintroduce a dependency on them.
        for name in (
            "MUSIC_DUCK_THRESHOLD",
            "MUSIC_DUCK_RATIO",
            "MUSIC_DUCK_ATTACK_MS",
            "MUSIC_DUCK_RELEASE_MS",
        ):
            self.assertFalse(
                hasattr(render, name),
                f"render.{name} should have been removed with sidechain ducking (bug-59)",
            )


if __name__ == "__main__":
    unittest.main()
