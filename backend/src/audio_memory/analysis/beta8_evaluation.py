from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from audio_memory.prompts.beta8_composer import Beta8PromptComposer
from audio_memory.prompts.beta8_event_index_schema import Beta8NormalizedEventIndex
from audio_memory.prompts.beta8_pipeline_schema import AggregatedAudit


EXPECTED_DOUBAO_MANIFEST: dict[str, object] = {
    "schema_version": 1,
    "provider": "豆包",
    "model": "录音文件识别 2.0",
    "resource_id": "volc.seedasr.auc",
    "file_count": 4,
    "segment_count": 6_373,
    "sha256": "6cb6881073d769f91639eb478d5c8dadf96be0431016885ddddd7e8f16cb20b8",
    "ground_truth_sha256": (
        "c232c736e42c0676d8c77d3250030ba025542ec73f79d78e0d9b75210fba8cb0"
    ),
}
EXPECTED_STAGES: dict[str, list[str]] = {
    "normal_v1_model_calls": ["event_index", "all_scenes_v1"],
    "required_model_stages": [
        "event_index",
        "all_scenes_v1",
        "initial_audit",
        "global_editorial_review",
        "final_audit",
    ],
    "conditional_model_stages": ["targeted_revision"],
    "conditional_non_model_stages": ["native_search"],
    "repair_attempt_kinds": [
        "schema_repair",
        "retry",
        "resume",
    ],
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREWRITTEN_KEYS = frozenset({
    "expected_report_markdown",
    "expected_card_markdown",
    "expected_title",
    "report_markdown",
    "card_markdown",
    "model_answer",
    "reference_answer",
})
_REQUIRED_BLIND_REVIEW_HARD_BLOCKERS = frozenset({
    "主体错误",
    "工作沟通拆分或误合并",
    "未解决事实错误",
    "搜索来源错配",
    "未完成修订要求",
})
_REQUIRED_BLIND_REVIEW_FIELDS = frozenset({
    "reviewer",
    "label",
    "dimension",
    "score",
    "evidence",
    "notes",
})
_EXPECTED_BLIND_REVIEW_LABELS = ["candidate_a", "candidate_b"]
_EXPECTED_BLIND_REVIEW_SCALE = {"minimum": 1, "maximum": 5}
_EXPECTED_BLIND_REVIEW_DIMENSIONS = [
    "完整覆盖与防漏",
    "工作沟通边界正确",
    "主体与表达用途正确",
    "非工作内容价值准入",
    "单卡信息密度与核心判断",
    "结构和内容类型匹配",
    "建议的可直接使用性",
    "直接表达且无第三方报告腔",
    "Markdown 主标题与编号正确",
    "引用正文与来源一一对应",
    "本次运行计量完整",
]


@dataclass(frozen=True, slots=True)
class EvaluationPaths:
    transcript: Path
    manifest: Path
    ground_truth: Path
    output_root: Path


def _segment_ordinal(segment_id: str) -> tuple[int, int]:
    match = re.fullmatch(r"seg_(\d+)_(\d+)", segment_id)
    if match is None:
        raise ValueError(f"Invalid segment ID in communication boundary gate: {segment_id}")
    return int(match.group(1)), int(match.group(2))


def _ranges_overlap(index_range, truth_range: dict[str, Any]) -> bool:
    index_start = _segment_ordinal(index_range.start_segment_id)
    index_end = _segment_ordinal(index_range.end_segment_id)
    truth_start = _segment_ordinal(str(truth_range.get("start_segment_id")))
    truth_end = _segment_ordinal(str(truth_range.get("end_segment_id")))
    if (
        index_start[0] != truth_start[0]
        or index_end[0] != truth_end[0]
    ):
        return False
    return max(index_start, truth_start) <= min(index_end, truth_end)


def _ranges_cover(index_ranges, truth_range: dict[str, Any]) -> bool:
    """Check the union, so a one-segment overlap cannot stand for full coverage."""
    truth_start = _segment_ordinal(str(truth_range.get("start_segment_id")))
    truth_end = _segment_ordinal(str(truth_range.get("end_segment_id")))
    if truth_start[0] != truth_end[0] or truth_start > truth_end:
        raise ValueError("Invalid ground truth communication range")
    intervals = []
    for source_range in index_ranges:
        start = _segment_ordinal(source_range.start_segment_id)
        end = _segment_ordinal(source_range.end_segment_id)
        if start[0] == end[0] == truth_start[0]:
            intervals.append((start[1], end[1]))
    next_required = truth_start[1]
    for start, end in sorted(intervals):
        if end < next_required:
            continue
        if start > next_required:
            return False
        next_required = end + 1
        if next_required > truth_end[1]:
            return True
    return False


def validate_event_index_communication_boundaries(
    event_index: Beta8NormalizedEventIndex,
    ground_truth: dict[str, Any],
) -> dict[str, Any]:
    """Require one and only one indexed work unit per annotated communication."""
    expected = ground_truth.get("work_communications")
    if not isinstance(expected, list):
        raise ValueError("Ground truth work_communications must be a list")
    actual = [
        session
        for session in event_index.primary_sessions
        if session.is_work_communication
    ]
    if len(actual) != len(expected):
        raise ValueError(
            "Ground truth communication boundary mismatch: "
            f"expected {len(expected)} work communications, got {len(actual)}"
        )

    matched_unit_ids: list[str] = []
    matched_event_ids: list[str] = []
    for event in expected:
        if not isinstance(event, dict) or not isinstance(event.get("ranges"), list):
            raise ValueError("Ground truth communication boundary entry is invalid")
        event_ranges = event["ranges"]
        candidates = [
            unit
            for unit in actual
            if all(
                any(_ranges_overlap(index_range, truth_range) for index_range in unit.ranges)
                for truth_range in event_ranges
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                "Ground truth communication boundary mismatch: "
                f"event {event.get('event_id')!r} matched {len(candidates)} index units"
            )
        if not all(_ranges_cover(candidates[0].ranges, item) for item in event_ranges):
            raise ValueError(
                "Ground truth communication boundary mismatch: "
                f"event {event.get('event_id')!r} is only partially covered by "
                f"index unit {candidates[0].session_id!r}"
            )
        matched_unit_ids.append(candidates[0].session_id)
        matched_event_ids.append(str(event.get("event_id")))

    if len(set(matched_unit_ids)) != len(matched_unit_ids):
        raise ValueError(
            "Ground truth communication boundary mismatch: distinct real "
            "communications were merged into one index unit"
        )
    if set(matched_unit_ids) != {session.session_id for session in actual}:
        raise ValueError(
            "Ground truth communication boundary mismatch: an indexed work "
            "communication does not match a real communication"
        )
    return {
        "expected_work_communication_count": len(expected),
        "actual_work_communication_count": len(actual),
        "matched_event_ids": matched_event_ids,
    }


def evaluate_event_index_acceptance(
    event_index: Beta8NormalizedEventIndex, ground_truth: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate annotated facts without equating range coverage with semantic quality.

    Used only by the acceptance entrypoint, never included in model input.
    The caller must still inspect descriptions, subject attribution and V1 prose.
    """
    blockers: list[str] = []
    checks: list[dict[str, Any]] = []
    try:
        boundaries = validate_event_index_communication_boundaries(event_index, ground_truth)
    except ValueError as error:
        boundaries = None
        blockers.append(str(error))
    primary = [(item.session_id, item.ranges) for item in event_index.primary_sessions]
    embedded = [(item.event_id, item.evidence_ranges) for item in event_index.embedded_events]
    media = [
        (item.session_id, item.ranges) for item in event_index.primary_sessions
        if item.activity_kind == "content_playback"
        and item.participation_mode == "recorded_or_broadcast_content"
    ] + [
        (item.event_id, item.evidence_ranges) for item in event_index.embedded_events
        if item.event_kind == "content_playback" and item.expression_mode == "media_playback"
    ]
    cases = [
        (item.event_id, item.evidence_ranges) for item in event_index.embedded_events
        if item.event_kind == "third_party_case" and item.expression_mode == "third_party_case"
    ]
    for group, id_key in (
        ("must_cover_media_units", "unit_id"),
        ("third_party_cases", "case_id"),
        ("low_value_exclusions", "exclusion_id"),
    ):
        for fact in ground_truth.get(group, []):
            candidates = media if group == "must_cover_media_units" else (
                cases if group == "third_party_cases" else primary + embedded
            )
            if fact.get("expected_handling") == "index_as_substantive_family_unit_but_do_not_create_standalone_card":
                candidates = [
                    (item.session_id, item.ranges) for item in event_index.primary_sessions
                    if item.activity_kind == "conversation" and item.communication_purpose == "non_work"
                ] + [
                    (item.event_id, item.evidence_ranges)
                    for item in event_index.embedded_events
                    if item.event_kind in {"discussion_topic", "user_commentary"}
                    and item.expression_mode == "user_experience"
                ]
            allowed_exclusions = []
            if fact.get("expected_handling") == "index_or_exclude_with_media_noise_reason_but_do_not_create_card":
                allowed_exclusions = [
                    item.range for item in event_index.coverage_ranges
                    if item.disposition == "excluded" and item.excluded_reason == "noise"
                ]
            fact_ranges = fact["ranges"]
            matched = [
                (unit_id, ranges) for unit_id, ranges in candidates
                if any(_ranges_overlap(source, target) for source in ranges for target in fact_ranges)
            ]
            covered_ranges = [source for _, ranges in matched for source in ranges] + allowed_exclusions
            covered = bool(fact_ranges) and all(
                _ranges_cover(covered_ranges, target) for target in fact_ranges
            )
            if not covered:
                blockers.append(f"Ground truth content mismatch: {fact[id_key]} lacks full coverage with required content type")
            checks.append({
                "fact_id": fact[id_key], "category": group,
                "range_and_type_passed": covered,
                "matched_unit_ids": [unit_id for unit_id, _ in matched],
                "ranges": fact_ranges,
                "semantic_review_requirements": fact.get("must_preserve", [fact.get("expected_handling")]),
            })
    return {
        "schema_version": 1,
        "automatic_status": "failed" if blockers else "passed",
        "semantic_review_status": "pending",
        "work_boundaries": boundaries, "checks": checks, "blockers": blockers,
    }


def build_v1_fact_review(
    result: dict[str, Any], event_index: Beta8NormalizedEventIndex,
    ground_truth: dict[str, Any],
) -> dict[str, Any]:
    """Prepare a reading checklist; a citation is not proof that prose is correct."""
    cards = [
        {"scene_id": scene["scene_id"], **card}
        for scene in result["scene_results"] for card in scene["cards"]
    ]
    units = [(item.session_id, item.ranges) for item in event_index.primary_sessions]
    units += [(item.event_id, item.evidence_ranges) for item in event_index.embedded_events]
    facts = []
    for group, id_key in (
        ("work_communications", "event_id"), ("must_cover_media_units", "unit_id"),
        ("third_party_cases", "case_id"), ("low_value_exclusions", "exclusion_id"),
    ):
        for fact in ground_truth.get(group, []):
            related_units = {
                unit_id for unit_id, ranges in units
                if any(_ranges_overlap(source, target) for source in ranges for target in fact["ranges"])
            }
            related_cards = [card for card in cards if related_units.intersection(card["card_basis"]["source_unit_ids"])]
            direct = []
            for card in cards:
                if any(
                    _segment_ordinal(target["start_segment_id"]) <= _segment_ordinal(segment_id)
                    <= _segment_ordinal(target["end_segment_id"])
                    for segment_id in card.get("source_segment_ids", []) for target in fact["ranges"]
                ):
                    direct.append(card["draft_card_key"])
            facts.append({
                "fact_id": fact[id_key], "category": group, "ranges": fact["ranges"],
                "requirements": fact.get("must_include_topics", fact.get("must_preserve", [fact.get("expected_handling")])),
                "related_card_keys": [card["draft_card_key"] for card in related_cards],
                "direct_evidence_card_keys": direct,
                "omissions": [item for item in result.get("omitted_units", []) if item["unit_id"] in related_units],
            })
    return {"schema_version": 1, "semantic_review_status": "pending", "facts": facts, "cards": cards}


def mark_paid_execution(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        **plan,
        "mode": "paid-run",
        "network_accessed": True,
        "writes_performed": True,
    }


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON object from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def validate_doubao_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("provider") != EXPECTED_DOUBAO_MANIFEST["provider"]:
        raise ValueError("Doubao acceptance requires provider=豆包")
    for field in (
        "schema_version",
        "model",
        "resource_id",
        "file_count",
        "segment_count",
        "sha256",
        "ground_truth_sha256",
    ):
        expected = EXPECTED_DOUBAO_MANIFEST[field]
        if manifest.get(field) != expected:
            raise ValueError(
                f"Doubao acceptance manifest {field} must be {expected!r}"
            )
    for field in ("sha256", "ground_truth_sha256"):
        if not _SHA256.fullmatch(str(manifest[field])):
            raise ValueError(
                f"Doubao acceptance manifest {field} must be lowercase hex"
            )
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != int(manifest["file_count"]):
        raise ValueError("Doubao acceptance manifest files must match file_count")
    names: list[str] = []
    counts: list[int] = []
    for position, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"Manifest files[{position}] must be an object")
        name = item.get("file_name")
        count = item.get("segment_count")
        if not isinstance(name, str) or not name:
            raise ValueError(f"Manifest files[{position}].file_name is required")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError(f"Manifest files[{position}].segment_count is invalid")
        names.append(name)
        counts.append(count)
    if len(names) != len(set(names)):
        raise ValueError("Manifest file_name entries must be unique")
    if sum(counts) != int(manifest["segment_count"]):
        raise ValueError("Manifest file segment_count values must total segment_count")
    return manifest


def _walk_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [
            str(key)
            for key in value
        ] + [nested for child in value.values() for nested in _walk_keys(child)]
    if isinstance(value, list):
        return [nested for child in value for nested in _walk_keys(child)]
    return []


def _require_ranges(
    owner: str,
    ranges: object,
    segment_by_id: dict[str, dict[str, object]],
    position_by_id: dict[str, int],
) -> set[str]:
    if not isinstance(ranges, list) or not ranges:
        raise ValueError(f"Ground truth {owner}.ranges must be non-empty")
    files: set[str] = set()
    for index, source_range in enumerate(ranges):
        if not isinstance(source_range, dict):
            raise ValueError(f"Ground truth {owner}.ranges[{index}] must be an object")
        file_name = source_range.get("file_name")
        start_id = source_range.get("start_segment_id")
        end_id = source_range.get("end_segment_id")
        if start_id not in segment_by_id or end_id not in segment_by_id:
            raise ValueError(f"Ground truth {owner} references an unknown segment")
        start = segment_by_id[str(start_id)]
        end = segment_by_id[str(end_id)]
        if start["file_name"] != file_name or end["file_name"] != file_name:
            raise ValueError(f"Ground truth {owner} range file_name does not match segments")
        if position_by_id[str(start_id)] > position_by_id[str(end_id)]:
            raise ValueError(f"Ground truth {owner} range is reversed")
        files.add(str(file_name))
    return files


def _validate_entries(
    ground_truth: dict[str, Any],
    key: str,
    id_key: str,
    segment_by_id: dict[str, dict[str, object]],
    position_by_id: dict[str, int],
) -> list[dict[str, Any]]:
    entries = ground_truth.get(key)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"Ground truth {key} must be a non-empty list")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Ground truth {key}[{index}] must be an object")
        entry_id = entry.get(id_key)
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            raise ValueError(f"Ground truth {key}[{index}].{id_key} must be unique")
        seen.add(entry_id)
        _require_ranges(
            f"{key}[{index}]",
            entry.get("ranges"),
            segment_by_id,
            position_by_id,
        )
    return entries


def _require_non_empty_text(entry: dict[str, Any], owner: str, field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Ground truth {owner}.{field} must be non-empty")
    return value


def _require_non_empty_text_list(
    entry: dict[str, Any], owner: str, field: str
) -> list[str]:
    value = entry.get(field)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"Ground truth {owner}.{field} must be non-empty")
    if len(value) != len(set(value)):
        raise ValueError(f"Ground truth {owner}.{field} must not contain duplicates")
    return value


def validate_ground_truth(
    ground_truth: dict[str, Any],
    manifest: dict[str, Any],
    transcript_segments: list[dict[str, object]],
) -> dict[str, Any]:
    forbidden = _PREWRITTEN_KEYS.intersection(_walk_keys(ground_truth))
    if forbidden:
        raise ValueError(
            "Ground truth must not contain a prewritten report answer: "
            + ", ".join(sorted(forbidden))
        )
    if ground_truth.get("schema_version") != 1:
        raise ValueError("Ground truth schema_version must be 1")
    if ground_truth.get("source_sha256") != manifest.get("sha256"):
        raise ValueError("Ground truth source_sha256 does not match manifest")
    segment_by_id = {
        str(segment["segment_id"]): segment for segment in transcript_segments
    }
    position_by_id = {
        str(segment["segment_id"]): position
        for position, segment in enumerate(transcript_segments)
    }
    work = _validate_entries(
        ground_truth,
        "work_communications",
        "event_id",
        segment_by_id,
        position_by_id,
    )
    for index, event in enumerate(work):
        topics = event.get("must_include_topics")
        if not isinstance(topics, list) or not topics or not all(
            isinstance(topic, str) and topic.strip() for topic in topics
        ):
            raise ValueError(
                f"Ground truth work_communications[{index}].must_include_topics "
                "must be non-empty"
            )
        files = _require_ranges(
            f"work_communications[{index}]",
            event.get("ranges"),
            segment_by_id,
            position_by_id,
        )
        if bool(event.get("cross_file")) != (len(files) > 1):
            raise ValueError(
                f"Ground truth work_communications[{index}].cross_file is inconsistent"
            )
    media = _validate_entries(
        ground_truth,
        "must_cover_media_units",
        "unit_id",
        segment_by_id,
        position_by_id,
    )
    for index, unit in enumerate(media):
        owner = f"must_cover_media_units[{index}]"
        _require_non_empty_text(unit, owner, "speaker_owner")
        _require_non_empty_text_list(unit, owner, "must_preserve")

    third_party = _validate_entries(
        ground_truth,
        "third_party_cases",
        "case_id",
        segment_by_id,
        position_by_id,
    )
    for index, case in enumerate(third_party):
        owner = f"third_party_cases[{index}]"
        _require_non_empty_text(case, owner, "subject")
        expected = _require_non_empty_text(case, owner, "expected_attribution")
        forbidden = _require_non_empty_text(case, owner, "forbidden_attribution")
        if expected == forbidden:
            raise ValueError(
                f"Ground truth {owner}.forbidden_attribution must differ from "
                "expected_attribution"
            )

    low_value = _validate_entries(
        ground_truth,
        "low_value_exclusions",
        "exclusion_id",
        segment_by_id,
        position_by_id,
    )
    for index, exclusion in enumerate(low_value):
        owner = f"low_value_exclusions[{index}]"
        _require_non_empty_text(exclusion, owner, "reason")
        _require_non_empty_text(exclusion, owner, "expected_handling")

    blind_review = ground_truth.get("human_blind_review")
    if not isinstance(blind_review, dict):
        raise ValueError("Ground truth human_blind_review must be an object")
    labels = _require_non_empty_text_list(
        blind_review, "human_blind_review", "labels"
    )
    if labels != _EXPECTED_BLIND_REVIEW_LABELS:
        raise ValueError(
            "Ground truth human_blind_review.labels must be candidate_a/candidate_b"
        )
    if blind_review.get("scale") != _EXPECTED_BLIND_REVIEW_SCALE:
        raise ValueError(
            "Ground truth human_blind_review.scale must be exactly 1..5"
        )
    dimensions = _require_non_empty_text_list(
        blind_review, "human_blind_review", "dimensions"
    )
    if dimensions != _EXPECTED_BLIND_REVIEW_DIMENSIONS:
        raise ValueError(
            "Ground truth human_blind_review.dimensions must contain the reviewed 11 dimensions"
        )
    hard_blockers = set(_require_non_empty_text_list(
        blind_review, "human_blind_review", "hard_blockers"
    ))
    missing_blockers = _REQUIRED_BLIND_REVIEW_HARD_BLOCKERS - hard_blockers
    if missing_blockers:
        raise ValueError(
            "Ground truth human_blind_review.hard_blockers is missing: "
            + ", ".join(sorted(missing_blockers))
        )
    review_fields = set(_require_non_empty_text_list(
        blind_review, "human_blind_review", "review_fields"
    ))
    missing_fields = _REQUIRED_BLIND_REVIEW_FIELDS - review_fields
    if missing_fields:
        raise ValueError(
            "Ground truth human_blind_review.review_fields is missing: "
            + ", ".join(sorted(missing_fields))
        )
    unexpected_fields = review_fields - _REQUIRED_BLIND_REVIEW_FIELDS
    if unexpected_fields:
        raise ValueError(
            "Ground truth human_blind_review.review_fields has unexpected fields: "
            + ", ".join(sorted(unexpected_fields))
        )
    return ground_truth


def _load_bound_ground_truth(
    path: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read Ground Truth from {path}: {exc}") from exc
    actual_sha256 = sha256(raw).hexdigest()
    expected_sha256 = str(manifest["ground_truth_sha256"])
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Ground Truth sha256 mismatch: expected {expected_sha256}, "
            f"got {actual_sha256}"
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot parse Ground Truth JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a Ground Truth JSON object in {path}")
    return payload


def _validate_transcript_against_manifest(
    transcript: Path,
    manifest: dict[str, Any],
) -> list[dict[str, object]]:
    actual_sha256 = sha256(transcript.read_bytes()).hexdigest()
    if actual_sha256 != manifest["sha256"]:
        raise ValueError(
            f"Transcript sha256 mismatch: expected {manifest['sha256']}, "
            f"got {actual_sha256}"
        )
    rows = parse_merged_transcript(transcript)
    if len(rows) != manifest["segment_count"]:
        raise ValueError("Transcript segment_count does not match manifest")
    actual_files: list[dict[str, object]] = []
    for file_item in manifest["files"]:
        file_name = str(file_item["file_name"])
        count = sum(row["file_name"] == file_name for row in rows)
        actual_files.append({"file_name": file_name, "segment_count": count})
    if actual_files != manifest["files"]:
        raise ValueError("Transcript files and per-file segment_count do not match manifest")
    return rows


def _validate_provenance(manifest: dict[str, Any]) -> None:
    path_value = manifest.get("provenance_path")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("Doubao provenance_path is required")
    try:
        source_proof = Path(path_value).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read Doubao provenance proof: {exc}") from exc
    required = (
        "模型：豆包录音文件识别 2.0",
        "资源 ID：volc.seedasr.auc",
    )
    if any(item not in source_proof for item in required):
        raise ValueError("ASR provenance does not prove the required Doubao source")
    for file_item in manifest["files"]:
        stem = Path(str(file_item["file_name"])).stem
        proof_line = f"{stem}: {file_item['segment_count']} 段"
        if proof_line not in source_proof:
            raise ValueError(
                f"ASR provenance does not prove manifest file count: {stem}"
            )


def prepare_dry_run(paths: EvaluationPaths) -> dict[str, Any]:
    manifest = validate_doubao_manifest(load_json_object(paths.manifest))
    ground_truth_payload = _load_bound_ground_truth(paths.ground_truth, manifest)
    _validate_provenance(manifest)
    rows = _validate_transcript_against_manifest(paths.transcript, manifest)
    ground_truth = validate_ground_truth(
        ground_truth_payload, manifest, rows
    )
    current_prompt_hashes = [
        {"prompt_id": item["prompt_id"], "sha256": item["sha256"]}
        for item in Beta8PromptComposer.prompt_manifest()
    ]
    expected_prompt_hashes = manifest.get("prompt_hashes")
    if expected_prompt_hashes != current_prompt_hashes:
        raise ValueError("Prompt hashes do not match the acceptance manifest")
    fixed_rules_hash = Beta8PromptComposer.fixed_rules_hash()
    if manifest.get("fixed_rules_hash") != fixed_rules_hash:
        raise ValueError("Prompt fixed_rules_hash does not match the acceptance manifest")
    run_id = f"run-{uuid4()}"
    output_directory = paths.output_root / run_id
    return {
        "mode": "dry-run",
        "network_accessed": False,
        "writes_performed": False,
        "run_id": run_id,
        "planned_output_directory": str(output_directory),
        "input": {
            "transcript_path": str(paths.transcript),
            "manifest_path": str(paths.manifest),
            "ground_truth_path": str(paths.ground_truth),
            "provider": manifest["provider"],
            "model": manifest["model"],
            "resource_id": manifest["resource_id"],
            "sha256": manifest["sha256"],
            "ground_truth_sha256": manifest["ground_truth_sha256"],
            "file_count": manifest["file_count"],
            "segment_count": len(rows),
        },
        "ground_truth": {
            "work_communication_count": len(ground_truth["work_communications"]),
            "media_unit_count": len(ground_truth["must_cover_media_units"]),
            "third_party_case_count": len(ground_truth["third_party_cases"]),
            "low_value_exclusion_count": len(ground_truth["low_value_exclusions"]),
        },
        "prompt_binding": {
            "fixed_rules_hash": fixed_rules_hash,
            "prompt_hashes": current_prompt_hashes,
        },
        "expected_stages": EXPECTED_STAGES,
    }


_FILE_HEADING = re.compile(
    r"^##\s+(.+?\.(?:mp3|aac))(?:\s+逐字稿)?\s*$", re.IGNORECASE
)
_SEGMENT = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+-\s+"
    r"(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\]\s*(.+?)\s*$"
)


def require_paid_confirmation(confirmed: bool) -> None:
    if not confirmed:
        raise PermissionError(
            "Real Beta 8 evaluation requires explicit confirmation of paid model calls"
        )


def final_audit_score(audit: AggregatedAudit) -> int | None:
    """Return the strict publication-gate score without inventing quality weights."""
    if audit.incomplete_audit_unit_ids:
        return None
    return 100 if not audit.issues else 0


def _timestamp_ms(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    return int(
        round(
            (int(hours) * 3_600 + int(minutes) * 60 + float(seconds)) * 1_000
        )
    )


def parse_merged_transcript(source: Path) -> list[dict[str, object]]:
    current_file: str | None = None
    file_position = -1
    segment_index = 0
    rows: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        heading = _FILE_HEADING.match(line)
        if heading:
            current_file = heading.group(1)
            file_position += 1
            segment_index = 0
            continue
        segment = _SEGMENT.match(line)
        if segment is None:
            continue
        if current_file is None:
            raise ValueError(
                f"Transcript segment at line {line_number} has no file heading"
            )
        rows.append(
            {
                "segment_id": f"seg_{file_position}_{segment_index}",
                "file_name": current_file,
                "file_position": file_position,
                "segment_index": segment_index,
                "start_ms": _timestamp_ms(segment.group(1)),
                "end_ms": _timestamp_ms(segment.group(2)),
                "text": segment.group(3),
            }
        )
        segment_index += 1
    if not rows:
        raise ValueError("Transcript contains no timestamped segments")
    return rows
