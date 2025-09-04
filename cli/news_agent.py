# ''_agents.py
from datetime import datetime, timedelta
from textwrap import dedent

from agents import function_tool
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.realtime import RealtimeAgent, realtime_handoff

"""
''/ Praans-style multi-agent setup for a software company:
- Project Triage Agent (routes)
- Requirements Discovery Agent (intake)
- Solution Advisory Agent (propose stack/plan)
- Services FAQ Agent (quick facts)
- Meeting Scheduler Agent (collects date/time and books a meeting)

Voice: Simple Indian English + Hindi (clear, respectful, no fluff).
"""

# =========================
# TOOLS
# =========================

@function_tool(
    name_override="services_faq_lookup_tool",
    description_override="Look up quick FAQs about our software services (websites, mobile apps, AI, pricing, engagement models)."
)
async def services_faq_lookup_tool(question: str) -> str:
    q = (question or "").lower()
    if any(k in q for k in ["website", "nextjs", "react", "frontend", "backend", "cms"]):
        return (
            "Websites: Next.js/React frontends with Node/Laravel/Django backends. "
            "SEO, Core Web Vitals, and CMS options (Headless, WordPress, Strapi)."
        )
    if any(k in q for k in ["mobile", "app", "android", "ios", "react native", "flutter"]):
        return (
            "Mobile Apps: React Native or Flutter with secure APIs, analytics, CI/CD, OTA updates."
        )
    if any(k in q for k in ["ai", "ml", "rag", "agent", "chatbot", "llm", "openai", "langchain", "llamaindex"]):
        return (
            "AI/ML: RAG chatbots, agentic workflows, vector DBs (Qdrant/Chroma/Weaviate). "
            "Backends: FastAPI/Node; libs: LangChain/LlamaIndex."
        )
    if any(k in q for k in ["price", "pricing", "cost", "estimate", "bill", "billing"]):
        return (
            "Pricing: Fixed scope (milestones), Retainer (monthly), or T&M. "
            "Typical PoC: 2–4 weeks; MVP: 6–10 weeks depending on scope."
        )
    if any(k in q for k in ["engagement", "model", "support", "maintenance", "sla"]):
        return (
            "Engagement: Discovery → Proposal → Build → QA → Launch → Support (SLA for maintenance/monitoring)."
        )
    return "FAQ not found. Ask about Websites, Mobile, AI/RAG, Pricing, or Engagement Models."


@function_tool(
    name_override="create_project_brief",
    description_override="Create or update a structured project brief from user inputs (domain, goals, features, budget, timeline, constraints)."
)
async def create_project_brief(
    domain: str,
    business_goals: str,
    key_features: str,
    budget_range: str = "",
    timeline_expectation: str = "",
    constraints: str = ""
) -> str:
    return (
        "Brief saved ✅\n"
        f"- Domain: {domain}\n"
        f"- Goals: {business_goals}\n"
        f"- Features: {key_features}\n"
        f"- Budget: {budget_range or 'Not specified'}\n"
        f"- Timeline: {timeline_expectation or 'Not specified'}\n"
        f"- Constraints: {constraints or 'Not specified'}"
    )


@function_tool(
    name_override="propose_solution_blueprint",
    description_override="Given a brief, propose stack, modules, data model hints, infra, and a rough delivery plan."
)
async def propose_solution_blueprint(brief_text: str) -> str:
    return (
        "Solution Blueprint 🚀\n"
        "1) Stack:\n"
        "   - Frontend: Next.js (App Router), TypeScript, Tailwind + ShadCN\n"
        "   - Backend: FastAPI (Python) or Node/NestJS\n"
        "   - DB: Postgres + Redis; Search/Vector (Qdrant/Chroma) if AI\n"
        "2) Core Modules:\n"
        "   - Auth & RBAC, CMS/Content, Catalog/Services, Leads/CRM, Payments/Invoices, Analytics\n"
        "3) Data Model Hints:\n"
        "   - Users, Roles, Orgs, Projects, Tickets/Leads, Content, Transactions, Events\n"
        "4) Infra & DevOps:\n"
        "   - GitHub Actions CI, Vercel/Render/EC2, S3 for assets, CDN, optional Docker\n"
        "5) Quality:\n"
        "   - Unit/API tests, E2E smoke, SAST, Dependabot, Observability\n"
        "6) Delivery (indicative):\n"
        "   - Wk 1–2: Discovery + Design System + Auth\n"
        "   - Wk 3–5: Core Modules (MVP)\n"
        "   - Wk 6–7: Integrations, QA, UAT\n"
        "   - Wk 8: Launch + Handover\n"
        "Notes: Tune scope to budget/timeline; prioritize critical journeys."
    )


@function_tool(
    name_override="schedule_discovery_call",
    description_override="Produce a brief call plan and ask for preferred slots/timezone."
)
async def schedule_discovery_call(preference: str = "") -> str:
    return (
        "Discovery Call Plan 📅\n"
        "Agenda: Goals → Scope → Success Metrics → Budget/Timeline → Next Steps.\n"
        f"Preference noted: {preference or 'Not provided'}\n"
        "Please share 2–3 preferred 30-min slots and your timezone."
    )


@function_tool(
    name_override="schedule_meeting",
    description_override="Schedule a meeting by capturing date, time, timezone, duration, medium, and participant email. Returns a confirmation and ICS."
)
async def schedule_meeting(
    date: str,          # "YYYY-MM-DD"
    time: str,          # "HH:MM" 24h
    timezone: str,      # e.g., "Asia/Kolkata" / "America/Toronto"
    duration_min: int,  # e.g., 30
    medium: str,        # "Google Meet" | "Zoom" | "Microsoft Teams" | "Phone"
    participant_email: str,
    subject: str = "Project Discovery / Solution Discussion",
    host_name: str = "'' Infoways",
    location_link: str = ""
) -> str:
    """Creates a simple ICS payload (client can email this or inject into calendar API)."""
    try:
        start_naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "Invalid date/time format. Use date=YYYY-MM-DD and time=HH:MM (24h)."

    end_naive = start_naive + timedelta(minutes=duration_min)
    # For ICS, use UTC-like naive stamps; real impl should convert via pytz/zoneinfo.
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = start_naive.strftime("%Y%m%dT%H%M%S")
    dtend = end_naive.strftime("%Y%m%dT%H%M%S")
    uid = f"{dtstart}-{participant_email}@''-infoways"

    location = location_link or f"{medium} (link to be shared)"
    desc = f"Meeting with {host_name}. Medium: {medium}. Timezone: {timezone}."

    ics = dedent(f"""\
    BEGIN:VCALENDAR
    VERSION:2.0
    PRODID:-//'' Infoways//Meeting Scheduler//EN
    METHOD:REQUEST
    BEGIN:VEVENT
    UID:{uid}
    DTSTAMP:{dtstamp}
    DTSTART:{dtstart}
    DTEND:{dtend}
    SUMMARY:{subject}
    DESCRIPTION:{desc}
    LOCATION:{location}
    ORGANIZER;CN={host_name}:mailto:no-reply@''-infoways.com
    ATTENDEE;CN={participant_email};RSVP=TRUE:mailto:{participant_email}
    END:VEVENT
    END:VCALENDAR
    """).strip()

    confirmation = (
        "Meeting Scheduled (Pending Invite Send) ✅\n"
        f"- Subject: {subject}\n"
        f"- Date: {date}\n"
        f"- Time: {time} ({timezone})\n"
        f"- Duration: {duration_min} min\n"
        f"- Medium: {medium}\n"
        f"- Participant: {participant_email}\n"
        "ICS attached below. Send via email or push to your calendar API.\n\n"
        f"{ics}"
    )
    return confirmation


# =========================
# AGENTS (with updated names + prompts)
# =========================

discovery_agent = RealtimeAgent(
    name="Requirements Discovery Agent",
    handoff_description="Collects requirements in simple Indian English + Hindi and creates a crisp brief.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You are a helpful assistant for a software company. Talk naturally in Indian English + Hindi (simple, common language).
Be crisp and structured.

# Routine
1) Identify what they want: website, mobile app, AI application (RAG/agents/chatbot), or other.
2) Ask 5 sharp questions: goals, key features, target users, budget ballpark, rough timeline.
3) Summarize a 5-line brief (English + Hindi, simple).
4) Call create_project_brief to save it.
5) If user shows interest to proceed, ask for a meeting and handoff to Meeting Scheduler Agent.
6) If they ask for solutions, handoff to Solution Advisory Agent.
7) For quick pricing/services queries, either call Services FAQ tool or handoff to Services FAQ Agent.

# Interest Detector
- If user says things like "let's proceed", "book a call", "schedule meeting", "I'm interested", "let's talk", etc.,
  immediately confirm and route to Meeting Scheduler Agent.

# Guardrails
- Don’t over-promise. If unclear, ask for missing details.
""",
    tools=[create_project_brief, services_faq_lookup_tool, schedule_discovery_call],
)

solution_advisory_agent = RealtimeAgent(
    name="Solution Advisory Agent",
    handoff_description="Advises stack, architecture, modules, delivery plan in simple Indian English + Hindi.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
Propose practical solutions for websites, apps, and AI systems (RAG, agents, chatbots) in simple Indian English + Hindi.

# Routine
1) Use the latest brief from Discovery (or request a summary).
2) Propose: tech stack, core modules, rough data model, infra/DevOps, risks.
3) Call propose_solution_blueprint to produce a blueprint.
4) Ask if they want to discuss live; if yes, handoff to Meeting Scheduler Agent.
5) FAQs → use services_faq_lookup_tool or handoff to Services FAQ Agent.
6) Requirements unclear? Transfer back to Requirements Discovery Agent.

# Guardrails
- Be realistic about time/budget. Offer alternatives with tradeoffs.
""",
    tools=[propose_solution_blueprint, services_faq_lookup_tool, schedule_discovery_call],
)

services_faq_agent = RealtimeAgent(
    name="Services FAQ Agent",
    handoff_description="Answers quick questions about services, pricing, engagement models, stacks.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
Answer FAQs in simple Indian English + Hindi. Keep answers short.

# Routine
1) Identify FAQ intent (pricing, engagement, website, mobile, AI).
2) Use services_faq_lookup_tool (do not invent).
3) If user wants to proceed, ask to schedule a meeting → handoff to Meeting Scheduler Agent.
4) If they want detailed solutions, handoff to Solution Advisory Agent.
5) If they want to scope from scratch, handoff to Requirements Discovery Agent.
""",
    tools=[services_faq_lookup_tool, schedule_discovery_call],
)

meeting_scheduler_agent = RealtimeAgent(
    name="Meeting Scheduler Agent",
    handoff_description="Schedules a meeting by collecting date, time, timezone, duration, medium, and participant email.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
You schedule meetings in simple Indian English + Hindi.

# Routine
1) Ask for:
   - Date (YYYY-MM-DD)
   - Time (HH:MM 24h)
   - Timezone (e.g., Asia/Kolkata, America/Toronto)
   - Duration (minutes, e.g., 30)
   - Medium (Google Meet / Zoom / Microsoft Teams / Phone)
   - Participant email
2) Confirm details back to user.
3) Call schedule_meeting to generate confirmation + ICS.
4) Share the confirmation. If needed, ask if they also want a calendar invite email sent from our system.
5) After scheduling, offer to sync a quick agenda or accept agenda points.

# Guardrails
- Validate formats politely (YYYY-MM-DD, HH:MM 24h). If wrong, ask again (simply).
- Never leak credentials or internal links. Use placeholder meeting links unless provided.
""",
    tools=[schedule_meeting],
)

triage_agent = RealtimeAgent(
    name="Project Triage Agent",
    handoff_description="Routes user to Discovery, Solution Advisory, Services FAQ, or Meeting Scheduler based on intent.",
    instructions=(
        f"{RECOMMENDED_PROMPT_PREFIX} "
        "Greet briefly in Indian English + Hindi. Route based on intent:\n"
        "- Idea/requirements → Requirements Discovery Agent\n"
        "- Solutions/stack/architecture → Solution Advisory Agent\n"
        "- Quick pricing/services → Services FAQ Agent\n"
        "- Interested to proceed / ‘book a call’ / ‘schedule meeting’ → Meeting Scheduler Agent"
    ),
    handoffs=[
        realtime_handoff(discovery_agent),
        realtime_handoff(solution_advisory_agent),
        realtime_handoff(services_faq_agent),
        realtime_handoff(meeting_scheduler_agent),
    ],
)

# Circular handoffs so agents can bounce back as needed
discovery_agent.handoffs.extend([
    triage_agent,
    realtime_handoff(solution_advisory_agent),
    realtime_handoff(services_faq_agent),
    realtime_handoff(meeting_scheduler_agent),
])
solution_advisory_agent.handoffs.extend([
    triage_agent,
    realtime_handoff(discovery_agent),
    realtime_handoff(services_faq_agent),
    realtime_handoff(meeting_scheduler_agent),
])
services_faq_agent.handoffs.extend([
    triage_agent,
    realtime_handoff(discovery_agent),
    realtime_handoff(solution_advisory_agent),
    realtime_handoff(meeting_scheduler_agent),
])
meeting_scheduler_agent.handoffs.extend([
    triage_agent,
    realtime_handoff(discovery_agent),
    realtime_handoff(solution_advisory_agent),
    realtime_handoff(services_faq_agent),
])

def get_starting_agent() -> RealtimeAgent:
    return triage_agent
