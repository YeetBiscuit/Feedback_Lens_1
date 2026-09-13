"""Token accounting for the feedback pipeline.

Measures real generation prompts from the database and rebuilds judge prompts
using the same code paths the gate uses, then projects cohort-scale totals
under several optimisation scenarios. Local only - makes no API calls.
"""

import argparse
import json
import sqlite3
import statistics

import tiktoken

from feedback_lens.feedback.quality_gate import (
    GATE_JUDGES,
    _build_feedback_text,
    _load_judge_context,
)
from llm_judge import DIMENSIONS, STRICT_SCORING_PROTOCOL, build_dimension_prompt


DB_PATH = "feedback_system.db"
COHORT_SUBMISSIONS = 20000
ATTEMPT_FACTORS = [1.0, 1.3, 1.5, 2.0]

# Markers used to split a generation prompt into cacheable / dynamic parts.
MARK_METADATA = "Assignment metadata:"
MARK_RETRIEVED = "Retrieved course context that could be helpful:"
MARK_SPEC = "Assignment specification text:"
MARK_SUBMISSION = "Student submission text:"

MARK_JUDGE_MATERIALS = "RETRIEVED COURSE MATERIALS"


def encoder():
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


ENC = encoder()


def tok(text):
    if not text:
        return 0
    return len(ENC.encode(str(text)))


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def split_generation_prompt(prompt_text):
    """Locate the static / dynamic boundaries in a built generation prompt.

    Current layout puts retrieved chunks *before* the assignment spec, which
    truncates the cacheable prefix at the rubric. Reordering the spec ahead of
    the retrieved block would extend it.
    """
    idx_meta = prompt_text.find(MARK_METADATA)
    idx_retrieved = prompt_text.find(MARK_RETRIEVED)
    idx_spec = prompt_text.find(MARK_SPEC)
    idx_sub = prompt_text.find(MARK_SUBMISSION)

    if min(idx_meta, idx_spec, idx_sub) < 0:
        return None

    # No retrieved block in direct mode; spec follows metadata directly.
    boundary = idx_retrieved if idx_retrieved >= 0 else idx_spec

    head = prompt_text[:boundary]
    retrieved = prompt_text[boundary:idx_spec] if idx_retrieved >= 0 else ""
    spec = prompt_text[idx_spec:idx_sub]
    tail = prompt_text[idx_sub:]

    return {
        "head": head,
        "retrieved": retrieved,
        "spec": spec,
        "tail": tail,
        "has_retrieved": idx_retrieved >= 0,
    }


def chunk_materials(conn, generation_id, limit=8):
    """What the judge would see if it read chunks instead of whole documents."""
    rows = conn.execute(
        """
        SELECT DISTINCT mc.chunk_text
        FROM retrieval_records rr
        JOIN material_chunks mc ON mc.chunk_id = rr.chunk_id
        WHERE rr.generation_id = ? AND rr.used_in_prompt = 1
        LIMIT ?
        """,
        (generation_id, limit),
    ).fetchall()
    return "\n\n---\n\n".join(r["chunk_text"] for r in rows)


def judge_prompt_tokens(context, feedback_text, materials_override=None):
    """Token cost of one full judge pass (three single-dimension calls)."""
    materials = context["materials"] if materials_override is None else materials_override

    per_dimension = {}
    criteria_tokens = {}
    cacheable_prefix = None

    for dimension, criteria in DIMENSIONS.items():
        prompt = build_dimension_prompt(
            dimension,
            criteria,
            feedback_text,
            context["submission"],
            context["spec"],
            context["rubric"],
            materials,
            scoring_mode="strict",
        )
        per_dimension[dimension] = tok(prompt)
        criteria_tokens[dimension] = tok(criteria)

        if cacheable_prefix is None:
            cut = prompt.find(MARK_JUDGE_MATERIALS)
            cacheable_prefix = tok(prompt[:cut]) if cut > 0 else 0

    split_total = sum(per_dimension.values())

    # Merging the three dimensions into one call sends the shared context once.
    # Approximate the merged prompt as (one prompt minus its criteria block)
    # plus all three criteria blocks.
    sample_dim = next(iter(DIMENSIONS))
    shared = per_dimension[sample_dim] - criteria_tokens[sample_dim]
    merged_total = shared + sum(criteria_tokens.values())

    return {
        "per_dimension": per_dimension,
        "split_total": split_total,
        "merged_total": merged_total,
        "cacheable_prefix": cacheable_prefix or 0,
        "protocol_tokens": tok(STRICT_SCORING_PROTOCOL),
    }


def measure_one(conn, row):
    generation_id = row["generation_id"]
    prompt_text = row["prompt_text"]

    record = {
        "generation_id": generation_id,
        "gen_input": tok(prompt_text),
        "gen_output": tok(row["raw_response_text"]),
    }

    parts = split_generation_prompt(prompt_text)
    if parts:
        record["gen_cacheable_now"] = tok(parts["head"])
        record["gen_cacheable_reordered"] = tok(parts["head"]) + tok(parts["spec"])
        record["gen_retrieved"] = tok(parts["retrieved"])
        record["gen_spec"] = tok(parts["spec"])
        record["gen_dynamic_tail"] = tok(parts["tail"])

    try:
        feedback_text, data = _build_feedback_text(conn, generation_id)
        context = _load_judge_context(conn, generation_id, dict(data["run"]))
    except Exception as err:
        record["judge_error"] = str(err)[:200]
        return record

    record["feedback_tokens"] = tok(feedback_text)
    record["judge_materials_fulltext"] = tok(context["materials"])

    full = judge_prompt_tokens(context, feedback_text)
    record["judge_fulltext"] = full

    chunks = chunk_materials(conn, generation_id)
    record["judge_materials_chunks"] = tok(chunks)
    record["judge_chunks"] = judge_prompt_tokens(context, feedback_text, chunks)

    return record


def mean(values):
    return statistics.mean(values) if values else 0.0


def summarise(records):
    usable = [r for r in records if "judge_fulltext" in r]
    if not usable:
        raise SystemExit("No generation had enough context to rebuild a judge prompt.")

    n_judges = len(GATE_JUDGES)

    avg = {
        "n_measured": len(usable),
        "n_judges": n_judges,
        "gen_input": mean([r["gen_input"] for r in usable]),
        "gen_output": mean([r["gen_output"] for r in usable]),
        "gen_cacheable_now": mean([r.get("gen_cacheable_now", 0) for r in usable]),
        "gen_cacheable_reordered": mean(
            [r.get("gen_cacheable_reordered", 0) for r in usable]
        ),
        "judge_materials_fulltext": mean([r["judge_materials_fulltext"] for r in usable]),
        "judge_materials_chunks": mean([r["judge_materials_chunks"] for r in usable]),
        "judge_split_fulltext": mean([r["judge_fulltext"]["split_total"] for r in usable]),
        "judge_merged_fulltext": mean([r["judge_fulltext"]["merged_total"] for r in usable]),
        "judge_split_chunks": mean([r["judge_chunks"]["split_total"] for r in usable]),
        "judge_merged_chunks": mean([r["judge_chunks"]["merged_total"] for r in usable]),
        "judge_cacheable_prefix": mean(
            [r["judge_fulltext"]["cacheable_prefix"] for r in usable]
        ),
    }

    scenarios = {
        "S0_current": avg["gen_input"] + n_judges * avg["judge_split_fulltext"],
        "S1_chunk_materials": avg["gen_input"] + n_judges * avg["judge_split_chunks"],
        "S2_merge_dimensions": avg["gen_input"] + n_judges * avg["judge_merged_fulltext"],
        "S3_both": avg["gen_input"] + n_judges * avg["judge_merged_chunks"],
    }

    projection = {}
    for name, per_attempt in scenarios.items():
        projection[name] = {
            f"attempts_{factor}": round(per_attempt * factor * COHORT_SUBMISSIONS)
            for factor in ATTEMPT_FACTORS
        }

    return avg, scenarios, projection


def report(avg, scenarios, projection):
    n = avg["n_judges"]
    print("=" * 74)
    print(f"TOKEN MEASUREMENT  ({avg['n_measured']} generations, {n} judges)")
    print("=" * 74)

    print("\nGeneration (per submission, per attempt)")
    print(f"  input prompt                    {avg['gen_input']:>10,.0f}")
    print(f"  output                          {avg['gen_output']:>10,.0f}")
    print(f"  cacheable prefix, current order {avg['gen_cacheable_now']:>10,.0f}")
    print(f"  cacheable prefix, spec moved up {avg['gen_cacheable_reordered']:>10,.0f}")
    gain = avg["gen_cacheable_reordered"] - avg["gen_cacheable_now"]
    print(f"  -> reordering gains             {gain:>10,.0f}  per call")

    print("\nJudge context: course materials")
    print(f"  whole documents (current)       {avg['judge_materials_fulltext']:>10,.0f}")
    print(f"  retrieved chunks only           {avg['judge_materials_chunks']:>10,.0f}")

    print("\nJudge input per judge, per attempt")
    print(f"  3 separate calls, full docs     {avg['judge_split_fulltext']:>10,.0f}")
    print(f"  3 separate calls, chunks        {avg['judge_split_chunks']:>10,.0f}")
    print(f"  1 merged call, full docs        {avg['judge_merged_fulltext']:>10,.0f}")
    print(f"  1 merged call, chunks           {avg['judge_merged_chunks']:>10,.0f}")
    print(f"  cacheable prefix (before docs)  {avg['judge_cacheable_prefix']:>10,.0f}")

    print("\nInput tokens per submission, per attempt")
    for name, value in scenarios.items():
        ratio = value / scenarios["S0_current"]
        print(f"  {name:<22} {value:>12,.0f}   ({ratio:.2f}x)")

    print(f"\nCohort projection: {COHORT_SUBMISSIONS:,} submissions, input tokens")
    header = "  scenario              " + "".join(
        f"{'x' + str(f):>16}" for f in ATTEMPT_FACTORS
    )
    print(header)
    for name, row in projection.items():
        line = f"  {name:<22}"
        for factor in ATTEMPT_FACTORS:
            line += f"{row[f'attempts_{factor}']:>16,}"
        print(line)

    print("\nNote: attempt factors are a sensitivity range, not measured -")
    print("stored verdicts predate the current judge lineup and MAX_ATTEMPTS.")
    print("tiktoken is an OpenAI tokenizer; other vendors will differ.")


def main():
    parser = argparse.ArgumentParser(description="Measure pipeline token usage.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--limit", type=int, default=12,
                        help="How many recent generations to measure.")
    parser.add_argument("--output", default="exports/token_measurement.json")
    args = parser.parse_args()

    conn = open_db(args.db)
    rows = conn.execute(
        """
        SELECT generation_id, prompt_text, raw_response_text
        FROM generation_runs
        WHERE prompt_text IS NOT NULL
        ORDER BY generation_id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    print(f"Measuring {len(rows)} generations...")
    records = []
    for row in rows:
        record = measure_one(conn, row)
        records.append(record)
        flag = " (judge context unavailable)" if "judge_error" in record else ""
        print(f"  generation {record['generation_id']}: "
              f"gen {record['gen_input']:,}{flag}")

    avg, scenarios, projection = summarise(records)
    report(avg, scenarios, projection)

    payload = {
        "cohort_submissions": COHORT_SUBMISSIONS,
        "attempt_factors": ATTEMPT_FACTORS,
        "averages": avg,
        "per_submission_per_attempt": scenarios,
        "cohort_projection": projection,
        "records": records,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
