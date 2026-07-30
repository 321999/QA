import os

# MySQL connection settings - override via environment variables.
MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "admin")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "qa_dashboard3")

# Speech-to-text service: takes rx_path (IN leg) + tx_path (OUT leg) recording URLs,
# returns a diarized transcript (utterances + full conversation text).
STT_API_URL = os.environ.get("STT_API_URL", "http://192.168.11.253:9000/transcribe")
RECORDINGS_PLAYBACK_BASE_URL = os.environ.get(
    "RECORDINGS_PLAYBACK_BASE_URL", "http://192.168.11.253:9000/recordings"
)
# http://192.168.11.253:9000/recordings/7002_06397853103_17-Jun-26-19-02-49.wav16
# SLM QA analysis service: takes the transcript/conversation, returns the 16-point
# checklist scoring + fatal checks + summary + verdict.
SLM_API_URL = os.environ.get("SLM_API_URL", "http://192.168.11.253:8000/qa")

# If a manifest's "Recordings" cell isn't already a full http URL, it gets built as:
#   {RECORDINGS_BASE_URL}/{agent_name}/{recording_base}-IN.wav16
#   {RECORDINGS_BASE_URL}/{agent_name}/{recording_base}-OUT.wav16
# Adjust this (or pass recording_base_url per-upload) to match how your recordings
# server actually lays out folders.


RECORDINGS_BASE_URL = os.environ.get("RECORDINGS_BASE_URL", "http://192.168.10.189/qa_upload")
RECORDINGS_BASE_URL = os.environ.get("RECORDINGS_BASE_URL", r"C:/Users/kishor/Desktop/offline/ybl27rec")
RECORDINGS_BASE_URL = os.environ.get("RECORDINGS_BASE_URL", r"/home/kishore/ybl27rec")



REQUEST_TIMEOUT_SECONDS = int(os.environ.get("PIPELINE_REQUEST_TIMEOUT", "120"))

# Canonical 16-point checklist: SLM response key -> display label shown on the dashboard.
PARAMETERS = [
    ("greeted_professionally", "Greeted professionally"),
    ("clearly_introduced_self_and_bank", "Clearly introduces self"),
    ("verified_customer_identity_rpc", "Verified customer identity"),
    ("presented_product_benefits_convincingly", "Presented product benefits"),
    ("used_appropriate_probing", "Used appropriate probing"),
    ("attempted_cross_sell_upsell_nba", "Attempted cross sell and probing"),
    ("listened_actively", "Listened actively"),
    ("showed_empathy", "Showed empathy"),
    ("maintained_polite_professional_tone", "Maintained politeness and professional tone"),
    ("avoided_interrupting_talking_over", "Avoided interrupting/talking over"),
    ("appropriate_rate_of_speech", "Appropriate rate of speech"),
    ("summarized_key_points_next_steps", "Summarised key points"),
    ("thanked_customer_for_time", "Thanked customer for time"),
    ("ended_call_courteous_positive_note", "Ended call on courteous/positive note"),
    ("stated_purpose_of_call_clearly", "Stated purpose of the call"),
    ("addressed_objections_confusions_effectively", "Addressed objections/confusions effectively"),
]

# Raw SLM scores use mixed max-points per parameter (e.g. 4, 2, 10 - see sample response).
# This normalizes each score to 0-100 so the dashboard bars/averages are comparable.
PARAMETER_MAX_POINTS = {
    "greeted_professionally": 4,
    "clearly_introduced_self_and_bank": 2,
    "verified_customer_identity_rpc": 2,
    "presented_product_benefits_convincingly": 10,
    "used_appropriate_probing": 10, # kuch poochne ke baad bhi agent ne cross sell upsell nahi kiya, to is parameter ko 0 score milega
    "attempted_cross_sell_upsell_nba": 10, 
    "showed_empathy": 5,
    "listened_actively": 5,
    "maintained_polite_professional_tone": 10,
    "avoided_interrupting_talking_over": 10,
    "appropriate_rate_of_speech": 10,
    "summarized_key_points_next_steps": 5,
    "thanked_customer_for_time": 2,
    "ended_call_courteous_positive_note": 3,
    "stated_purpose_of_call_clearly": 2,
    "addressed_objections_confusions_effectively": 10,
}
DEFAULT_PARAMETER_MAX_POINTS = 10

FATAL_CHECKS = [
    "asked_otp",
    "asked_cvv",
    "asked_pin",
    "asked_password",
    "asked_full_card_number",
]
