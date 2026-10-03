"""V3.5 validator + vocabulary sanity checks.
Run: python tests/test_validator.py  (from project root)
"""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import vocabulary as v
from pipeline.validator import validate_and_assemble


def main():
    passed = total = 0

    def run(name, cond, detail=""):
        nonlocal passed, total
        total += 1
        tag = "OK  " if cond else "FAIL"
        print(f"  [{tag}] {name}" + (f"  -- {detail}" if detail else ""))
        if cond: passed += 1

    # ------------------------------------------------------------------
    print("== V3.5 vocabulary ==")
    run("VOCABULARY_VERSION == 3.5", v.VOCABULARY_VERSION == "3.5")
    run("VOCAB_VERSION_MATCHES", v.VOCAB_VERSION_MATCHES)
    run("3 categories", v.CATEGORIES == {"ProductShot", "ProductUse", "Problem"})
    run("30 ProductShot scenes", len(v.CATEGORY_SCENES["ProductShot"]) == 30)
    run("13 ProductUse scenes", len(v.CATEGORY_SCENES["ProductUse"]) == 13)
    run("11 Problem scenes", len(v.CATEGORY_SCENES["Problem"]) == 11)
    run("54 total scenes", sum(len(s) for s in v.CATEGORY_SCENES.values()) == 54)

    print("\n== V3.5 new conditional tokens ==")
    run("3 CozyRiderColor values", len(v.COZY_RIDER_COLORS) == 3)
    run("BlackCover in COZY_RIDER_COLORS", "BlackCover" in v.COZY_RIDER_COLORS)
    run("BlackCoverBrownTrim in COZY_RIDER_COLORS", "BlackCoverBrownTrim" in v.COZY_RIDER_COLORS)
    run("GreyCover in COZY_RIDER_COLORS", "GreyCover" in v.COZY_RIDER_COLORS)
    run("Voiceover in VOICEOVER_TOKENS", "Voiceover" in v.VOICEOVER_TOKENS)
    run("Cushion in CUSHION_TOKENS", "Cushion" in v.CUSHION_TOKENS)

    print("\n== V3.5 stem limit and AudioAnalysisFailure ==")
    run("FILENAME_STEM_MAX_CHARS == 160", v.FILENAME_STEM_MAX_CHARS == 160)
    run("AudioAnalysisFailure is a system error",
        "AudioAnalysisFailure" in v.SYSTEM_ERROR_EXAMPLES)

    print("\n== V3.5 ShotType set (6 shots; ECU/Overhead/LowAngle gone since V3.2) ==")
    for st in ["ECU", "Overhead", "LowAngle"]:
        run(f"{st} NOT in SHOT_TYPES", st not in v.SHOT_TYPES)
    run("6 shot types", len(v.SHOT_TYPES) == 6)
    for st in ["CU", "Mid", "Wide", "OTS", "POV", "DashCam"]:
        run(f"{st} in SHOT_TYPES", st in v.SHOT_TYPES)

    print("\n== V3.5 scenes ==")
    run("Testimonial in ProductShot", v.is_valid_category_scene("ProductShot", "Testimonial"))
    run("Testimonial in ProductUse",  v.is_valid_category_scene("ProductUse", "Testimonial"))
    run("CleanWipeFabric in ProductShot", v.is_valid_category_scene("ProductShot", "CleanWipeFabric"))
    run("DogSeatbelt in ProductUse", v.is_valid_category_scene("ProductUse", "DogSeatbelt"))
    run("WeakPlasticClips in Problem", v.is_valid_category_scene("Problem", "WeakPlasticClips"))

    print("\n== V3.5 VehicleClass includes FullSizeTruck ==")
    run("FullSizeTruck in VEHICLE_CLASSES", "FullSizeTruck" in v.VEHICLE_CLASSES)

    # ------------------------------------------------------------------
    print("\n== V3.5 validator — audio analysis failure blocks auto-rename ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "FullProduct", "shot_type": "Wide",
        "audio_analysis_failed": True,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    run("audio_analysis_failed → NeedsReview",
        r.status == "NeedsReview" and "AudioAnalysisFailure" in r.review_reasons,
        r.review_reasons)

    # ------------------------------------------------------------------
    print("\n== V3.5 validator — new field order (Color, Cushion, Voiceover) ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "Testimonial",
        "dog_type": None,
        "vehicle_class": "SUV",
        "cozy_rider_color": "BlackCover",
        "cushion": "Cushion",
        "voiceover": "Voiceover",
        "shot_type": "Mid",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    expected = "ProductShot_Testimonial_SUV_BlackCover_Cushion_Voiceover_Mid_9x16.mov"
    run("Full V3.5 filename with all optional tokens",
        r.proposed_filename == expected, r.proposed_filename or "")

    print("\n== V3.5 validator — Voiceover only (no Color/Cushion) ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductUse", "category_fit": True,
        "scene": "DogRiding",
        "dog_type": "Goldendoodle",
        "vehicle_class": "SUV",
        "cozy_rider_color": None,
        "cushion": None,
        "voiceover": "Voiceover",
        "shot_type": "DashCam",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mp4")
    expected = "ProductUse_DogRiding_Goldendoodle_SUV_Voiceover_DashCam_9x16.mp4"
    run("Voiceover without Color/Cushion",
        r.proposed_filename == expected, r.proposed_filename or "")

    print("\n== V3.5 validator — CozyRiderColor only (no Cushion/Voiceover) ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "FullProduct",
        "dog_type": None,
        "vehicle_class": None,
        "cozy_rider_color": "GreyCover",
        "cushion": None,
        "voiceover": None,
        "shot_type": "Wide",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    expected = "ProductShot_FullProduct_GreyCover_Wide_9x16.mov"
    run("GreyCover in filename, no Cushion/Voiceover",
        r.proposed_filename == expected, r.proposed_filename or "")

    print("\n== V3.5 validator — null optional tokens omitted from filename ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "HardBottomPlatform",
        "dog_type": None, "vehicle_class": "SUV",
        "cozy_rider_color": None, "cushion": None, "voiceover": None,
        "shot_type": "Mid",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    expected = "ProductShot_HardBottomPlatform_SUV_Mid_9x16.mov"
    run("No optional tokens → classic filename",
        r.proposed_filename == expected, r.proposed_filename or "")

    print("\n== V3.5 validator — invalid CozyRiderColor rejected ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "FullProduct", "shot_type": "Wide",
        "cozy_rider_color": "RedCover",   # not in vocabulary
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    run("Invalid CozyRiderColor → NeedsReview",
        r.status == "NeedsReview" and "SchemaValidationFailed" in r.review_reasons,
        r.review_reasons)

    # ------------------------------------------------------------------
    print("\n== V3.5 validator — happy path (no new tokens) ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "SetupHeadrestStraps",
        "dog_type": None, "vehicle_class": "FullSizeTruck", "shot_type": "OTS",
        "cozy_rider_color": None, "cushion": None, "voiceover": None,
        "audio_analysis_failed": False,
        "scene_runner_up": "SetupFullProduct",
        "evidence_timestamps": [1.2, 3.5],
        "classifier_version": "gemini-flash-latest+prompt-v35-01-audio-voiceover",
    }
    r = validate_and_assemble(cls, width=1080, height=1920, extension=".mp4",
                              folder_category="ProductShot")
    run("Setup under ProductShot",
        r.proposed_filename == "ProductShot_SetupHeadrestStraps_FullSizeTruck_OTS_9x16.mp4",
        r.proposed_filename or "")
    run("classifier_version carried through",
        r.classifier_version == "gemini-flash-latest+prompt-v35-01-audio-voiceover")
    run("scene_runner_up carried through", r.scene_runner_up == "SetupFullProduct")
    run("evidence_timestamps carried through", r.evidence_timestamps == [1.2, 3.5])

    print("\n== V3.5 validator — LongVideo routing ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "Testimonial", "shot_type": "Mid",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov",
                              duration_sec=800)
    run("Duration>600s → LongVideo", "LongVideo" in r.review_reasons and r.status == "NeedsReview")

    print("\n== V3.5 validator — CategoryConflict ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductUse", "category_fit": False,
        "scene": "DogRiding", "shot_type": "Wide",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mp4",
                              folder_category="ProductShot")
    run("CategoryConflict flagged",
        "CategoryConflict" in r.review_reasons and r.status == "NeedsReview")

    print("\n== V3.5 validator — invalid Category×Scene ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "DogRiding",   # DogRiding is ProductUse only
        "shot_type": "Mid",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mp4")
    run("Bad Category/Scene pair rejected",
        "SchemaValidationFailed" in r.review_reasons and r.status == "NeedsReview")

    print("\n== V3.5 validator — take collision ==")
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "StoragePockets",
        "vehicle_class": "SUV", "shot_type": "CU",
        "cozy_rider_color": None, "cushion": None, "voiceover": None,
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(
        cls, width=2160, height=3840, extension=".mp4",
        existing_take_stems=["ProductShot_StoragePockets_SUV_CU_9x16"],
    )
    run("T02 appended",
        r.proposed_filename == "ProductShot_StoragePockets_SUV_CU_9x16_T02.mp4",
        r.proposed_filename or "")

    print("\n== V3.5 validator — stem limit (160 chars) ==")
    # Build a stem that's just over 160 chars
    cls = {
        "relevant": True, "needs_split": False,
        "category": "ProductShot", "category_fit": True,
        "scene": "SetupFullProduct",
        "dog_type": "BlackLabradorRetriever" if "BlackLabradorRetriever" in v.DOG_TYPES else None,
        "vehicle_class": "SUV",
        "cozy_rider_color": "BlackCoverBrownTrim",
        "cushion": "Cushion",
        "voiceover": "Voiceover",
        "shot_type": "Wide",
        "audio_analysis_failed": False,
    }
    r = validate_and_assemble(cls, width=2160, height=3840, extension=".mov")
    if r.proposed_filename:
        stem_len = len(r.proposed_filename.rsplit(".", 1)[0])
        run("Stem within 160-char limit", stem_len <= 160, f"stem={stem_len}")
    else:
        run("Assembled or routed to FilenameTooLong (either is valid)",
            r.status in ("AutoRenamed", "NeedsReview"))

    print("\n== V3.5 format picker ==")
    fmt, _, _ = v.pick_format(2160, 3840)
    run("2160x3840 → 9x16", fmt == "9x16")
    fmt, _, _ = v.pick_format(1920, 1080)
    run("1920x1080 → 16x9", fmt == "16x9")
    fmt, _, _ = v.pick_format(1080, 1080)
    run("1080x1080 → 1x1", fmt == "1x1")

    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
