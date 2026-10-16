# ============================================================
# Interview Routes
# File: routes/interviews.py
# Create interviews, save Q&A, store feedback, view history
# ============================================================

import httpx
import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import logging
import json

from models.database import get_db, Interview, InterviewQuestion, Feedback, User, to_utc_iso
from models.schemas import InterviewCreate, QuestionAnswer, InterviewResponse, FeedbackCreate, FailInterviewRequest
from routes.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

# Extra buffer (beyond the interview's own duration_limit) before we treat
# a still-"in_progress" interview as abandoned rather than genuinely active.
STALE_GRACE_SECONDS = 15 * 60  # 15 minutes


# ============================================================
# Auto-heal stale "in_progress" interviews
# Runs on every read of dashboard/history/list so a candidate never
# needs a cron job to clean this up. Any interview stuck in_progress
# well past its own time limit gets marked "failed" with a real
# reason instead of hanging forever — and stops eating the free quota.
# ============================================================
def _reconcile_stale_interviews(db: Session, user_id: int):
    now = datetime.utcnow()
    stuck = db.query(Interview).filter(
        Interview.user_id == user_id,
        Interview.status == "in_progress"
    ).all()

    changed = False
    for interview in stuck:
        limit = (interview.duration_limit or 300) + STALE_GRACE_SECONDS
        if not interview.started_at or (now - interview.started_at).total_seconds() <= limit:
            continue  # still genuinely within its active window

        has_answers = db.query(InterviewQuestion).filter(
            InterviewQuestion.interview_id == interview.id
        ).first() is not None

        interview.status = "failed"
        interview.completed_at = now
        interview.failure_reason = (
            "Interview was interrupted mid-session and never finished — likely a network drop, "
            "the browser/tab being closed, or a server error before the report could be generated."
            if has_answers else
            "Interview session never really started — likely a network issue, or the browser/tab "
            "was closed right after the session was created."
        )
        changed = True

    if changed:
        db.commit()


# ============================================================
# POST /api/interviews — Start a new interview session
# ============================================================
@router.post("/interviews", response_model=InterviewResponse)
async def create_interview(
    payload: InterviewCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Resolve any abandoned in_progress sessions first so the quota count
    # below is accurate (same reconciliation the dashboard already relies on).
    _reconcile_stale_interviews(db, current_user.id)

    now = datetime.utcnow()
    has_active_subscription = bool(
        current_user.subscription_active_until and current_user.subscription_active_until > now
    )

    if not has_active_subscription:
        # "failed" sessions don't count against the quota — same rule the
        # dashboard's interviews_remaining_free uses, so the two stay in sync.
        used = db.query(Interview).filter(
            Interview.user_id == current_user.id,
            Interview.status != "failed"
        ).count()

        if used >= 3:
            raise HTTPException(
                status_code=402,
                detail={
                    "error_code": "free_quota_exceeded",
                    "message": "You've used all 3 free interviews. Please purchase a plan to continue.",
                }
            )
    else:
        # Paid plans are unlimited over the life of the subscription, but
        # capped at 6/day — both as a fair-use practice limit and so a paid
        # account can't be used to spin up unbounded interviews (each one
        # allows up to MAX_CHAT_CALLS_PER_INTERVIEW /api/chat calls).
        #
        # "Day" here means IST calendar day (midnight to midnight India
        # time), not UTC — matches how Indian users actually think about
        # "today". started_at is stored in UTC, so we compute IST midnight
        # and convert it back to UTC to use as the query boundary.
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_now = now + IST_OFFSET
        ist_midnight = datetime(ist_now.year, ist_now.month, ist_now.day)
        today_start_utc = ist_midnight - IST_OFFSET

        used_today = db.query(Interview).filter(
            Interview.user_id == current_user.id,
            Interview.status != "failed",
            Interview.started_at >= today_start_utc
        ).count()

        if used_today >= 6:
            raise HTTPException(
                status_code=402,
                detail={
                    "error_code": "daily_limit_exceeded",
                    "message": "You've reached today's limit of 6 interviews. Please try again after midnight IST.",
                }
            )

    interview = Interview(
        user_id=current_user.id,
        role=payload.role,
        difficulty=payload.difficulty,
        duration_limit=payload.duration_limit,
        status="in_progress",
        sector=payload.sector,
        government_domain=payload.government_domain,
        government_role=payload.government_role,
        biodata=payload.biodata,
        biodata_source=payload.biodata_source,
        candidate_summary=payload.candidate_summary,
        target_company=payload.target_company,
        interview_round=payload.interview_round or "mixed",
    )
    db.add(interview)
    db.commit()
    db.refresh(interview)

    logger.info(f"Interview started: user={current_user.email}, role={payload.role}")
    return InterviewResponse.model_validate(interview)


# ============================================================
# POST /api/interviews/{id}/questions — Save one Q&A pair
# ============================================================
@router.post("/interviews/{interview_id}/questions")
async def add_question_answer(
    interview_id: int,
    payload: QuestionAnswer,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    qa = InterviewQuestion(
        interview_id=interview_id,
        question_text=payload.question_text,
        answer_text=payload.answer_text,
        order_index=payload.order_index
    )
    db.add(qa)
    db.commit()
    db.refresh(qa)

    return {"status": "saved", "question_id": qa.id}


# ============================================================
# POST /api/interviews/{id}/feedback — Save final report
# ============================================================
@router.post("/interviews/{interview_id}/feedback")
async def save_feedback(
    interview_id: int,
    payload: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    existing = db.query(Feedback).filter(Feedback.interview_id == interview_id).first()
    if existing:
        db.delete(existing)
        db.commit()

    feedback = Feedback(
        interview_id=interview_id,
        overall_score=payload.overall_score,
        hiring_recommendation=payload.hiring_recommendation,
        summary=payload.summary,
        technical_score=payload.technical_score,
        soft_skills_score=payload.soft_skills_score,
        eye_contact_score=payload.eye_contact_score,
        confidence_score=payload.confidence_score,
        engagement_score=payload.engagement_score,
        strengths=json.dumps(payload.strengths or []),
        areas_to_improve=json.dumps(payload.areas_to_improve or []),
        next_steps=payload.next_steps,
        personal_note=payload.personal_note
    )
    db.add(feedback)

    interview.status = "completed"
    interview.overall_score = payload.overall_score
    interview.completed_at = datetime.utcnow()
    db.commit()

    logger.info(f"Interview {interview_id} completed, score={payload.overall_score}")
    return {"status": "saved", "interview_id": interview_id}


# ============================================================
# GET /api/interviews — List history for current user
# ============================================================
@router.get("/interviews", response_model=list[InterviewResponse])
async def list_interviews(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    _reconcile_stale_interviews(db, current_user.id)

    interviews = db.query(Interview).filter(
        Interview.user_id == current_user.id
    ).order_by(Interview.started_at.desc()).all()

    return [InterviewResponse.model_validate(i) for i in interviews]


# ============================================================
# GET /api/interviews/{id} — Full detail (Q&A + feedback)
# ============================================================
@router.get("/interviews/{interview_id}")
async def get_interview_detail(
    interview_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    questions = db.query(InterviewQuestion).filter(
        InterviewQuestion.interview_id == interview_id
    ).order_by(InterviewQuestion.order_index).all()

    feedback = db.query(Feedback).filter(Feedback.interview_id == interview_id).first()

    feedback_data = None
    if feedback:
        feedback_data = {
            "overall_score": feedback.overall_score,
            "hiring_recommendation": feedback.hiring_recommendation,
            "summary": feedback.summary,
            "technical_score": feedback.technical_score,
            "soft_skills_score": feedback.soft_skills_score,
            "eye_contact_score": feedback.eye_contact_score,
            "confidence_score": feedback.confidence_score,
            "engagement_score": feedback.engagement_score,
            "strengths": json.loads(feedback.strengths or "[]"),
            "areas_to_improve": json.loads(feedback.areas_to_improve or "[]"),
            "next_steps": feedback.next_steps,
            "personal_note": feedback.personal_note
        }

    return {
        "id": interview.id,
        "role": interview.role,
        "difficulty": interview.difficulty,
        "status": interview.status,
        "overall_score": interview.overall_score,
        "started_at": to_utc_iso(interview.started_at),
        "completed_at": to_utc_iso(interview.completed_at),
        "sector": interview.sector,
        "government_domain": interview.government_domain,
        "government_role": interview.government_role,
        "biodata_source": interview.biodata_source,
        "candidate_summary": interview.candidate_summary,
        "failure_reason": interview.failure_reason,
        "questions": [
            {"question_text": q.question_text, "answer_text": q.answer_text, "order_index": q.order_index}
            for q in questions
        ],
        "feedback": feedback_data
    }


# ============================================================
# GET /api/dashboard — User stats for dashboard
# ============================================================
@router.get("/dashboard")
async def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    _reconcile_stale_interviews(db, current_user.id)

    interviews = db.query(Interview).filter(
        Interview.user_id == current_user.id
    ).order_by(Interview.started_at.desc()).all()

    # "failed" sessions were wasted through no fault of the candidate's —
    # they don't count as a used attempt, so they're excluded from both
    # the total and the free-trial quota.
    countable = [i for i in interviews if i.status != "failed"]
    total = len(countable)
    completed = [i for i in interviews if i.status == 'completed']
    scores = [i.overall_score for i in completed if i.overall_score is not None]

    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    best_score = round(max(scores), 1) if scores else 0

    recent = []
    for i in interviews[:5]:
        recent.append({
            "id": i.id,
            "role": i.role,
            "status": i.status,
            "overall_score": i.overall_score,
            "started_at": to_utc_iso(i.started_at),
            "completed_at": to_utc_iso(i.completed_at),
            "sector": i.sector,
            "government_domain": i.government_domain,
            "government_role": i.government_role,
            "target_company": i.target_company,
            "interview_round": i.interview_round,
            "failure_reason": i.failure_reason
        })

    now = datetime.utcnow()
    has_active_subscription = bool(
        current_user.subscription_active_until and current_user.subscription_active_until > now
    )

    interviews_remaining_today = None
    if has_active_subscription:
        # Same IST-midnight window used by the actual limit check in
        # create_interview — kept in sync so the dashboard's pre-check
        # never says "you can start one" when the real endpoint would
        # then reject it.
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_now = now + IST_OFFSET
        ist_midnight = datetime(ist_now.year, ist_now.month, ist_now.day)
        today_start_utc = ist_midnight - IST_OFFSET
        used_today = sum(
            1 for i in countable
            if i.started_at and i.started_at >= today_start_utc
        )
        interviews_remaining_today = max(0, 6 - used_today)

    return {
        "user": {
            "name": current_user.name,
            "email": current_user.email,
            "profile_picture": current_user.profile_picture,
            "subscription_plan": current_user.subscription_plan,
            "subscription_active_until": to_utc_iso(current_user.subscription_active_until)
        },
        "stats": {
            "total_interviews": total,
            "completed_interviews": len(completed),
            "average_score": avg_score,
            "best_score": best_score,
            "interviews_remaining_free": max(0, 3 - total),
            "interviews_remaining_today": interviews_remaining_today
        },
        "recent_interviews": recent
    }


# ============================================================
# GET /api/history — Full interview history with feedback
# ============================================================
@router.get("/history")
async def get_interview_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    _reconcile_stale_interviews(db, current_user.id)

    # completed + cheating_terminated + failed दिखाएं (failed भी दिखाना है ताकि
    # user को पता रहे reason क्या था) — बस genuinely-active in_progress छुपाएं
    interviews = db.query(Interview).filter(
        Interview.user_id == current_user.id,
        Interview.status.in_(["completed", "cheating_terminated", "failed"])
    ).order_by(Interview.started_at.desc()).all()

    result = []
    for i in interviews:
        questions = db.query(InterviewQuestion).filter(
            InterviewQuestion.interview_id == i.id
        ).order_by(InterviewQuestion.order_index).all()

        feedback = db.query(Feedback).filter(
            Feedback.interview_id == i.id
        ).first()

        feedback_data = None
        if feedback:
            feedback_data = {
                "overall_score": feedback.overall_score,
                "hiring_recommendation": feedback.hiring_recommendation,
                "summary": feedback.summary,
                "technical_score": feedback.technical_score,
                "soft_skills_score": feedback.soft_skills_score,
                "strengths": json.loads(feedback.strengths or "[]"),
                "areas_to_improve": json.loads(feedback.areas_to_improve or "[]"),
                "next_steps": feedback.next_steps,
                "personal_note": feedback.personal_note
            }

        result.append({
            "id": i.id,
            "role": i.role,
            "status": i.status,
            "overall_score": i.overall_score,
            "started_at": to_utc_iso(i.started_at),
            "completed_at": to_utc_iso(i.completed_at),
            "sector": i.sector,
            "government_domain": i.government_domain,
            "government_role": i.government_role,
            "candidate_summary": i.candidate_summary,
            "target_company": i.target_company,
            "interview_round": i.interview_round,
            "failure_reason": i.failure_reason,
            "questions": [
                {
                    "question_text": q.question_text,
                    "answer_text": q.answer_text,
                    "order_index": q.order_index
                } for q in questions
            ],
            "feedback": feedback_data
        })

    return result


# ============================================================
# POST /api/interviews/{id}/fail — Frontend reports a failure reason
# (network drop, API error, mic/cam issue, tab closed mid-session)
# so the interview doesn't linger as "in_progress" forever, and
# doesn't count against the free-trial quota.
# ============================================================
@router.post("/interviews/{interview_id}/fail")
async def fail_interview(
    interview_id: int,
    payload: FailInterviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    # Already reached a final state (e.g. feedback saved right before the
    # tab closed) — don't overwrite a real result with a late failure report.
    if interview.status in ("completed", "cheating_terminated", "failed"):
        return {"status": interview.status, "interview_id": interview_id}

    interview.status = "failed"
    interview.completed_at = datetime.utcnow()
    interview.failure_reason = (payload.reason or "Interview could not be completed.").strip()[:500]
    db.commit()

    logger.info(f"Interview {interview_id} marked failed: {interview.failure_reason}")
    return {"status": "failed", "interview_id": interview_id}


# ============================================================
# POST /api/interviews/{id}/terminate — Cheating terminated
# ============================================================
@router.post("/interviews/{interview_id}/terminate")
async def terminate_interview(
    interview_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    interview.status = "cheating_terminated"
    interview.completed_at = datetime.utcnow()
    db.commit()

    return {"status": "terminated", "interview_id": interview_id}


# ============================================================
# DELETE /api/interviews/cleanup — पुराने incomplete interviews हटाएं
# ============================================================
@router.delete("/interviews/cleanup")
async def cleanup_old_interviews(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=7)

    # 7 दिन से पुराने in_progress interviews delete करें
    old = db.query(Interview).filter(
        Interview.user_id == current_user.id,
        Interview.status == "in_progress",
        Interview.started_at < cutoff
    ).all()

    count = len(old)
    for interview in old:
        db.delete(interview)
    db.commit()

    return {"deleted": count, "message": f"Cleaned up {count} old incomplete interviews"}


# ============================================================
# POST /api/chat — Groq API proxy, with Gemini as a free fallback
# Both keys stay server-side; frontend contract (data.choices[0]
# .message.content) is unchanged no matter which provider answers.
#
# Speed + "don't hammer the API" design:
#   1. One shared, keep-alive HTTP client (no TLS handshake per turn).
#   2. Live interview turns use low reasoning effort on Groq — the
#      question is 1-3 sentences, deep chain-of-thought just adds latency.
#   3. If a provider answers 429, it's put on a short cooldown and
#      traffic goes straight to the other one instead of retrying it.
#   4. Per-user burst limiter on top of the per-interview call cap.
#   5. Calls that fail on every provider are refunded from the
#      per-interview cap so a provider outage can't lock a candidate out.
# ============================================================
import time
import asyncio
from models.rate_limiter import hit as _rate_hit

# Statuses worth failing over on: 429 (rate/quota), 5xx (provider down),
# 404 (model retired/renamed by the provider). 400/401 are real request/auth
# problems — another provider won't fix a bad prompt or key.
_FAILOVER_STATUSES = {404, 429, 500, 502, 503, 504}

_http_client: "httpx.AsyncClient | None" = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _http_client


# provider name -> unix time until which we skip it (set after a 429)
_provider_cooldown_until: dict = {"groq": 0.0, "gemini": 0.0}
_MAX_COOLDOWN_SECONDS = 60


def _start_cooldown(provider: str, response: httpx.Response):
    try:
        wait = float(response.headers.get("retry-after", 20))
    except (TypeError, ValueError):
        wait = 20.0
    wait = max(5.0, min(wait, _MAX_COOLDOWN_SECONDS))
    _provider_cooldown_until[provider] = time.time() + wait
    logger.warning(f"{provider} rate-limited — skipping it for {wait:.0f}s")


def _in_cooldown(provider: str) -> bool:
    return time.time() < _provider_cooldown_until.get(provider, 0.0)


async def _call_groq(messages: list, temperature: float, max_tokens: int, task: str = "interview") -> dict:
    """Raises RuntimeError on any failover-worthy failure."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY not configured")

    body = {
        "model": "openai/gpt-oss-120b",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Live questions are 1-3 sentences: low reasoning = much faster first
        # token. The one-shot report keeps more reasoning for accuracy.
        "reasoning_effort": "medium" if task == "feedback" else "low",
    }
    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    url = "https://api.groq.com/openai/v1/chat/completions"
    timeout = 40.0 if task == "feedback" else 20.0

    try:
        client = _get_client()
        response = await client.post(url, headers=headers, json=body, timeout=timeout)
        if response.status_code == 400 and "reasoning_effort" in response.text:
            # Model/param combo not accepted — retry once without it.
            body.pop("reasoning_effort", None)
            response = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise RuntimeError(f"groq_failover:network:{type(e).__name__}")

    if response.status_code != 200:
        logger.warning(f"Groq call failed: {response.status_code} {response.text[:300]}")
        if response.status_code == 429:
            _start_cooldown("groq", response)
        if response.status_code in _FAILOVER_STATUSES:
            raise RuntimeError(f"groq_failover:{response.status_code}")
        raise HTTPException(status_code=response.status_code, detail="Groq API error")

    result = response.json()
    try:
        content = (result["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        content = ""
    if not content:
        # Usually the token budget was eaten by reasoning — try the other provider.
        raise RuntimeError("groq_failover:empty_content")
    result["_provider"] = "groq"
    return result


def _messages_to_gemini(messages: list):
    """OpenAI-style [{role, content}, ...] -> Gemini's (system_instruction, contents)."""
    system_parts = []
    contents = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        if role == "system":
            system_parts.append(text)
            continue
        g_role = "model" if role == "assistant" else "user"
        # Gemini rejects consecutive same-role turns in some cases — merge them.
        if contents and contents[-1]["role"] == g_role:
            contents[-1]["parts"][0]["text"] += "\n\n" + text
        else:
            contents.append({"role": g_role, "parts": [{"text": text}]})
    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "Begin."}]})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Begin."}]})
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


async def _call_gemini(messages: list, temperature: float, max_tokens: int, task: str = "interview") -> dict:
    """Reshapes Gemini's reply into the OpenAI-style shape the frontend expects."""
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    system_instruction, contents = _messages_to_gemini(messages)
    # Check current availability at ai.google.dev if this model gets retired.
    model = "gemini-3.1-flash-lite"

    generation_config = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if task == "feedback":
        # Guarantees syntactically valid JSON for the report — far fewer
        # "formatting issue" fallbacks on the results screen.
        generation_config["responseMimeType"] = "application/json"

    body = {"contents": contents, "generationConfig": generation_config}
    if system_instruction:
        body["system_instruction"] = {"parts": [{"text": system_instruction}]}

    try:
        response = await _get_client().post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": gemini_key, "Content-Type": "application/json"},
            json=body,
            timeout=40.0 if task == "feedback" else 20.0,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"gemini_failover:network:{type(e).__name__}")

    if response.status_code != 200:
        logger.warning(f"Gemini call failed: {response.status_code} {response.text[:300]}")
        if response.status_code == 429:
            _start_cooldown("gemini", response)
        if response.status_code in _FAILOVER_STATUSES:
            raise RuntimeError(f"gemini_failover:{response.status_code}")
        raise HTTPException(status_code=response.status_code, detail="Gemini API error")

    data = response.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        logger.error(f"Unexpected Gemini response shape: {json.dumps(data)[:300]}")
        raise RuntimeError("gemini_failover:bad_response_shape")
    if not text:
        raise RuntimeError("gemini_failover:empty_content")

    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "_provider": "gemini",
    }


# ============================================================
# /api/chat SAFEGUARDS
# Every call must belong to a real, in-progress interview of the caller,
# and is capped so one interview can't run up an unbounded API bill.
# ============================================================
MAX_CHAT_CALLS_PER_INTERVIEW = 80
SERVER_MAX_TOKENS = 1200
MAX_MESSAGES_IN_PAYLOAD = 100
MAX_CONTEXT_MESSAGES_INTERVIEW = 60   # non-system turns sent to the model for a live question
MAX_CONTENT_CHARS = 6000              # per message — a resume/system prompt fits, a pasted novel doesn't
USER_BURST_LIMIT = 10                 # /api/chat calls ...
USER_BURST_WINDOW_SECONDS = 60        # ... per user per minute (normal use is ~1-3/min)
MIN_SECONDS_BETWEEN_CALLS = 1.5       # same interview can't be spammed faster than this
_last_call_at: dict = {}              # interview_id -> unix time of last accepted call


def _prepare_messages(messages: list, task: str) -> list:
    """Validates shape, caps size. System messages are always kept (they carry
    the persona + the pacing note); older non-system turns are dropped first."""
    cleaned = []
    for m in messages:
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail="Invalid message in payload")
        role = m.get("role")
        content = m.get("content")
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Invalid message in payload")
        cleaned.append({"role": role, "content": content[:MAX_CONTENT_CHARS]})

    if task == "feedback":
        return cleaned  # the report needs the whole conversation

    system_msgs = [m for m in cleaned if m["role"] == "system"]
    convo = [m for m in cleaned if m["role"] != "system"]
    if len(convo) > MAX_CONTEXT_MESSAGES_INTERVIEW:
        convo = convo[-MAX_CONTEXT_MESSAGES_INTERVIEW:]
    # keep persona/system first, then the conversation, pacing note (a system
    # msg) stays last since it was appended last in the original order
    base = [m for m in system_msgs[:1]]
    tail_system = system_msgs[1:]
    return base + convo + tail_system


@router.post("/chat")
async def chat_with_groq(
    request: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    interview_id = request.get("interview_id")
    if not interview_id:
        raise HTTPException(status_code=400, detail="interview_id is required")

    interview = db.query(Interview).filter(
        Interview.id == interview_id,
        Interview.user_id == current_user.id
    ).first()

    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    if interview.status != "in_progress":
        raise HTTPException(status_code=400, detail="This interview is no longer active")

    # Per-user burst limit (protects the API keys from loops / abuse)
    limited, retry_after = _rate_hit(f"chat:{current_user.id}", USER_BURST_LIMIT, USER_BURST_WINDOW_SECONDS)
    if limited:
        raise HTTPException(
            status_code=429,
            detail="You're going a bit fast — please wait a few seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    # Same-interview spam guard (double submits, runaway client loops)
    now_ts = time.time()
    last = _last_call_at.get(interview_id, 0.0)
    if now_ts - last < MIN_SECONDS_BETWEEN_CALLS:
        wait = max(1, int(MIN_SECONDS_BETWEEN_CALLS - (now_ts - last)) + 1)
        raise HTTPException(status_code=429, detail="Please wait a moment.", headers={"Retry-After": str(wait)})
    if len(_last_call_at) > 5000:
        _last_call_at.clear()
    _last_call_at[interview_id] = now_ts

    current_call_count = interview.chat_call_count or 0
    if current_call_count >= MAX_CHAT_CALLS_PER_INTERVIEW:
        logger.warning(f"/api/chat call-count limit hit: interview {interview_id}, user {current_user.id}")
        raise HTTPException(status_code=429, detail="This interview has reached its message limit")

    messages = request.get("messages", [])
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES_IN_PAYLOAD or not messages:
        raise HTTPException(status_code=400, detail="Invalid or oversized messages payload")

    task = "feedback" if request.get("task") == "feedback" else "interview"
    messages = _prepare_messages(messages, task)

    try:
        temperature = float(request.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    temperature = min(1.2, max(0.0, temperature))
    # The report is an evaluation, not a creative task — keep it steadier.
    if task == "feedback":
        temperature = min(temperature, 0.4)

    try:
        requested_tokens = int(request.get("max_tokens", 800))
    except (TypeError, ValueError):
        requested_tokens = 800
    max_tokens = max(64, min(requested_tokens, SERVER_MAX_TOKENS))

    interview.chat_call_count = current_call_count + 1
    db.commit()

    # Report = quality first (Gemini primary). Live turns = latency first (Groq primary).
    if task == "feedback":
        order = [("gemini", _call_gemini), ("groq", _call_groq)]
    else:
        order = [("groq", _call_groq), ("gemini", _call_gemini)]
    # If the primary was just rate-limited, don't poke it again — go straight to the other.
    if _in_cooldown(order[0][0]) and not _in_cooldown(order[1][0]):
        order.reverse()

    last_error = None
    for name, fn in order:
        try:
            result = await fn(messages, temperature, max_tokens, task)
            logger.info(f"/api/chat task='{task}' served by {name}")
            return result
        except RuntimeError as e:
            last_error = e
            logger.warning(f"{name} unavailable for task='{task}' ({e})")

    # Nothing answered — this attempt shouldn't count against the interview's cap.
    try:
        interview.chat_call_count = max(0, (interview.chat_call_count or 1) - 1)
        db.commit()
    except Exception:
        db.rollback()
    _last_call_at.pop(interview_id, None)
    logger.error(f"All AI providers failed for task='{task}': {last_error}")
    raise HTTPException(status_code=503, detail="AI service is busy right now — please retry in a few seconds.",
                        headers={"Retry-After": "5"})