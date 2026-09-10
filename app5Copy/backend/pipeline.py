import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from openpyxl import load_workbook

import config
from db import get_conn, get_or_create_agent

# Manifest column header -> internal field name. Matches the sample manifest
# (agent_name, call_number, cti_call_number, call_start_time, call_service_name,
#  call_lead_id, call_end_type_name, call_talk_duration, call_trunk_duration, Recordings).
COLUMN_MAP = {
    "agent_name": "agent_name",
    "call_number": "call_number",
    "cti_call_number": "cti_call_number",
    "call_start_time": "call_start_time",
    "call_service_name": "call_service_name",
    "call_lead_id": "call_lead_id",
    "call_end_type_name": "call_end_type_name",
    "call_talk_duration": "call_talk_duration",
    "call_trunk_duration": "call_trunk_duration",
    "recordings": "recording",
}

DATE_FORMATS = [
    "%m-%d-%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%d-%b-%y %H:%M",
]


def parse_call_date(raw_value):
    """Best-effort parse of call_start_time into a YYYY-MM-DD string for filtering."""
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return raw_value.strftime("%Y-%m-%d")
    text = str(raw_value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_recording_base(cell_value, hyperlink_target=None):
    """
    Pulls the shared base filename out of a manifest 'Recordings' cell, e.g.
      "7002_08320801862_17-Jun-26-18-11-32.wav16"  ->  "7002_08320801862_17-Jun-26-18-11-32"
    Works whether the cell holds a bare filename, a local path, or a full URL/hyperlink.
    """
    source = hyperlink_target or cell_value
    if not source:
        return None
    source = str(source).strip()
    # take just the filename if it's a path or URL
    parsed = urlparse(source)
    filename = os.path.basename(parsed.path) if parsed.scheme else os.path.basename(source)
    # strip extension
    base = re.sub(r"\.wav16?$", "", filename, flags=re.IGNORECASE)
    # strip any existing -IN / -OUT suffix so we always get the shared base
    base = re.sub(r"-(IN|OUT)$", "", base, flags=re.IGNORECASE)
    return base or None


# def build_leg_paths(recording_base, agent_name, recording_base_url):
#     base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
#     folder = requests.utils.quote(agent_name or "")
#     rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
#     tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
#     return rx_path, tx_path
def build_leg_paths(recording_base, agent_name, recording_base_url):
    # base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
    # folder = requests.utils.quote(agent_name or "")
    # # rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
    # rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-IN.wav16"
    # # tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
    # tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-OUT.wav16"
    path,ext=os.path.splitext(recording_base)
    rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-IN{ext}"
    tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-OUT{ext}"
    return rx_path, tx_path


def parse_manifest_file(file_path):
    """Reads the uploaded xlsx and returns a list of row dicts keyed by COLUMN_MAP values."""
    wb = load_workbook(file_path, data_only=True)
    ws = wb.active

    header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    header_index = {}
    for i, h in enumerate(header_row):
        if h is None:
            continue
        key = str(h).strip().lower().replace(" ", "_")
        if key in COLUMN_MAP:
            header_index[COLUMN_MAP[key]] = i

    rows = []
    for excel_row in ws.iter_rows(min_row=2):
        if all(c.value is None for c in excel_row):
            continue
        record = {}
        for field, idx in header_index.items():
            cell = excel_row[idx]
            if field == "recording":
                record["recording_raw"] = cell.value
                record["recording_hyperlink"] = cell.hyperlink.target if cell.hyperlink else None
            else:
                record[field] = cell.value
        rows.append(record)
    return rows


# def save_manifest(file_path, filename, recording_base_url):
#     """Parses the manifest and inserts one 'pending' call row per manifest row."""
#     rows = parse_manifest_file(file_path)
#     conn = get_conn()
#     cur = conn.execute(
#         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
#         "VALUES (?, ?, ?, ?, 'uploaded')",
#         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
#     )
#     manifest_id = cur.lastrowid

#     inserted = 0
#     for r in rows:
#         agent_name = (r.get("agent_name") or "Unassigned").strip()
#         agent_id = get_or_create_agent(conn, agent_name)

#         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
#         rx_path, tx_path = (None, None)
#         if recording_base:
#             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)

#         call_date = parse_call_date(r.get("call_start_time"))

#         conn.execute(
#             """INSERT INTO calls (
#                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
#                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
#                 call_trunk_duration, recording_base, rx_path, tx_path, status
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
#             (
#                 manifest_id, agent_id,
#                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
#                 str(r.get("call_start_time") or ""), call_date,
#                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
#                 r.get("call_end_type_name"), r.get("call_talk_duration"),
#                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
#             ),
#         )
#         inserted += 1

#     conn.commit()
#     conn.close()
#     return manifest_id, inserted

def save_manifest(file_path, filename, recording_base_url):
    """Parses the manifest and inserts one 'pending' call row per manifest row."""
    rows = parse_manifest_file(file_path)
    print(rows)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
        "VALUES (?, ?, ?, ?, 'uploaded')",
        (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
    )
    manifest_id = cur.lastrowid

    inserted = 0
    for r in rows:
        agent_name = (r.get("agent_name") or "Unassigned").strip()
        agent_id = get_or_create_agent(conn, agent_name)
        # print
        recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
        print("****"*12)
        recording_base=r.get("recording_raw")
        print(recording_base,r.get("recording_raw"))
        rx_path, tx_path = (None, None)
        if recording_base:
            rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)
        # rx_path = recording_base
        print("****"*12)
        print(rx_path, tx_path) 
        call_date = parse_call_date(r.get("call_start_time"))

        conn.execute(
            """INSERT INTO calls (
                manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
                call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
                call_trunk_duration, recording_base, rx_path, tx_path, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
            (
                manifest_id, agent_id,
                str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
                str(r.get("call_start_time") or ""), call_date,
                r.get("call_service_name"), str(r.get("call_lead_id") or ""),
                r.get("call_end_type_name"), r.get("call_talk_duration"),
                r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return manifest_id, inserted


def call_stt(rx_path, tx_path):
    resp = requests.post(
        config.STT_API_URL,
        json={"rx_path": rx_path, "tx_path": tx_path},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


# def call_slm(conversation_text, utterances):
#     resp = requests.post(
#         config.SLM_API_URL,
#         json={"transcript": conversation_text},
#         json={"conversation": conversation_text, "utterances": utterances},
#         timeout=config.REQUEST_TIMEOUT_SECONDS, 
#     ) 
#     resp.raise_for_status()   
#     return resp.json() 



#  to get the json text 
# def _dig_for_conversation(obj):
#     """Recursively find the 'conversation' key, unwrapping stringified JSON."""
#     if isinstance(obj, str):
#         try:
#             obj = json.loads(obj)
#         except (json.JSONDecodeError, TypeError):
#             return None
#     if isinstance(obj, dict):
#         if "conversation" in obj and isinstance(obj["conversation"], str):
#             return obj["conversation"]
#         for v in obj.values():
#             found = _dig_for_conversation(v)
#             if found is not None:
#                 return found
#     return None 


# def convert(raw_text: str) -> str: 
#     """ 
#     raw_text: the complete raw text/JSON blob (any nesting level). 
#     returns: a JSON string like {"transcript": "..."}
#     """
#     conversation = _dig_for_conversation(raw_text) 
#     if conversation is None: 
#         raise ValueError("Could not find a 'conversation' field in input.") 
#     return json.dumps({"transcript": conversation.strip()}, ensure_ascii=False, indent=2) 


# from 43570 splitting the text into the parts to get the desired result in one form  

#********************** working ******************************************************************
def call_slm(conversation_text, utterances): 
    print("length of conversation_text:", len(conversation_text))  # Debugging line
    print("?"*100)
    resp = requests.post( 
        config.SLM_API_URL,  
        json={"transcript": conversation_text},   
        timeout=config.REQUEST_TIMEOUT_SECONDS,  
    ) 
    print("SLM API response status code:", resp.status_code)  # Debugging line
    resp.raise_for_status()           
    return resp.json()  


# def call_slm(conversation_text, utterances):
#     chunks = split_conversation(conversation_text)
 
#     if len(chunks) == 1:
#         resp = requests.post(
#             config.SLM_API_URL,
#             json={"transcript": chunks[0]},
#             timeout=config.REQUEST_TIMEOUT_SECONDS,
#         )
#         print("SLM API response status code:", resp.status_code)
#         resp.raise_for_status()
#         return resp.json() 
 
#     # Conversation exceeds the SLM's context limit (config.SLM_CHAR_LIMIT) -
#     # split into utterance-boundary-safe chunks, score each chunk against
#     # the SAME rubric independently, then merge into ONE call-level result.
#     # merge_qa_analyses() below has the actual per-field merge rules - this
#     # is not "just take the last chunk's answer", each field (parameters,
#     # fatal_checks, disposition, summary, etc) has its own sensible combine
#     # logic so the final shape returned here is identical to what a single
#     # non-chunked call would have produced - nothing downstream (process_call,
#     # our /api endpoints, the frontend) needs to know chunking happened at all.
#     print(f"conversation_text is {len(conversation_text)} chars "
#           f"(> {config.SLM_CHAR_LIMIT} limit) - splitting into {len(chunks)} chunks for SLM")
 
#     chunk_analyses = []
#     for i, chunk in enumerate(chunks):
#         resp = requests.post(
#             config.SLM_API_URL,
#             json={"transcript": chunk},
#             timeout=config.REQUEST_TIMEOUT_SECONDS,
#         )
#         print(f"SLM API response status code (chunk {i + 1}/{len(chunks)}):", resp.status_code)
#         resp.raise_for_status()
#         data = resp.json()
#         if "error" in data:
#             raise RuntimeError(f"SLM chunk {i + 1}/{len(chunks)} failed: {data['error']}")
#         chunk_analyses.append(data.get("analysis", data))
 
#     merged = merge_qa_analyses(chunk_analyses)
#     return {"analysis": merged}
 
 
def split_conversation(conversation_text, max_chars=None):
    """
    Splits on utterance-LINE boundaries (never mid-line/mid-sentence), so
    every chunk stays a coherent, independently-readable slice of the call -
    cutting mid-utterance would hand the SLM a garbled fragment instead of
    real dialogue. A single utterance line longer than max_chars on its own
    is kept whole rather than butchered (extremely rare in practice, and a
    corrupted single utterance is a smaller problem than a corrupted split).
    """
    max_chars = max_chars or config.SLM_CHAR_LIMIT
    if len(conversation_text) <= max_chars:
        return [conversation_text]
 
    lines = conversation_text.split("\n")
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for the "\n" that rejoins it
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks
 
 
def merge_qa_analyses(chunk_analyses):
    """
    Combines N chunk-level QA analyses (each only saw part of the call) into
    ONE call-level analysis, matching the exact shape process_call() already
    expects from a single non-chunked SLM response.
 
    Per-field merge rules:
      - parameters: a checklist item counts as satisfied if it was satisfied
        in ANY chunk - "greeted professionally" happening once anywhere in
        the call makes it true for the whole call, regardless of which slice
        contains that moment. Keeps the evidence/reason/timestamps from
        whichever chunk actually found it.
      - total score: recomputed as the sum of the MERGED parameter scores,
        never trusted from any single chunk (each chunk's own "total" only
        reflects its own slice).
      - fatal_checks: a flag raised in ANY chunk surfaces for the whole call -
        compliance/safety signals, a false negative here is worse than a
        false positive.
      - disposition: whole-call outcome judgments usually crystallize near
        the END of a conversation (closing / final decision), so the LAST
        chunk with a non-empty disposition type wins; earlier chunks are
        the fallback only if later ones never actually reached a decision.
      - summary: concatenated across chunks (each only describes its own
        slice, none has the full picture alone).
      - strengths / improvements: unioned + de-duplicated across chunks,
        capped at 5 each to match the rubric's original expected length.
      - fatal_error / verdict: recomputed from the merged results (verdict
        from the merged total score + fatal_error), not copied from any
        single chunk's own judgment of just its slice.
    """
    if len(chunk_analyses) == 1:
        return chunk_analyses[0]
 
    # ---- parameters ----
    param_keys = []
    for a in chunk_analyses:
        for k in a.get("parameters", {}):
            if k != "total" and k not in param_keys:
                param_keys.append(k)
 
    merged_params = {}
    for key in param_keys:
        best = None
        for a in chunk_analyses:
            entry = a.get("parameters", {}).get(key)
            if not entry:
                continue
            if entry.get("status"):
                best = entry
                break
            if best is None:
                best = entry
        merged_params[key] = best or {
            "status": False, "reason": "No supporting evidence found.",
            "evidence": "", "start_time": 0, "end_time": 0, "score": 0,
        }
 
    total_score = sum((merged_params[k].get("score") or 0) for k in param_keys)
    merged_params["total"] = total_score
 
    # ---- fatal_checks ----
    fatal_keys = []
    for a in chunk_analyses:
        for k in a.get("fatal_checks", {}):
            if k not in fatal_keys:
                fatal_keys.append(k)
 
    merged_fatal = {}
    any_fatal_flag = False
    for key in fatal_keys:
        best = None
        for a in chunk_analyses:
            entry = a.get("fatal_checks", {}).get(key)
            if not entry:
                continue
            if entry.get("status"):
                best = entry
                break
            if best is None:
                best = entry
        merged_fatal[key] = best or {"status": False, "reason": "", "evidence": "", "start_time": 0, "end_time": 0}
        if merged_fatal[key].get("status"):
            any_fatal_flag = True
 
    # ---- disposition (last chunk with a real decision wins) ----
    merged_disposition = {
        "type": None, "reason": "", "evidence": "", "start_time": None,
        "end_time": None, "callback_date": "", "callback_time": "",
    }
    for a in reversed(chunk_analyses):
        d = a.get("disposition") or {}
        if d.get("type"):
            merged_disposition = d
            break
 
    # ---- summary / strengths / improvements ----
    summaries = [a.get("summary", "").strip() for a in chunk_analyses if a.get("summary", "").strip()]
    merged_summary = " ".join(summaries)
 
    strengths, improvements = [], []
    for a in chunk_analyses:
        for s in a.get("strengths", []) or []:
            if s not in strengths:
                strengths.append(s)
        for s in a.get("improvements", []) or []:
            if s not in improvements:
                improvements.append(s)
 
    fatal_error = any_fatal_flag or any(a.get("fatal_error") for a in chunk_analyses)
 
    if fatal_error:
        verdict = "Poor"
    elif total_score >= 90:
        verdict = "Excellent"
    elif total_score >= 75:
        verdict = "Good"
    elif total_score >= 60:
        verdict = "Average"
    else:
        verdict = "Poor"
 
    return {
        "parameters": merged_params,
        "summary": merged_summary,
        "fatal_checks": merged_fatal,
        "disposition": merged_disposition,
        "strengths": strengths[:5],
        "improvements": improvements[:5],
        "fatal_error": fatal_error,
        "verdict": verdict,
        "total": total_score,
    }
 






def normalize_score(param_key, raw_score):
    max_points = config.PARAMETER_MAX_POINTS.get(param_key, config.DEFAULT_PARAMETER_MAX_POINTS)
    if max_points <= 0:
        return 0
    # return round(min(raw_score, max_points) / max_points * 100, 1)
    return raw_score



def quality_from_score(score, fatal_error):
    if fatal_error:
        return "poor"
    if score >= 80:
        return "good"
    if score >= 60:
        return "average"
    return "poor"


def sentiment_from_verdict(verdict, fatal_error):
    if fatal_error:
        return "negative"
    v = (verdict or "").lower()
    if v in ("good", "excellent","average"):
        return "positive" 
    if v in ("poor", "bad", "fail"): 
        return "negative" 
    return "negative" 


def normalize_disposition(value):
    """'Not Interested', ' not-interested ', 'NOT_INTERESTED' -> 'NOT INTERESTED'
    so actual (manifest) vs predicted (SLM) compare cleanly regardless of
    whichever casing/punctuation style either side happens to use."""
    if not value:
        return None
    text = str(value).strip().upper()
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text or None


# Alias groups: manifest side and SLM side don't always use same label for
# same real outcome (manifest "LEAD" vs SLM "LEAD_GENERATED"; manifest
# "HANGUP" vs SLM "SHORT_HANGUP"). Mirrors grouping already used for
# dashboard's disp_lead / disp_short_hangup buttons (main.py
# DISPOSITION_CATEGORIES) - keep both in sync, else buckets and
# correct/wrong disagree.
DISPOSITION_ALIAS_GROUPS = [
    {"LEAD", "LEAD GENERATED", "INTERESTED"},
    {"SHORT HANGUP", "HANGUP"},
    {"NOT ELIGIBLE"},
    {"NOT INTERESTED"},
    {"CALLBACK"},
]


def canonicalize_disposition(value):
    """Normalize, then fold into alias group's canonical label so
    equivalent outcomes compare equal regardless of which side's wording
    used. Falls back to normalized text for anything not in a known
    group."""
    norm = normalize_disposition(value)
    if norm is None:
        return None
    for group in DISPOSITION_ALIAS_GROUPS:
        if norm in group:
            return sorted(group)[0]
    return norm


def extract_predicted_disposition(analysis):
    """SLM's actual shape (confirmed): analysis["disposition"] is an object,
    same pattern as a parameter entry -

        "disposition": {
          "type": "INTERESTED",
          "reason": "...",
          "evidence": "...",
          "start_time": 52.51,
          "end_time": 55.41,
          "callback_date": "",
          "callback_time": ""
        }

    Returns a plain dict with those 7 keys, defaulting anything missing to
    None, so callers never need to guard against missing keys. Also accepts
    a bare string (old/simplified SLM response, or a different key name) as
    a fallback so this doesn't hard-break if the contract shifts again -
    everything except "type" just comes back None in that case."""
    value = analysis.get("disposition")
    if value is None:
        # fall back to older/alternate key names in case the contract moves again
        for key in ("predicted_disposition", "call_disposition", "call_end_type"):
            value = analysis.get(key)
            if value:
                break

    if isinstance(value, dict):
        return {
            "type": value.get("type"),
            "reason": value.get("reason"),
            "evidence": value.get("evidence"),
            "start_time": value.get("start_time"),
            "end_time": value.get("end_time"),
            "callback_date": value.get("callback_date") or None,
            "callback_time": value.get("callback_time") or None,
        }
    if value:
        # plain string fallback - just the type, nothing else to show
        return {"type": str(value), "reason": None, "evidence": None, "start_time": None,
                 "end_time": None, "callback_date": None, "callback_time": None}
    return {"type": None, "reason": None, "evidence": None, "start_time": None,
             "end_time": None, "callback_date": None, "callback_time": None}


def process_call(conn, call_row):
    call_id = call_row["id"]
    if not call_row["rx_path"] or not call_row["tx_path"]:
        conn.execute(
            "UPDATE calls SET status='failed', error_message=? WHERE id=?",
            ("Could not resolve recording IN/OUT paths from manifest.", call_id),
        )
        return False

    # 1. Speech to text
    conn.execute("UPDATE calls SET status='transcribing' WHERE id=?", (call_id,))
    conn.commit()
    try:
        stt_result = call_stt(call_row["rx_path"], call_row["tx_path"])
        # print(stt_result[""])
        if len(stt_result)>0:
            print("stt is correct and working----"*10)
    except Exception as e:
        conn.execute(
            "UPDATE calls SET status='failed', error_message=? WHERE id=?",
            (f"STT request failed: {e}", call_id),
        )
        return False 

    conversation_text = stt_result.get("conversation", "")
    utterances = stt_result.get("utterances", [])
    # print("conversation_text:\n\n", conversation_text)
    # print("utterances:\n\n", utterances)
    conn.execute(
        "INSERT INTO transcripts (call_id, conversation_text, utterances_json, raw_json) VALUES (?,?,?,?)",
        (call_id, conversation_text, json.dumps(utterances), json.dumps(stt_result)),
    )
    # print(f"transcript insertred into call id --.{call_id}--"*10)
    # 2. SLM QA analysis
    conn.execute("UPDATE calls SET status='analyzing' WHERE id=?", (call_id,))
    conn.commit()
    try:
        print("calling slm "*10)
        slm_result = call_slm(conversation_text, utterances)
    except Exception as e:
        conn.execute(
            "UPDATE calls SET status='failed', error_message=? WHERE id=?",
            (f"SLM analysis request failed: {e}", call_id),
        )
        return False

    analysis = slm_result.get("analysis", slm_result)
    param_data = analysis.get("parameters", {})
    fatal_data = analysis.get("fatal_checks", {})
    total = analysis.get("total") or param_data.get("total") or 0
    summary = analysis.get("summary", "")
    verdict = analysis.get("verdict", "")
    fatal_error = bool(analysis.get("fatal_error", False))
    disposition = extract_predicted_disposition(analysis)
    predicted_disposition_raw = disposition["type"]

    param_rows = conn.execute("SELECT id, param_key FROM parameters").fetchall()
    param_id_by_key = {p["param_key"]: p["id"] for p in param_rows}

    scores = []
    for key, pid in param_id_by_key.items():
        entry = param_data.get(key)
        if not entry:
            continue
        raw_score = entry.get("score", 0) or 0
        norm_score = normalize_score(key, raw_score)# keeping the score as it is so the ouput looks good 
        # scores.append(norm_score)
        scores.append(raw_score)

        conn.execute(
            """INSERT INTO call_scores
               (call_id, parameter_id, status, reason, evidence, start_time, end_time, raw_score, score)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                call_id, pid, int(bool(entry.get("status"))), entry.get("reason", ""),
                entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
                raw_score, norm_score,
            ),
        )

    for key in config.FATAL_CHECKS:
        entry = fatal_data.get(key, {})
        conn.execute(
            """INSERT INTO fatal_checks (call_id, check_key, status, reason, evidence, start_time, end_time)
               VALUES (?,?,?,?,?,?,?)""",
            (
                call_id, key, int(bool(entry.get("status"))), entry.get("reason", ""),
                entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
            ),
        )

    # overall_score = round(sum(scores) / len(scores), 1) if scores else (total or 0)
    overall_score = sum(scores) if scores else (total or 0)# just sum up the scores 

    overall_quality = quality_from_score(overall_score, fatal_error)
    sentiment = sentiment_from_verdict(verdict, fatal_error)

    # Correct/Wrong: compare the manifest's actual disposition (call_end_type_name,
    # what really happened on the call) against the SLM's predicted one. Only
    # set disposition_match when BOTH sides are actually present - a call
    # missing either value isn't "wrong", it's "not comparable", and should
    # stay out of both buckets rather than silently counting as a miss.
    actual_disposition_canon = canonicalize_disposition(call_row["call_end_type_name"])
    predicted_disposition_canon = canonicalize_disposition(predicted_disposition_raw)
    if actual_disposition_canon and predicted_disposition_canon:
        disposition_match = int(actual_disposition_canon == predicted_disposition_canon)
    else:
        disposition_match = None

    conn.execute(
        """UPDATE calls SET status='audited', overall_score=?, overall_quality=?, verdict=?,
           summary=?, fatal_error=?, sentiment=?, predicted_disposition=?, disposition_match=?,
           disposition_reason=?, disposition_evidence=?, disposition_start_time=?, disposition_end_time=?,
           disposition_callback_date=?, disposition_callback_time=?,
           audited_at = COALESCE(audited_at, NOW())
           WHERE id=?""",
        (
            overall_score, overall_quality, verdict, summary, int(fatal_error), sentiment,
            predicted_disposition_raw, disposition_match,
            disposition["reason"], disposition["evidence"], disposition["start_time"], disposition["end_time"],
            disposition["callback_date"], disposition["callback_time"], call_id,
        ),
    )
    return True


def process_manifest(manifest_id):
    conn = get_conn()
    conn.execute("UPDATE manifests SET status='processing' WHERE id=?", (manifest_id,))
    conn.commit()

    pending = conn.execute(
        "SELECT * FROM calls WHERE manifest_id = ? AND status = 'pending'", (manifest_id,)
    ).fetchall()

    ok, failed = 0, 0
    for row in pending:
        success = process_call(conn, row)
        conn.commit()
        if success:
            ok += 1
        else:
            failed += 1

    status = "done_with_errors" if failed else "done"
    conn.execute(
        "UPDATE manifests SET status=?, processed_rows=?, failed_rows=? WHERE id=?",
        (status, ok, failed, manifest_id),
    )
    conn.commit()
    conn.close()
    return {"processed": ok, "failed": failed}

# ////////////////////////////////w5////////////////////////////////////
# import json
# import os
# import re
# from datetime import datetime
# from urllib.parse import urlparse

# import requests
# from openpyxl import load_workbook

# import config
# from db import get_conn, get_or_create_agent

# # Manifest column header -> internal field name. Matches the sample manifest
# # (agent_name, call_number, cti_call_number, call_start_time, call_service_name,
# #  call_lead_id, call_end_type_name, call_talk_duration, call_trunk_duration, Recordings).
# COLUMN_MAP = {
#     "agent_name": "agent_name",
#     "call_number": "call_number",
#     "cti_call_number": "cti_call_number",
#     "call_start_time": "call_start_time",
#     "call_service_name": "call_service_name",
#     "call_lead_id": "call_lead_id",
#     "call_end_type_name": "call_end_type_name",
#     "call_talk_duration": "call_talk_duration",
#     "call_trunk_duration": "call_trunk_duration",
#     "recordings": "recording",
# }

# DATE_FORMATS = [
#     "%m-%d-%Y %H:%M",
#     "%d-%m-%Y %H:%M",
#     "%Y-%m-%d %H:%M",
#     "%m/%d/%Y %H:%M",
#     "%d-%b-%y %H:%M",
# ]


# def parse_call_date(raw_value):
#     """Best-effort parse of call_start_time into a YYYY-MM-DD string for filtering."""
#     if raw_value is None:
#         return None
#     if isinstance(raw_value, datetime):
#         return raw_value.strftime("%Y-%m-%d")
#     text = str(raw_value).strip()
#     for fmt in DATE_FORMATS:
#         try:
#             return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
#         except ValueError:
#             continue
#     return None


# def extract_recording_base(cell_value, hyperlink_target=None):
#     """
#     Pulls the shared base filename out of a manifest 'Recordings' cell, e.g.
#       "7002_08320801862_17-Jun-26-18-11-32.wav16"  ->  "7002_08320801862_17-Jun-26-18-11-32"
#     Works whether the cell holds a bare filename, a local path, or a full URL/hyperlink.
#     """
#     source = hyperlink_target or cell_value
#     if not source:
#         return None
#     source = str(source).strip()
#     # take just the filename if it's a path or URL
#     parsed = urlparse(source)
#     filename = os.path.basename(parsed.path) if parsed.scheme else os.path.basename(source)
#     # strip extension
#     base = re.sub(r"\.wav16?$", "", filename, flags=re.IGNORECASE)
#     # strip any existing -IN / -OUT suffix so we always get the shared base
#     base = re.sub(r"-(IN|OUT)$", "", base, flags=re.IGNORECASE)
#     return base or None


# # def build_leg_paths(recording_base, agent_name, recording_base_url):
# #     base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
# #     folder = requests.utils.quote(agent_name or "")
# #     rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
# #     tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
# #     return rx_path, tx_path
# def build_leg_paths(recording_base, agent_name, recording_base_url):
#     # base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
#     # folder = requests.utils.quote(agent_name or "")
#     # # rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
#     # rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-IN.wav16"
#     # # tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
#     # tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-OUT.wav16"
#     path,ext=os.path.splitext(recording_base)
#     rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-IN{ext}"
#     tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-OUT{ext}"
#     return rx_path, tx_path


# def parse_manifest_file(file_path):
#     """Reads the uploaded xlsx and returns a list of row dicts keyed by COLUMN_MAP values."""
#     wb = load_workbook(file_path, data_only=True)
#     ws = wb.active

#     header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
#     header_index = {}
#     for i, h in enumerate(header_row):
#         if h is None:
#             continue
#         key = str(h).strip().lower().replace(" ", "_")
#         if key in COLUMN_MAP:
#             header_index[COLUMN_MAP[key]] = i

#     rows = []
#     for excel_row in ws.iter_rows(min_row=2):
#         if all(c.value is None for c in excel_row):
#             continue
#         record = {}
#         for field, idx in header_index.items():
#             cell = excel_row[idx]
#             if field == "recording":
#                 record["recording_raw"] = cell.value
#                 record["recording_hyperlink"] = cell.hyperlink.target if cell.hyperlink else None
#             else:
#                 record[field] = cell.value
#         rows.append(record)
#     return rows


# # def save_manifest(file_path, filename, recording_base_url):
# #     """Parses the manifest and inserts one 'pending' call row per manifest row."""
# #     rows = parse_manifest_file(file_path)
# #     conn = get_conn()
# #     cur = conn.execute(
# #         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
# #         "VALUES (?, ?, ?, ?, 'uploaded')",
# #         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
# #     )
# #     manifest_id = cur.lastrowid

# #     inserted = 0
# #     for r in rows:
# #         agent_name = (r.get("agent_name") or "Unassigned").strip()
# #         agent_id = get_or_create_agent(conn, agent_name)

# #         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
# #         rx_path, tx_path = (None, None)
# #         if recording_base:
# #             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)

# #         call_date = parse_call_date(r.get("call_start_time"))

# #         conn.execute(
# #             """INSERT INTO calls (
# #                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
# #                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
# #                 call_trunk_duration, recording_base, rx_path, tx_path, status
# #             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
# #             (
# #                 manifest_id, agent_id,
# #                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
# #                 str(r.get("call_start_time") or ""), call_date,
# #                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
# #                 r.get("call_end_type_name"), r.get("call_talk_duration"),
# #                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
# #             ),
# #         )
# #         inserted += 1

# #     conn.commit()
# #     conn.close()
# #     return manifest_id, inserted

# def save_manifest(file_path, filename, recording_base_url):
#     """Parses the manifest and inserts one 'pending' call row per manifest row."""
#     rows = parse_manifest_file(file_path)
#     print(rows)
#     conn = get_conn()
#     cur = conn.execute(
#         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
#         "VALUES (?, ?, ?, ?, 'uploaded')",
#         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
#     )
#     manifest_id = cur.lastrowid

#     inserted = 0
#     for r in rows:
#         agent_name = (r.get("agent_name") or "Unassigned").strip()
#         agent_id = get_or_create_agent(conn, agent_name)
#         # print
#         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
#         print("****"*12)
#         recording_base=r.get("recording_raw")
#         print(recording_base,r.get("recording_raw"))
#         rx_path, tx_path = (None, None)
#         if recording_base:
#             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)
#         # rx_path = recording_base
#         print("****"*12)
#         print(rx_path, tx_path) 
#         call_date = parse_call_date(r.get("call_start_time"))

#         conn.execute(
#             """INSERT INTO calls (
#                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
#                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
#                 call_trunk_duration, recording_base, rx_path, tx_path, status
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
#             (
#                 manifest_id, agent_id,
#                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
#                 str(r.get("call_start_time") or ""), call_date,
#                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
#                 r.get("call_end_type_name"), r.get("call_talk_duration"),
#                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
#             ),
#         )
#         inserted += 1

#     conn.commit()
#     conn.close()
#     return manifest_id, inserted


# def call_stt(rx_path, tx_path):
#     resp = requests.post(
#         config.STT_API_URL,
#         json={"rx_path": rx_path, "tx_path": tx_path},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()
#     return resp.json()


# # def call_slm(conversation_text, utterances):
# #     resp = requests.post(
# #         config.SLM_API_URL,
# #         json={"transcript": conversation_text},
# #         json={"conversation": conversation_text, "utterances": utterances},
# #         timeout=config.REQUEST_TIMEOUT_SECONDS,
# #     )
# #     resp.raise_for_status()
# #     return resp.json()

# def call_slm(conversation_text, utterances):
#     resp = requests.post(
#         config.SLM_API_URL,
#         json={"transcript": conversation_text},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()    
#     return resp.json()


# def normalize_score(param_key, raw_score):
#     max_points = config.PARAMETER_MAX_POINTS.get(param_key, config.DEFAULT_PARAMETER_MAX_POINTS)
#     if max_points <= 0:
#         return 0
#     # return round(min(raw_score, max_points) / max_points * 100, 1)
#     return raw_score



# def quality_from_score(score, fatal_error):
#     if fatal_error:
#         return "poor"
#     if score >= 80:
#         return "good"
#     if score >= 60:
#         return "average"
#     return "poor"


# def sentiment_from_verdict(verdict, fatal_error):
#     if fatal_error:
#         return "negative"
#     v = (verdict or "").lower()
#     if v in ("good", "excellent","average"):
#         return "positive" 
#     if v in ("poor", "bad", "fail"): 
#         return "negative" 
#     return "negative" 


# def normalize_disposition(value):
#     """'Not Interested', ' not-interested ', 'NOT_INTERESTED' -> 'NOT INTERESTED'
#     so actual (manifest) vs predicted (SLM) compare cleanly regardless of
#     whichever casing/punctuation style either side happens to use."""
#     if not value:
#         return None
#     text = str(value).strip().upper()
#     text = re.sub(r"[-_]+", " ", text)
#     text = re.sub(r"\s+", " ", text)
#     return text or None


# def extract_predicted_disposition(analysis):
#     """SLM's actual shape (confirmed): analysis["disposition"] is an object,
#     same pattern as a parameter entry -

#         "disposition": {
#           "type": "INTERESTED",
#           "reason": "...",
#           "evidence": "...",
#           "start_time": 52.51,
#           "end_time": 55.41,
#           "callback_date": "",
#           "callback_time": ""
#         }

#     Returns a plain dict with those 7 keys, defaulting anything missing to
#     None, so callers never need to guard against missing keys. Also accepts
#     a bare string (old/simplified SLM response, or a different key name) as
#     a fallback so this doesn't hard-break if the contract shifts again -
#     everything except "type" just comes back None in that case."""
#     value = analysis.get("disposition")
#     if value is None:
#         # fall back to older/alternate key names in case the contract moves again
#         for key in ("predicted_disposition", "call_disposition", "call_end_type"):
#             value = analysis.get(key)
#             if value:
#                 break

#     if isinstance(value, dict):
#         return {
#             "type": value.get("type"),
#             "reason": value.get("reason"),
#             "evidence": value.get("evidence"),
#             "start_time": value.get("start_time"),
#             "end_time": value.get("end_time"),
#             "callback_date": value.get("callback_date") or None,
#             "callback_time": value.get("callback_time") or None,
#         }
#     if value:
#         # plain string fallback - just the type, nothing else to show
#         return {"type": str(value), "reason": None, "evidence": None, "start_time": None,
#                  "end_time": None, "callback_date": None, "callback_time": None}
#     return {"type": None, "reason": None, "evidence": None, "start_time": None,
#              "end_time": None, "callback_date": None, "callback_time": None}


# def process_call(conn, call_row):
#     call_id = call_row["id"]
#     if not call_row["rx_path"] or not call_row["tx_path"]:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             ("Could not resolve recording IN/OUT paths from manifest.", call_id),
#         )
#         return False

#     # 1. Speech to text
#     conn.execute("UPDATE calls SET status='transcribing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         stt_result = call_stt(call_row["rx_path"], call_row["tx_path"])
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"STT request failed: {e}", call_id),
#         )
#         return False

#     conversation_text = stt_result.get("conversation", "")
#     utterances = stt_result.get("utterances", [])
#     conn.execute(
#         "INSERT INTO transcripts (call_id, conversation_text, utterances_json, raw_json) VALUES (?,?,?,?)",
#         (call_id, conversation_text, json.dumps(utterances), json.dumps(stt_result)),
#     )

#     # 2. SLM QA analysis
#     conn.execute("UPDATE calls SET status='analyzing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         slm_result = call_slm(conversation_text, utterances)
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"SLM analysis request failed: {e}", call_id),
#         )
#         return False

#     analysis = slm_result.get("analysis", slm_result)
#     param_data = analysis.get("parameters", {})
#     fatal_data = analysis.get("fatal_checks", {})
#     total = analysis.get("total") or param_data.get("total") or 0
#     summary = analysis.get("summary", "")
#     verdict = analysis.get("verdict", "")
#     fatal_error = bool(analysis.get("fatal_error", False))
#     disposition = extract_predicted_disposition(analysis)
#     predicted_disposition_raw = disposition["type"]

#     param_rows = conn.execute("SELECT id, param_key FROM parameters").fetchall()
#     param_id_by_key = {p["param_key"]: p["id"] for p in param_rows}

#     scores = []
#     for key, pid in param_id_by_key.items():
#         entry = param_data.get(key)
#         if not entry:
#             continue
#         raw_score = entry.get("score", 0) or 0
#         norm_score = normalize_score(key, raw_score)# keeping the score as it is so the ouput looks good 
#         # scores.append(norm_score)
#         scores.append(raw_score)

#         conn.execute(
#             """INSERT INTO call_scores
#                (call_id, parameter_id, status, reason, evidence, start_time, end_time, raw_score, score)
#                VALUES (?,?,?,?,?,?,?,?,?)""",
#             (
#                 call_id, pid, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#                 raw_score, norm_score,
#             ),
#         )

#     for key in config.FATAL_CHECKS:
#         entry = fatal_data.get(key, {})
#         conn.execute(
#             """INSERT INTO fatal_checks (call_id, check_key, status, reason, evidence, start_time, end_time)
#                VALUES (?,?,?,?,?,?,?)""",
#             (
#                 call_id, key, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#             ),
#         )

#     # overall_score = round(sum(scores) / len(scores), 1) if scores else (total or 0)
#     overall_score = sum(scores) if scores else (total or 0)# just sum up the scores 

#     overall_quality = quality_from_score(overall_score, fatal_error)
#     sentiment = sentiment_from_verdict(verdict, fatal_error)

#     # Correct/Wrong: compare the manifest's actual disposition (call_end_type_name,
#     # what really happened on the call) against the SLM's predicted one. Only
#     # set disposition_match when BOTH sides are actually present - a call
#     # missing either value isn't "wrong", it's "not comparable", and should
#     # stay out of both buckets rather than silently counting as a miss.
#     actual_disposition_norm = normalize_disposition(call_row["call_end_type_name"])
#     predicted_disposition_norm = normalize_disposition(predicted_disposition_raw)
#     if actual_disposition_norm and predicted_disposition_norm:
#         disposition_match = int(actual_disposition_norm == predicted_disposition_norm)
#     else:
#         disposition_match = None

#     conn.execute(
#         """UPDATE calls SET status='audited', overall_score=?, overall_quality=?, verdict=?,
#            summary=?, fatal_error=?, sentiment=?, predicted_disposition=?, disposition_match=?,
#            disposition_reason=?, disposition_evidence=?, disposition_start_time=?, disposition_end_time=?,
#            disposition_callback_date=?, disposition_callback_time=?,
#            audited_at = COALESCE(audited_at, NOW())
#            WHERE id=?""",
#         (
#             overall_score, overall_quality, verdict, summary, int(fatal_error), sentiment,
#             predicted_disposition_raw, disposition_match,
#             disposition["reason"], disposition["evidence"], disposition["start_time"], disposition["end_time"],
#             disposition["callback_date"], disposition["callback_time"], call_id,
#         ),
#     )
#     return True


# def process_manifest(manifest_id):
#     conn = get_conn()
#     conn.execute("UPDATE manifests SET status='processing' WHERE id=?", (manifest_id,))
#     conn.commit()

#     pending = conn.execute(
#         "SELECT * FROM calls WHERE manifest_id = ? AND status = 'pending'", (manifest_id,)
#     ).fetchall()

#     ok, failed = 0, 0
#     for row in pending:
#         success = process_call(conn, row)
#         conn.commit()
#         if success:
#             ok += 1
#         else:
#             failed += 1

#     status = "done_with_errors" if failed else "done"
#     conn.execute(
#         "UPDATE manifests SET status=?, processed_rows=?, failed_rows=? WHERE id=?",
#         (status, ok, failed, manifest_id),
#     )
#     conn.commit()
#     conn.close()
#     return {"processed": ok, "failed": failed}

# /////////////////w4/////////////////////////////////
# import json
# import os
# import re
# from datetime import datetime
# from urllib.parse import urlparse

# import requests
# from openpyxl import load_workbook

# import config
# from db import get_conn, get_or_create_agent

# # Manifest column header -> internal field name. Matches the sample manifest
# # (agent_name, call_number, cti_call_number, call_start_time, call_service_name,
# #  call_lead_id, call_end_type_name, call_talk_duration, call_trunk_duration, Recordings).
# COLUMN_MAP = {
#     "agent_name": "agent_name",
#     "call_number": "call_number",
#     "cti_call_number": "cti_call_number",
#     "call_start_time": "call_start_time",
#     "call_service_name": "call_service_name",
#     "call_lead_id": "call_lead_id",
#     "call_end_type_name": "call_end_type_name",
#     "call_talk_duration": "call_talk_duration",
#     "call_trunk_duration": "call_trunk_duration",
#     "recordings": "recording",
# }

# DATE_FORMATS = [
#     "%m-%d-%Y %H:%M",
#     "%d-%m-%Y %H:%M",
#     "%Y-%m-%d %H:%M",
#     "%m/%d/%Y %H:%M",
#     "%d-%b-%y %H:%M",
# ]


# def parse_call_date(raw_value):
#     """Best-effort parse of call_start_time into a YYYY-MM-DD string for filtering."""
#     if raw_value is None:
#         return None
#     if isinstance(raw_value, datetime):
#         return raw_value.strftime("%Y-%m-%d")
#     text = str(raw_value).strip()
#     for fmt in DATE_FORMATS:
#         try:
#             return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
#         except ValueError:
#             continue
#     return None


# def extract_recording_base(cell_value, hyperlink_target=None):
#     """
#     Pulls the shared base filename out of a manifest 'Recordings' cell, e.g.
#       "7002_08320801862_17-Jun-26-18-11-32.wav16"  ->  "7002_08320801862_17-Jun-26-18-11-32"
#     Works whether the cell holds a bare filename, a local path, or a full URL/hyperlink.
#     """
#     source = hyperlink_target or cell_value
#     if not source:
#         return None
#     source = str(source).strip()
#     # take just the filename if it's a path or URL
#     parsed = urlparse(source)
#     filename = os.path.basename(parsed.path) if parsed.scheme else os.path.basename(source)
#     # strip extension
#     base = re.sub(r"\.wav16?$", "", filename, flags=re.IGNORECASE)
#     # strip any existing -IN / -OUT suffix so we always get the shared base
#     base = re.sub(r"-(IN|OUT)$", "", base, flags=re.IGNORECASE)
#     return base or None


# # def build_leg_paths(recording_base, agent_name, recording_base_url):
# #     base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
# #     folder = requests.utils.quote(agent_name or "")
# #     rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
# #     tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
# #     return rx_path, tx_path
# def build_leg_paths(recording_base, agent_name, recording_base_url):
#     # base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
#     # folder = requests.utils.quote(agent_name or "")
#     # # rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
#     # rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-IN.wav16"
#     # # tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
#     # tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-OUT.wav16"
#     path,ext=os.path.splitext(recording_base)
#     rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-IN{ext}"
#     tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-OUT{ext}"
#     return rx_path, tx_path


# def parse_manifest_file(file_path):
#     """Reads the uploaded xlsx and returns a list of row dicts keyed by COLUMN_MAP values."""
#     wb = load_workbook(file_path, data_only=True)
#     ws = wb.active

#     header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
#     header_index = {}
#     for i, h in enumerate(header_row):
#         if h is None:
#             continue
#         key = str(h).strip().lower().replace(" ", "_")
#         if key in COLUMN_MAP:
#             header_index[COLUMN_MAP[key]] = i

#     rows = []
#     for excel_row in ws.iter_rows(min_row=2):
#         if all(c.value is None for c in excel_row):
#             continue
#         record = {}
#         for field, idx in header_index.items():
#             cell = excel_row[idx]
#             if field == "recording":
#                 record["recording_raw"] = cell.value
#                 record["recording_hyperlink"] = cell.hyperlink.target if cell.hyperlink else None
#             else:
#                 record[field] = cell.value
#         rows.append(record)
#     return rows


# # def save_manifest(file_path, filename, recording_base_url):
# #     """Parses the manifest and inserts one 'pending' call row per manifest row."""
# #     rows = parse_manifest_file(file_path)
# #     conn = get_conn()
# #     cur = conn.execute(
# #         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
# #         "VALUES (?, ?, ?, ?, 'uploaded')",
# #         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
# #     )
# #     manifest_id = cur.lastrowid

# #     inserted = 0
# #     for r in rows:
# #         agent_name = (r.get("agent_name") or "Unassigned").strip()
# #         agent_id = get_or_create_agent(conn, agent_name)

# #         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
# #         rx_path, tx_path = (None, None)
# #         if recording_base:
# #             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)

# #         call_date = parse_call_date(r.get("call_start_time"))

# #         conn.execute(
# #             """INSERT INTO calls (
# #                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
# #                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
# #                 call_trunk_duration, recording_base, rx_path, tx_path, status
# #             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
# #             (
# #                 manifest_id, agent_id,
# #                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
# #                 str(r.get("call_start_time") or ""), call_date,
# #                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
# #                 r.get("call_end_type_name"), r.get("call_talk_duration"),
# #                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
# #             ),
# #         )
# #         inserted += 1

# #     conn.commit()
# #     conn.close()
# #     return manifest_id, inserted

# def save_manifest(file_path, filename, recording_base_url):
#     """Parses the manifest and inserts one 'pending' call row per manifest row."""
#     rows = parse_manifest_file(file_path)
#     print(rows)
#     conn = get_conn()
#     cur = conn.execute(
#         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
#         "VALUES (?, ?, ?, ?, 'uploaded')",
#         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
#     )
#     manifest_id = cur.lastrowid

#     inserted = 0
#     for r in rows:
#         agent_name = (r.get("agent_name") or "Unassigned").strip()
#         agent_id = get_or_create_agent(conn, agent_name)
#         # print
#         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
#         print("****"*12)
#         recording_base=r.get("recording_raw")
#         print(recording_base,r.get("recording_raw"))
#         rx_path, tx_path = (None, None)
#         if recording_base:
#             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)
#         # rx_path = recording_base
#         print("****"*12)
#         print(rx_path, tx_path) 
#         call_date = parse_call_date(r.get("call_start_time"))

#         conn.execute(
#             """INSERT INTO calls (
#                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
#                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
#                 call_trunk_duration, recording_base, rx_path, tx_path, status
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
#             (
#                 manifest_id, agent_id,
#                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
#                 str(r.get("call_start_time") or ""), call_date,
#                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
#                 r.get("call_end_type_name"), r.get("call_talk_duration"),
#                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
#             ),
#         )
#         inserted += 1

#     conn.commit()
#     conn.close()
#     return manifest_id, inserted


# def call_stt(rx_path, tx_path):
#     resp = requests.post(
#         config.STT_API_URL,
#         json={"rx_path": rx_path, "tx_path": tx_path},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()
#     return resp.json()


# # def call_slm(conversation_text, utterances):
# #     resp = requests.post(
# #         config.SLM_API_URL,
# #         json={"transcript": conversation_text},
# #         json={"conversation": conversation_text, "utterances": utterances},
# #         timeout=config.REQUEST_TIMEOUT_SECONDS,
# #     )
# #     resp.raise_for_status()
# #     return resp.json()

# def call_slm(conversation_text, utterances):
#     resp = requests.post(
#         config.SLM_API_URL,
#         json={"transcript": conversation_text},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()    
#     return resp.json()


# def normalize_score(param_key, raw_score):
#     max_points = config.PARAMETER_MAX_POINTS.get(param_key, config.DEFAULT_PARAMETER_MAX_POINTS)
#     if max_points <= 0:
#         return 0
#     # return round(min(raw_score, max_points) / max_points * 100, 1)
#     return raw_score



# def quality_from_score(score, fatal_error):
#     if fatal_error:
#         return "poor"
#     if score >= 80:
#         return "good"
#     if score >= 60:
#         return "average"
#     return "poor"


# def sentiment_from_verdict(verdict, fatal_error):
#     if fatal_error:
#         return "negative"
#     v = (verdict or "").lower()
#     if v in ("good", "excellent"):
#         return "positive" 
#     if v in ("poor", "bad", "fail"): 
#         return "negative" 
#     return "neutral" 


# def normalize_disposition(value):
#     """'Not Interested', ' not-interested ', 'NOT_INTERESTED' -> 'NOT INTERESTED'
#     so actual (manifest) vs predicted (SLM) compare cleanly regardless of
#     whichever casing/punctuation style either side happens to use."""
#     if not value:
#         return None
#     text = str(value).strip().upper()
#     text = re.sub(r"[-_]+", " ", text)
#     text = re.sub(r"\s+", " ", text)
#     return text or None


# def extract_predicted_disposition(analysis):
#     """SLM's actual shape (confirmed): analysis["disposition"] is an object,
#     same pattern as a parameter entry -

#         "disposition": {
#           "type": "INTERESTED",
#           "reason": "...",
#           "evidence": "...",
#           "start_time": 52.51,
#           "end_time": 55.41,
#           "callback_date": "",
#           "callback_time": ""
#         }

#     Returns a plain dict with those 7 keys, defaulting anything missing to
#     None, so callers never need to guard against missing keys. Also accepts
#     a bare string (old/simplified SLM response, or a different key name) as
#     a fallback so this doesn't hard-break if the contract shifts again -
#     everything except "type" just comes back None in that case."""
#     value = analysis.get("disposition")
#     if value is None:
#         # fall back to older/alternate key names in case the contract moves again
#         for key in ("predicted_disposition", "call_disposition", "call_end_type"):
#             value = analysis.get(key)
#             if value:
#                 break

#     if isinstance(value, dict):
#         return {
#             "type": value.get("type"),
#             "reason": value.get("reason"),
#             "evidence": value.get("evidence"),
#             "start_time": value.get("start_time"),
#             "end_time": value.get("end_time"),
#             "callback_date": value.get("callback_date") or None,
#             "callback_time": value.get("callback_time") or None,
#         }
#     if value:
#         # plain string fallback - just the type, nothing else to show
#         return {"type": str(value), "reason": None, "evidence": None, "start_time": None,
#                  "end_time": None, "callback_date": None, "callback_time": None}
#     return {"type": None, "reason": None, "evidence": None, "start_time": None,
#              "end_time": None, "callback_date": None, "callback_time": None}


# def process_call(conn, call_row):
#     call_id = call_row["id"]
#     if not call_row["rx_path"] or not call_row["tx_path"]:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             ("Could not resolve recording IN/OUT paths from manifest.", call_id),
#         )
#         return False

#     # 1. Speech to text
#     conn.execute("UPDATE calls SET status='transcribing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         stt_result = call_stt(call_row["rx_path"], call_row["tx_path"])
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"STT request failed: {e}", call_id),
#         )
#         return False

#     conversation_text = stt_result.get("conversation", "")
#     utterances = stt_result.get("utterances", [])
#     conn.execute(
#         "INSERT INTO transcripts (call_id, conversation_text, utterances_json, raw_json) VALUES (?,?,?,?)",
#         (call_id, conversation_text, json.dumps(utterances), json.dumps(stt_result)),
#     )

#     # 2. SLM QA analysis
#     conn.execute("UPDATE calls SET status='analyzing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         slm_result = call_slm(conversation_text, utterances)
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"SLM analysis request failed: {e}", call_id),
#         )
#         return False

#     analysis = slm_result.get("analysis", slm_result)
#     param_data = analysis.get("parameters", {})
#     fatal_data = analysis.get("fatal_checks", {})
#     total = analysis.get("total") or param_data.get("total") or 0
#     summary = analysis.get("summary", "")
#     verdict = analysis.get("verdict", "")
#     fatal_error = bool(analysis.get("fatal_error", False))
#     disposition = extract_predicted_disposition(analysis)
#     predicted_disposition_raw = disposition["type"]

#     param_rows = conn.execute("SELECT id, param_key FROM parameters").fetchall()
#     param_id_by_key = {p["param_key"]: p["id"] for p in param_rows}

#     scores = []
#     for key, pid in param_id_by_key.items():
#         entry = param_data.get(key)
#         if not entry:
#             continue
#         raw_score = entry.get("score", 0) or 0
#         norm_score = normalize_score(key, raw_score)# keeping the score as it is so the ouput looks good 
#         # scores.append(norm_score)
#         scores.append(raw_score)

#         conn.execute(
#             """INSERT INTO call_scores
#                (call_id, parameter_id, status, reason, evidence, start_time, end_time, raw_score, score)
#                VALUES (?,?,?,?,?,?,?,?,?)""",
#             (
#                 call_id, pid, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#                 raw_score, norm_score,
#             ),
#         )

#     for key in config.FATAL_CHECKS:
#         entry = fatal_data.get(key, {})
#         conn.execute(
#             """INSERT INTO fatal_checks (call_id, check_key, status, reason, evidence, start_time, end_time)
#                VALUES (?,?,?,?,?,?,?)""",
#             (
#                 call_id, key, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#             ),
#         )

#     # overall_score = round(sum(scores) / len(scores), 1) if scores else (total or 0)
#     overall_score = sum(scores) if scores else (total or 0)# just sum up the scores 

#     overall_quality = quality_from_score(overall_score, fatal_error)
#     sentiment = sentiment_from_verdict(verdict, fatal_error)

#     # Correct/Wrong: compare the manifest's actual disposition (call_end_type_name,
#     # what really happened on the call) against the SLM's predicted one. Only
#     # set disposition_match when BOTH sides are actually present - a call
#     # missing either value isn't "wrong", it's "not comparable", and should
#     # stay out of both buckets rather than silently counting as a miss.
#     actual_disposition_norm = normalize_disposition(call_row["call_end_type_name"])
#     predicted_disposition_norm = normalize_disposition(predicted_disposition_raw)
#     if actual_disposition_norm and predicted_disposition_norm:
#         disposition_match = int(actual_disposition_norm == predicted_disposition_norm)
#     else:
#         disposition_match = None

#     conn.execute(
#         """UPDATE calls SET status='audited', overall_score=?, overall_quality=?, verdict=?,
#            summary=?, fatal_error=?, sentiment=?, predicted_disposition=?, disposition_match=?,
#            disposition_reason=?, disposition_evidence=?, disposition_start_time=?, disposition_end_time=?,
#            disposition_callback_date=?, disposition_callback_time=?
#            WHERE id=?""",
#         (
#             overall_score, overall_quality, verdict, summary, int(fatal_error), sentiment,
#             predicted_disposition_raw, disposition_match,
#             disposition["reason"], disposition["evidence"], disposition["start_time"], disposition["end_time"],
#             disposition["callback_date"], disposition["callback_time"], call_id,
#         ),
#     )
#     return True


# def process_manifest(manifest_id):
#     conn = get_conn()
#     conn.execute("UPDATE manifests SET status='processing' WHERE id=?", (manifest_id,))
#     conn.commit()

#     pending = conn.execute(
#         "SELECT * FROM calls WHERE manifest_id = ? AND status = 'pending'", (manifest_id,)
#     ).fetchall()

#     ok, failed = 0, 0
#     for row in pending:
#         success = process_call(conn, row)
#         conn.commit()
#         if success:
#             ok += 1
#         else:
#             failed += 1

#     status = "done_with_errors" if failed else "done"
#     conn.execute(
#         "UPDATE manifests SET status=?, processed_rows=?, failed_rows=? WHERE id=?",
#         (status, ok, failed, manifest_id),
#     )
#     conn.commit()
#     conn.close()
#     return {"processed": ok, "failed": failed}

# *********************************w2*********************************************
# import json
# import os
# import re
# from datetime import datetime
# from urllib.parse import urlparse

# import requests
# from openpyxl import load_workbook

# import config
# from db import get_conn, get_or_create_agent

# # Manifest column header -> internal field name. Matches the sample manifest
# # (agent_name, call_number, cti_call_number, call_start_time, call_service_name,
# #  call_lead_id, call_end_type_name, call_talk_duration, call_trunk_duration, Recordings).
# COLUMN_MAP = {
#     "agent_name": "agent_name",
#     "call_number": "call_number",
#     "cti_call_number": "cti_call_number",
#     "call_start_time": "call_start_time",
#     "call_service_name": "call_service_name",
#     "call_lead_id": "call_lead_id",
#     "call_end_type_name": "call_end_type_name",
#     "call_talk_duration": "call_talk_duration",
#     "call_trunk_duration": "call_trunk_duration",
#     "recordings": "recording",
# }

# DATE_FORMATS = [
#     "%m-%d-%Y %H:%M",
#     "%d-%m-%Y %H:%M",
#     "%Y-%m-%d %H:%M",
#     "%m/%d/%Y %H:%M",
#     "%d-%b-%y %H:%M",
# ]


# def parse_call_date(raw_value):
#     """Best-effort parse of call_start_time into a YYYY-MM-DD string for filtering."""
#     if raw_value is None:
#         return None
#     if isinstance(raw_value, datetime):
#         return raw_value.strftime("%Y-%m-%d")
#     text = str(raw_value).strip()
#     for fmt in DATE_FORMATS:
#         try:
#             return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
#         except ValueError:
#             continue
#     return None


# def extract_recording_base(cell_value, hyperlink_target=None):
#     """
#     Pulls the shared base filename out of a manifest 'Recordings' cell, e.g.
#       "7002_08320801862_17-Jun-26-18-11-32.wav16"  ->  "7002_08320801862_17-Jun-26-18-11-32"
#     Works whether the cell holds a bare filename, a local path, or a full URL/hyperlink.
#     """
#     source = hyperlink_target or cell_value
#     if not source:
#         return None
#     source = str(source).strip()
#     # take just the filename if it's a path or URL
#     parsed = urlparse(source)
#     filename = os.path.basename(parsed.path) if parsed.scheme else os.path.basename(source)
#     # strip extension
#     base = re.sub(r"\.wav16?$", "", filename, flags=re.IGNORECASE)
#     # strip any existing -IN / -OUT suffix so we always get the shared base
#     base = re.sub(r"-(IN|OUT)$", "", base, flags=re.IGNORECASE)
#     return base or None


# # def build_leg_paths(recording_base, agent_name, recording_base_url):
# #     base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
# #     folder = requests.utils.quote(agent_name or "")
# #     rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
# #     tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
# #     return rx_path, tx_path
# def build_leg_paths(recording_base, agent_name, recording_base_url):
#     # base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
#     # folder = requests.utils.quote(agent_name or "")
#     # # rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
#     # rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-IN.wav16"
#     # # tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
#     # tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-OUT.wav16"
#     path,ext=os.path.splitext(recording_base)
#     rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-IN{ext}"
#     tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-OUT{ext}"
#     return rx_path, tx_path


# def parse_manifest_file(file_path):
#     """Reads the uploaded xlsx and returns a list of row dicts keyed by COLUMN_MAP values."""
#     wb = load_workbook(file_path, data_only=True)
#     ws = wb.active

#     header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
#     header_index = {}
#     for i, h in enumerate(header_row):
#         if h is None:
#             continue
#         key = str(h).strip().lower().replace(" ", "_")
#         if key in COLUMN_MAP:
#             header_index[COLUMN_MAP[key]] = i

#     rows = []
#     for excel_row in ws.iter_rows(min_row=2):
#         if all(c.value is None for c in excel_row):
#             continue
#         record = {}
#         for field, idx in header_index.items():
#             cell = excel_row[idx]
#             if field == "recording":
#                 record["recording_raw"] = cell.value
#                 record["recording_hyperlink"] = cell.hyperlink.target if cell.hyperlink else None
#             else:
#                 record[field] = cell.value
#         rows.append(record)
#     return rows


# # def save_manifest(file_path, filename, recording_base_url):
# #     """Parses the manifest and inserts one 'pending' call row per manifest row."""
# #     rows = parse_manifest_file(file_path)
# #     conn = get_conn()
# #     cur = conn.execute(
# #         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
# #         "VALUES (?, ?, ?, ?, 'uploaded')",
# #         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
# #     )
# #     manifest_id = cur.lastrowid

# #     inserted = 0
# #     for r in rows:
# #         agent_name = (r.get("agent_name") or "Unassigned").strip()
# #         agent_id = get_or_create_agent(conn, agent_name)

# #         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
# #         rx_path, tx_path = (None, None)
# #         if recording_base:
# #             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)

# #         call_date = parse_call_date(r.get("call_start_time"))

# #         conn.execute(
# #             """INSERT INTO calls (
# #                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
# #                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
# #                 call_trunk_duration, recording_base, rx_path, tx_path, status
# #             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
# #             (
# #                 manifest_id, agent_id,
# #                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
# #                 str(r.get("call_start_time") or ""), call_date,
# #                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
# #                 r.get("call_end_type_name"), r.get("call_talk_duration"),
# #                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
# #             ),
# #         )
# #         inserted += 1

# #     conn.commit()
# #     conn.close()
# #     return manifest_id, inserted

# def save_manifest(file_path, filename, recording_base_url):
#     """Parses the manifest and inserts one 'pending' call row per manifest row."""
#     rows = parse_manifest_file(file_path)
#     print(rows)
#     conn = get_conn()
#     cur = conn.execute(
#         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
#         "VALUES (?, ?, ?, ?, 'uploaded')",
#         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
#     )
#     manifest_id = cur.lastrowid

#     inserted = 0
#     for r in rows:
#         agent_name = (r.get("agent_name") or "Unassigned").strip()
#         agent_id = get_or_create_agent(conn, agent_name)
#         # print
#         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
#         print("****"*12)
#         recording_base=r.get("recording_raw")
#         print(recording_base,r.get("recording_raw"))
#         rx_path, tx_path = (None, None)
#         if recording_base:
#             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)
#         # rx_path = recording_base
#         print("****"*12)
#         print(rx_path, tx_path) 
#         call_date = parse_call_date(r.get("call_start_time"))

#         conn.execute(
#             """INSERT INTO calls (
#                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
#                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
#                 call_trunk_duration, recording_base, rx_path, tx_path, status
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
#             (
#                 manifest_id, agent_id,
#                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
#                 str(r.get("call_start_time") or ""), call_date,
#                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
#                 r.get("call_end_type_name"), r.get("call_talk_duration"),
#                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
#             ),
#         )
#         inserted += 1

#     conn.commit()
#     conn.close()
#     return manifest_id, inserted


# def call_stt(rx_path, tx_path):
#     resp = requests.post(
#         config.STT_API_URL,
#         json={"rx_path": rx_path, "tx_path": tx_path},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()
#     return resp.json()


# # def call_slm(conversation_text, utterances):
# #     resp = requests.post(
# #         config.SLM_API_URL,
# #         json={"transcript": conversation_text},
# #         json={"conversation": conversation_text, "utterances": utterances},
# #         timeout=config.REQUEST_TIMEOUT_SECONDS,
# #     )
# #     resp.raise_for_status()
# #     return resp.json()

# def call_slm(conversation_text, utterances):
#     resp = requests.post(
#         config.SLM_API_URL,
#         json={"transcript": conversation_text},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()    
#     return resp.json()


# def normalize_score(param_key, raw_score):
#     max_points = config.PARAMETER_MAX_POINTS.get(param_key, config.DEFAULT_PARAMETER_MAX_POINTS)
#     if max_points <= 0:
#         return 0
#     # return round(min(raw_score, max_points) / max_points * 100, 1)
#     return raw_score



# def quality_from_score(score, fatal_error):
#     if fatal_error:
#         return "poor"
#     if score >= 80:
#         return "good"
#     if score >= 60:
#         return "average"
#     return "poor"


# def sentiment_from_verdict(verdict, fatal_error):
#     if fatal_error:
#         return "negative"
#     v = (verdict or "").lower()
#     if v in ("good", "excellent"):
#         return "positive" 
#     if v in ("poor", "bad", "fail"): 
#         return "negative" 
#     return "neutral" 


# def normalize_disposition(value):
#     """'Not Interested', ' not-interested ', 'NOT_INTERESTED' -> 'NOT INTERESTED'
#     so actual (manifest) vs predicted (SLM) compare cleanly regardless of
#     whichever casing/punctuation style either side happens to use."""
#     if not value:
#         return None
#     text = str(value).strip().upper()
#     text = re.sub(r"[-_]+", " ", text)
#     text = re.sub(r"\s+", " ", text)
#     return text or None


# # def extract_predicted_disposition(analysis):
# #     """The SLM's disposition field name isn't nailed down yet, so check the
# #     handful of names it's reasonably likely to use, in priority order, and
# #     take the first one present. Update this list (or better, pin one key)
# #     once the SLM's actual contract is confirmed - this is deliberately
# #     defensive rather than assuming a single exact key."""
# #     for key in ("disposition", "predicted_disposition", "call_disposition", "call_end_type"):
# #         value = analysis.get(key)
# #         if value:
# #             return str(value)
# #     return None
# def extract_predicted_disposition(analysis):
#     for key in (
#         "disposition",
#         "predicted_disposition",
#         "call_disposition",
#         "call_end_type",
#     ):
#         value = analysis.get(key)

#         if not value:
#             continue

#         if isinstance(value, dict):
#             return value

#         if isinstance(value, str):
#             return {
#                 "type": value
#             }

#     return None

# def process_call(conn, call_row):
#     call_id = call_row["id"]
#     if not call_row["rx_path"] or not call_row["tx_path"]:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             ("Could not resolve recording IN/OUT paths from manifest.", call_id),
#         )
#         return False

#     # 1. Speech to text
#     conn.execute("UPDATE calls SET status='transcribing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         stt_result = call_stt(call_row["rx_path"], call_row["tx_path"])
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"STT request failed: {e}", call_id),
#         )
#         return False

#     conversation_text = stt_result.get("conversation", "")
#     utterances = stt_result.get("utterances", [])
#     conn.execute(
#         "INSERT INTO transcripts (call_id, conversation_text, utterances_json, raw_json) VALUES (?,?,?,?)",
#         (call_id, conversation_text, json.dumps(utterances), json.dumps(stt_result)),
#     )

#     # 2. SLM QA analysis
#     conn.execute("UPDATE calls SET status='analyzing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         slm_result = call_slm(conversation_text, utterances)
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"SLM analysis request failed: {e}", call_id),
#         )
#         return False

#     analysis = slm_result.get("analysis", slm_result)
#     param_data = analysis.get("parameters", {})
#     fatal_data = analysis.get("fatal_checks", {})
#     total = analysis.get("total") or param_data.get("total") or 0
#     summary = analysis.get("summary", "")
#     verdict = analysis.get("verdict", "")
#     fatal_error = bool(analysis.get("fatal_error", False))
#     predicted_disposition_raw = extract_predicted_disposition(analysis)

#     if predicted_disposition_raw:
#         predicted_disposition = predicted_disposition_raw.get("type")
#         # callback_date = predicted_disposition_raw.get("callback_date")
#         callback_date = predicted_disposition_raw.get("callback_date")
#         callback_time = predicted_disposition_raw.get("callback_time")

#     param_rows = conn.execute("SELECT id, param_key FROM parameters").fetchall()
#     param_id_by_key = {p["param_key"]: p["id"] for p in param_rows}

#     scores = []
#     for key, pid in param_id_by_key.items():
#         entry = param_data.get(key)
#         if not entry:
#             continue
#         raw_score = entry.get("score", 0) or 0
#         norm_score = normalize_score(key, raw_score)# keeping the score as it is so the ouput looks good 
#         # scores.append(norm_score)
#         scores.append(raw_score)

#         conn.execute(
#             """INSERT INTO call_scores
#                (call_id, parameter_id, status, reason, evidence, start_time, end_time, raw_score, score)
#                VALUES (?,?,?,?,?,?,?,?,?)""",
#             (
#                 call_id, pid, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#                 raw_score, norm_score,
#             ),
#         )

#     for key in config.FATAL_CHECKS:
#         entry = fatal_data.get(key, {})
#         conn.execute(
#             """INSERT INTO fatal_checks (call_id, check_key, status, reason, evidence, start_time, end_time)
#                VALUES (?,?,?,?,?,?,?)""",
#             (
#                 call_id, key, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#             ),
#         )

#     # overall_score = round(sum(scores) / len(scores), 1) if scores else (total or 0)
#     overall_score = sum(scores) if scores else (total or 0)# just sum up the scores 

#     overall_quality = quality_from_score(overall_score, fatal_error)
#     sentiment = sentiment_from_verdict(verdict, fatal_error)

#     # Correct/Wrong: compare the manifest's actual disposition (call_end_type_name,
#     # what really happened on the call) against the SLM's predicted one. Only
#     # set disposition_match when BOTH sides are actually present - a call
#     # missing either value isn't "wrong", it's "not comparable", and should
#     # stay out of both buckets rather than silently counting as a miss.
#     actual_disposition_norm = normalize_disposition(call_row["call_end_type_name"])
#     predicted_disposition_norm = normalize_disposition(predicted_disposition_raw)
#     if actual_disposition_norm and predicted_disposition_norm:
#         disposition_match = int(actual_disposition_norm == predicted_disposition_norm)
#     else:
#         disposition_match = None

#     conn.execute(
#         """UPDATE calls SET status='audited', overall_score=?, overall_quality=?, verdict=?,
#            summary=?, fatal_error=?, sentiment=?, predicted_disposition=?, disposition_match=?
#            WHERE id=?""",
#         (
#             overall_score, overall_quality, verdict, summary, int(fatal_error), sentiment,
#             predicted_disposition_raw, disposition_match, call_id,
#         ),
#     )
#     return True


# def process_manifest(manifest_id):
#     conn = get_conn()
#     conn.execute("UPDATE manifests SET status='processing' WHERE id=?", (manifest_id,))
#     conn.commit()

#     pending = conn.execute(
#         "SELECT * FROM calls WHERE manifest_id = ? AND status = 'pending'", (manifest_id,)
#     ).fetchall()

#     ok, failed = 0, 0
#     for row in pending:
#         success = process_call(conn, row)
#         conn.commit()
#         if success:
#             ok += 1
#         else:
#             failed += 1

#     status = "done_with_errors" if failed else "done"
#     conn.execute(
#         "UPDATE manifests SET status=?, processed_rows=?, failed_rows=? WHERE id=?",
#         (status, ok, failed, manifest_id),
#     )
#     conn.commit()
#     conn.close()
#     return {"processed": ok, "failed": failed}

# **************************w1*****************************
# import json
# import os
# import re
# from datetime import datetime
# from urllib.parse import urlparse

# import requests
# from openpyxl import load_workbook

# import config
# from db import get_conn, get_or_create_agent

# # Manifest column header -> internal field name. Matches the sample manifest
# # (agent_name, call_number, cti_call_number, call_start_time, call_service_name,
# #  call_lead_id, call_end_type_name, call_talk_duration, call_trunk_duration, Recordings).
# COLUMN_MAP = {
#     "agent_name": "agent_name",
#     "call_number": "call_number",
#     "cti_call_number": "cti_call_number",
#     "call_start_time": "call_start_time",
#     "call_service_name": "call_service_name",
#     "call_lead_id": "call_lead_id",
#     "call_end_type_name": "call_end_type_name",
#     "call_talk_duration": "call_talk_duration",
#     "call_trunk_duration": "call_trunk_duration",
#     "recordings": "recording",
# }

# DATE_FORMATS = [
#     "%m-%d-%Y %H:%M",
#     "%d-%m-%Y %H:%M",
#     "%Y-%m-%d %H:%M",
#     "%m/%d/%Y %H:%M",
#     "%d-%b-%y %H:%M",
# ]


# def parse_call_date(raw_value):
#     """Best-effort parse of call_start_time into a YYYY-MM-DD string for filtering."""
#     if raw_value is None:
#         return None
#     if isinstance(raw_value, datetime):
#         return raw_value.strftime("%Y-%m-%d")
#     text = str(raw_value).strip()
#     for fmt in DATE_FORMATS:
#         try:
#             return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
#         except ValueError:
#             continue
#     return None


# def extract_recording_base(cell_value, hyperlink_target=None):
#     """
#     Pulls the shared base filename out of a manifest 'Recordings' cell, e.g.
#       "7002_08320801862_17-Jun-26-18-11-32.wav16"  ->  "7002_08320801862_17-Jun-26-18-11-32"
#     Works whether the cell holds a bare filename, a local path, or a full URL/hyperlink.
#     """
#     source = hyperlink_target or cell_value
#     if not source:
#         return None
#     source = str(source).strip()
#     # take just the filename if it's a path or URL
#     parsed = urlparse(source)
#     filename = os.path.basename(parsed.path) if parsed.scheme else os.path.basename(source)
#     # strip extension
#     base = re.sub(r"\.wav16?$", "", filename, flags=re.IGNORECASE)
#     # strip any existing -IN / -OUT suffix so we always get the shared base
#     base = re.sub(r"-(IN|OUT)$", "", base, flags=re.IGNORECASE)
#     return base or None


# # def build_leg_paths(recording_base, agent_name, recording_base_url):
# #     base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
# #     folder = requests.utils.quote(agent_name or "")
# #     rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
# #     tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
# #     return rx_path, tx_path
# def build_leg_paths(recording_base, agent_name, recording_base_url):
#     # base_url = (recording_base_url or config.RECORDINGS_BASE_URL).rstrip("/")
#     # folder = requests.utils.quote(agent_name or "")
#     # # rx_path = f"{base_url}/{folder}/{recording_base}-IN.wav16"
#     # rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-IN.wav16"
#     # # tx_path = f"{base_url}/{folder}/{recording_base}-OUT.wav16"
#     # tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{recording_base}-OUT.wav16"
#     path,ext=os.path.splitext(recording_base)
#     rx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-IN{ext}"
#     tx_path = f"{recording_base_url or config.RECORDINGS_BASE_URL}/{path}-OUT{ext}"
#     return rx_path, tx_path


# def parse_manifest_file(file_path):
#     """Reads the uploaded xlsx and returns a list of row dicts keyed by COLUMN_MAP values."""
#     wb = load_workbook(file_path, data_only=True)
#     ws = wb.active

#     header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
#     header_index = {}
#     for i, h in enumerate(header_row):
#         if h is None:
#             continue
#         key = str(h).strip().lower().replace(" ", "_")
#         if key in COLUMN_MAP:
#             header_index[COLUMN_MAP[key]] = i

#     rows = []
#     for excel_row in ws.iter_rows(min_row=2):
#         if all(c.value is None for c in excel_row):
#             continue
#         record = {}
#         for field, idx in header_index.items():
#             cell = excel_row[idx]
#             if field == "recording":
#                 record["recording_raw"] = cell.value
#                 record["recording_hyperlink"] = cell.hyperlink.target if cell.hyperlink else None
#             else:
#                 record[field] = cell.value
#         rows.append(record)
#     return rows


# # def save_manifest(file_path, filename, recording_base_url):
# #     """Parses the manifest and inserts one 'pending' call row per manifest row."""
# #     rows = parse_manifest_file(file_path)
# #     conn = get_conn()
# #     cur = conn.execute(
# #         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
# #         "VALUES (?, ?, ?, ?, 'uploaded')",
# #         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
# #     )
# #     manifest_id = cur.lastrowid

# #     inserted = 0
# #     for r in rows:
# #         agent_name = (r.get("agent_name") or "Unassigned").strip()
# #         agent_id = get_or_create_agent(conn, agent_name)

# #         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
# #         rx_path, tx_path = (None, None)
# #         if recording_base:
# #             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)

# #         call_date = parse_call_date(r.get("call_start_time"))

# #         conn.execute(
# #             """INSERT INTO calls (
# #                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
# #                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
# #                 call_trunk_duration, recording_base, rx_path, tx_path, status
# #             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
# #             (
# #                 manifest_id, agent_id,
# #                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
# #                 str(r.get("call_start_time") or ""), call_date,
# #                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
# #                 r.get("call_end_type_name"), r.get("call_talk_duration"),
# #                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
# #             ),
# #         )
# #         inserted += 1

# #     conn.commit()
# #     conn.close()
# #     return manifest_id, inserted

# def save_manifest(file_path, filename, recording_base_url):
#     """Parses the manifest and inserts one 'pending' call row per manifest row."""
#     rows = parse_manifest_file(file_path)
#     print(rows)
#     conn = get_conn()
#     cur = conn.execute(
#         "INSERT INTO manifests (filename, recording_base_url, uploaded_at, total_rows, status) "
#         "VALUES (?, ?, ?, ?, 'uploaded')",
#         (filename, recording_base_url, datetime.utcnow().isoformat(), len(rows)),
#     )
#     manifest_id = cur.lastrowid

#     inserted = 0
#     for r in rows:
#         agent_name = (r.get("agent_name") or "Unassigned").strip()
#         agent_id = get_or_create_agent(conn, agent_name)
#         # print
#         recording_base = extract_recording_base(r.get("recording_raw"), r.get("recording_hyperlink"))
#         print("****"*12)
#         recording_base=r.get("recording_raw")
#         print(recording_base,r.get("recording_raw"))
#         rx_path, tx_path = (None, None)
#         if recording_base:
#             rx_path, tx_path = build_leg_paths(recording_base, agent_name, recording_base_url)
#         # rx_path = recording_base
#         print("****"*12)
#         print(rx_path, tx_path) 
#         call_date = parse_call_date(r.get("call_start_time"))

#         conn.execute(
#             """INSERT INTO calls (
#                 manifest_id, agent_id, call_number, cti_call_number, call_start_time, call_date,
#                 call_service_name, call_lead_id, call_end_type_name, call_talk_duration,
#                 call_trunk_duration, recording_base, rx_path, tx_path, status
#             ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
#             (
#                 manifest_id, agent_id,
#                 str(r.get("call_number") or ""), str(r.get("cti_call_number") or ""),
#                 str(r.get("call_start_time") or ""), call_date,
#                 r.get("call_service_name"), str(r.get("call_lead_id") or ""),
#                 r.get("call_end_type_name"), r.get("call_talk_duration"),
#                 r.get("call_trunk_duration"), recording_base, rx_path, tx_path,
#             ),
#         )
#         inserted += 1

#     conn.commit()
#     conn.close()
#     return manifest_id, inserted


# def call_stt(rx_path, tx_path):
#     resp = requests.post(
#         config.STT_API_URL,
#         json={"rx_path": rx_path, "tx_path": tx_path},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()
#     return resp.json()


# # def call_slm(conversation_text, utterances):
# #     resp = requests.post(
# #         config.SLM_API_URL,
# #         json={"transcript": conversation_text},
# #         json={"conversation": conversation_text, "utterances": utterances},
# #         timeout=config.REQUEST_TIMEOUT_SECONDS,
# #     )
# #     resp.raise_for_status()
# #     return resp.json()

# def call_slm(conversation_text, utterances):
#     resp = requests.post(
#         config.SLM_API_URL,
#         json={"transcript": conversation_text},
#         timeout=config.REQUEST_TIMEOUT_SECONDS,
#     )
#     resp.raise_for_status()    
#     return resp.json()


# def normalize_score(param_key, raw_score):
#     max_points = config.PARAMETER_MAX_POINTS.get(param_key, config.DEFAULT_PARAMETER_MAX_POINTS)
#     if max_points <= 0:
#         return 0
#     # return round(min(raw_score, max_points) / max_points * 100, 1)
#     return raw_score



# def quality_from_score(score, fatal_error):
#     if fatal_error:
#         return "poor"
#     if score >= 80:
#         return "good"
#     if score >= 60:
#         return "average"
#     return "poor"


# def sentiment_from_verdict(verdict, fatal_error):
#     if fatal_error:
#         return "negative"
#     v = (verdict or "").lower()
#     if v in ("good", "excellent"):
#         return "positive" 
#     if v in ("poor", "bad", "fail"): 
#         return "negative" 
#     return "negative" 


# def process_call(conn, call_row):
#     call_id = call_row["id"]
#     if not call_row["rx_path"] or not call_row["tx_path"]:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             ("Could not resolve recording IN/OUT paths from manifest.", call_id),
#         )
#         return False

#     # 1. Speech to text
#     conn.execute("UPDATE calls SET status='transcribing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         stt_result = call_stt(call_row["rx_path"], call_row["tx_path"])
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"STT request failed: {e}", call_id),
#         )
#         return False

#     conversation_text = stt_result.get("conversation", "")
#     utterances = stt_result.get("utterances", [])
#     conn.execute(
#         "INSERT INTO transcripts (call_id, conversation_text, utterances_json, raw_json) VALUES (?,?,?,?)",
#         (call_id, conversation_text, json.dumps(utterances), json.dumps(stt_result)),
#     )

#     # 2. SLM QA analysis
#     conn.execute("UPDATE calls SET status='analyzing' WHERE id=?", (call_id,))
#     conn.commit()
#     try:
#         slm_result = call_slm(conversation_text, utterances)
#     except Exception as e:
#         conn.execute(
#             "UPDATE calls SET status='failed', error_message=? WHERE id=?",
#             (f"SLM analysis request failed: {e}", call_id),
#         )
#         return False

#     analysis = slm_result.get("analysis", slm_result)
#     param_data = analysis.get("parameters", {})
#     fatal_data = analysis.get("fatal_checks", {})
#     total = analysis.get("total") or param_data.get("total") or 0
#     summary = analysis.get("summary", "")
#     verdict = analysis.get("verdict", "")
#     fatal_error = bool(analysis.get("fatal_error", False))

#     param_rows = conn.execute("SELECT id, param_key FROM parameters").fetchall()
#     param_id_by_key = {p["param_key"]: p["id"] for p in param_rows}

#     scores = []
#     for key, pid in param_id_by_key.items():
#         entry = param_data.get(key)
#         if not entry:
#             continue
#         raw_score = entry.get("score", 0) or 0
#         norm_score = normalize_score(key, raw_score)# keeping the score as it is so the ouput looks good 
#         # scores.append(norm_score)
#         scores.append(raw_score)

#         conn.execute(
#             """INSERT INTO call_scores
#                (call_id, parameter_id, status, reason, evidence, start_time, end_time, raw_score, score)
#                VALUES (?,?,?,?,?,?,?,?,?)""",
#             (
#                 call_id, pid, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#                 raw_score, norm_score,
#             ),
#         )

#     for key in config.FATAL_CHECKS:
#         entry = fatal_data.get(key, {})
#         conn.execute(
#             """INSERT INTO fatal_checks (call_id, check_key, status, reason, evidence, start_time, end_time)
#                VALUES (?,?,?,?,?,?,?)""",
#             (
#                 call_id, key, int(bool(entry.get("status"))), entry.get("reason", ""),
#                 entry.get("evidence", ""), entry.get("start_time"), entry.get("end_time"),
#             ),
#         )

#     # overall_score = round(sum(scores) / len(scores), 1) if scores else (total or 0)
#     overall_score = sum(scores) if scores else (total or 0)# just sum up the scores 

#     overall_quality = quality_from_score(overall_score, fatal_error)
#     sentiment = sentiment_from_verdict(verdict, fatal_error)

#     conn.execute(
#         """UPDATE calls SET status='audited', overall_score=?, overall_quality=?, verdict=?,
#            summary=?, fatal_error=?, sentiment=? WHERE id=?""",
#         (overall_score, overall_quality, verdict, summary, int(fatal_error), sentiment, call_id),
#     )
#     return True


# def process_manifest(manifest_id):
#     conn = get_conn()
#     conn.execute("UPDATE manifests SET status='processing' WHERE id=?", (manifest_id,))
#     conn.commit()

#     pending = conn.execute(
#         "SELECT * FROM calls WHERE manifest_id = ? AND status = 'pending'", (manifest_id,)
#     ).fetchall()

#     ok, failed = 0, 0
#     for row in pending:
#         success = process_call(conn, row)
#         conn.commit()
#         if success:
#             ok += 1
#         else:
#             failed += 1

#     status = "done_with_errors" if failed else "done"
#     conn.execute(
#         "UPDATE manifests SET status=?, processed_rows=?, failed_rows=? WHERE id=?",
#         (status, ok, failed, manifest_id),
#     )
#     conn.commit()
#     conn.close()
#     return {"processed": ok, "failed": failed}
