import copy
import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

import streamlit as st
from anthropic import Anthropic, APIError, APIStatusError, RateLimitError
from dotenv import load_dotenv
from personas import PERSONAS, build_persona_prompt, get_suggested_prompts, get_followup_prompts


load_dotenv()


MODEL_NAME = "claude-sonnet-4-5"
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_FETCH_TOOL_TYPE = "web_fetch_20250910"
WEB_FETCH_BETA_HEADER = "web-fetch-2025-09-10"
DEFAULT_WEB_SEARCH_MAX_USES = 10
DEFAULT_WEB_FETCH_MAX_USES = 10
LOGO_CANDIDATES = ["honda_logo.png", "Honda-Logo.wine.png"]
BASE_SYSTEM_PROMPT = (
    "You are an automotive market intelligence analyst supporting a Honda India POC. "
    "Blend insights from the provided internal dataset with up-to-date public information. "
    "When internal context is supplied, cite it explicitly alongside any web sources. "
    "Prioritise synthesising trends and themes over numeric precision. "
    "Adopt a warm, conversational tone, weaving in brief storytelling or plain-language explanations so insights feel approachable to business stakeholders. "
    "Highlight 3-5 takeaways using short paragraphs or bullet-style callouts when helpful, and keep responses tight and human—not robotic. "
    "Only mention your affiliation with EMB Global if the user directly asks about your identity or organisation. "
    "Never mention Anthropic, Claude, or any underlying model names or providers. "
    "If asked about your identity or capabilities, give a concise response that you are an EMB Global assistant supporting the Honda market intelligence effort, without listing internal tooling or dataset sources unless the user already cited them."
)

ROLEPLAY_GUARDRAILS = (
    "General guardrails for this simulation: Do not mention underlying model providers or internal tooling. "
    "Cite web sources when used. Keep tone natural and human. If tools are unavailable, answer from lived experience and state uncertainty briefly when relevant."
)
DATASET_PATH = Path(__file__).with_name("honda_data_sources.json")
PREDEFINED_QUESTIONS = [
    "What are the top features customers talk about for mid-range SUVs?",
    "What issues are EV users reporting most frequently?",
    "How does Honda’s brand sentiment compare to Hyundai?",
    "Which sources highlight gaps in Honda’s value proposition?",
    "What charging challenges appear across Indian EV apps?",
    "Which forums surface repeated complaints about Honda sedans?",
    "Are people generally dissatisfied with Honda’s suspension?",
    "Are hybrid cars really popular among Indian consumers? Does it make sense for Honda to launch an Elevate hybrid model?",
    "Does Honda's brand position in India resonate with consumers?"
]
MAX_INTERNAL_CONTEXT_ROWS = 4


def request_rerun() -> None:
    """Trigger a Streamlit rerun in a version-compatible way."""
    try:
        st.experimental_rerun()
    except AttributeError:
        try:
            from streamlit.runtime.scriptrunner import RerunData, RerunException
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("Streamlit rerun is unavailable in this environment.") from err
        raise RerunException(RerunData())


@st.cache_data(show_spinner=False)
def load_internal_dataset(path: Path) -> List[Dict[str, str]]:
    """Load structured data that emulates the Honda Excel workbook."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
    except Exception:
        pass
    return []


def tokenize_text(text: str) -> List[str]:
    """Return lowercase tokens for simple keyword matching."""
    if not text:
        return []
    return re.findall(r"\b\w+\b", text.lower())


def format_record_summary(record: Dict[str, Any]) -> str:
    """Produce a compact one-line summary for a dataset entry."""
    parts = []
    source = record.get("source")
    category = record.get("category")
    if source:
        parts.append(source if not category else f"{source} [{category}]")
    insight = record.get("customer_insights")
    if insight:
        parts.append(f"Key insight: {insight}")
    remark = record.get("remark")
    if remark:
        parts.append(f"Notes: {remark}")
    details = record.get("details")
    if details:
        parts.append(f"Details: {details}")
    return " | ".join(parts)


def build_dataset_context(
    query: str,
    dataset: List[Dict[str, Any]],
    limit: int = MAX_INTERNAL_CONTEXT_ROWS,
) -> str:
    """Select the most relevant dataset rows for a given prompt."""
    if not dataset:
        return ""

    query_terms = set(tokenize_text(query))
    scored_records: List[tuple[int, Dict[str, Any]]] = []
    for record in dataset:
        haystack = " ".join(
            str(record.get(field, "")) for field in ("source", "category", "details", "customer_insights", "remark")
        )
        haystack_terms = set(tokenize_text(haystack))
        score = sum(1 for term in query_terms if term in haystack_terms)
        if score or not query_terms:
            scored_records.append((score, record))

    if not scored_records:
        return ""

    scored_records.sort(key=lambda item: item[0], reverse=True)
    top_records = [record for score, record in scored_records if score > 0][:limit]
    if not top_records:
        top_records = [scored_records[0][1]]

    summaries = [format_record_summary(record) for record in top_records]
    return "\n".join(f"- {summary}" for summary in summaries if summary)


def build_user_content(
    prompt: str,
    dataset: List[Dict[str, Any]],
    datetime_context: str | None = None,
) -> List[Dict[str, Any]]:
    """Compose the structured Anthropic message content for a user turn."""
    content_blocks: List[Dict[str, Any]] = []
    if datetime_context:
        content_blocks.append(
            {
                "type": "text",
                "text": (
                    "User time context:\n"
                    f"{datetime_context}\n"
                    "Treat timestamps as the user's current local view."
                ),
            }
        )
    context = build_dataset_context(prompt, dataset)
    if context:
        content_blocks.append(
            {
                "type": "text",
                "text": (
                    "Internal dataset excerpts (Honda Data Sources workbook):\n"
                    f"{context}\n"
                    "Use this structured context when forming your answer."
                ),
            }
        )
    content_blocks.append({"type": "text", "text": prompt})
    return content_blocks


def get_configured_api_key() -> str:
    """Return an API key sourced from Streamlit secrets or environment variables."""
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:  # noqa: BLE001
        # Accessing st.secrets outside of a Streamlit runtime raises an error.
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def get_client(api_key: str | None) -> Anthropic:
    """Return an Anthropic client, preferring an explicit key when provided."""
    if api_key:
        return Anthropic(api_key=api_key.strip())
    return Anthropic()


def message_to_dict(message: Any) -> Dict[str, Any]:
    """Convert an Anthropic SDK message object to a plain dictionary."""
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if hasattr(message, "dict"):
        return message.dict()
    if hasattr(message, "to_dict"):
        return message.to_dict()
    raise TypeError("Unsupported message type returned by Anthropic SDK.")


def build_tool_config(
    enable_web_search: bool,
    enable_web_fetch: bool,
) -> List[Dict[str, Any]]:
    """Assemble the list of tools to pass to the Anthropic assistant."""
    tools: List[Dict[str, Any]] = []
    if enable_web_search:
        search_tool: Dict[str, Any] = {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": DEFAULT_WEB_SEARCH_MAX_USES,
        }
        tools.append(search_tool)
    if enable_web_fetch:
        fetch_tool: Dict[str, Any] = {
            "type": WEB_FETCH_TOOL_TYPE,
            "name": "web_fetch",
            "max_uses": DEFAULT_WEB_FETCH_MAX_USES,
            "citations": {"enabled": True},
        }
        tools.append(fetch_tool)
    return tools


def format_blocks_for_display(blocks: List[Dict[str, Any]]) -> str:
    """Turn the assistant's structured response blocks into readable markdown."""
    segments: List[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in {"thinking", "redacted_thinking"}:
            continue
        if block_type == "text":
            text = block.get("text", "")
            citations = block.get("citations") or []
            if citations:
                citation_lines = []
                for citation in citations:
                    title = citation.get("title") or citation.get("url") or "Source"
                    url = citation.get("url")
                    cited_text = citation.get("cited_text")
                    line = f"- {title}"
                    if url:
                        line = f"- [{title}]({url})"
                    if cited_text:
                        line += f" — {cited_text}"
                    citation_lines.append(line)
                if citation_lines:
                    text = f"{text}\n\n**Citations:**\n" + "\n".join(citation_lines)
            segments.append(text.strip())
        elif block_type == "server_tool_use":
            continue
        elif block_type == "web_search_tool_result":
            content = block.get("content", [])
            if isinstance(content, dict) and content.get("type") == "web_search_tool_result_error":
                segments.append(f"⚠️ Web search error: {content.get('error_code', 'unknown error')}")
                continue
            continue
        elif block_type == "web_fetch_tool_result":
            raw_content = block.get("content")
            entries: List[Dict[str, Any]] = []
            if isinstance(raw_content, dict):
                entries = [raw_content]
            elif isinstance(raw_content, list):
                entries = [item for item in raw_content if isinstance(item, dict)]
            if not entries:
                continue
            for item in entries:
                item_type = item.get("type")
                if item_type in {"web_fetch_tool_result_error", "web_fetch_tool_error"} or (
                    item_type is None and "error_code" in item
                ):
                    segments.append(f"⚠️ Web fetch error: {item.get('error_code', 'unknown error')}")
            continue
        else:
            try:
                block_json = json.dumps(block, ensure_ascii=False)
            except TypeError:
                block_json = str(block)
            segments.append(f"_{block_type}: {block_json}_")

    formatted = "\n\n".join(segment for segment in segments if segment).strip()
    return formatted or "_The assistant returned no readable content._"


def _render_square_image(path: Path, size_px: int = 120) -> None:
    try:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        style = (
            f"width:{size_px}px;height:{size_px}px;object-fit:cover;"
            "border-radius:8px;border:1px solid #eee;display:block;"
        )
        html = f"<img src='data:image/png;base64,{b64}' style=\"{style}\" />"
        st.markdown(html, unsafe_allow_html=True)
    except Exception:
        # Fallback to default renderer if anything goes wrong
        st.image(str(path), width=size_px)


def extract_tool_summary(blocks: List[Dict[str, Any]]) -> str:
    """Produce a compact summary of tool usage for sidebar display."""
    summaries: List[str] = []
    search_results: List[tuple[str | None, str | None]] = []
    fetch_results: List[tuple[str | None, str | None]] = []

    for block in blocks:
        block_type = block.get("type")
        if block_type == "server_tool_use":
            name = block.get("name")
            if name == "web_search":
                query = block.get("input", {}).get("query")
                if query:
                    summaries.append(f"Web search: {query}")
            elif name == "web_fetch":
                url = block.get("input", {}).get("url")
                if url:
                    summaries.append(f"Web fetch: {url}")
        elif block_type == "web_search_tool_result":
            content = block.get("content", [])
            if isinstance(content, list):
                for item in content[:5]:
                    if item.get("type") == "web_search_result":
                        title = item.get("title") or "Search result"
                        url = item.get("url")
                        search_results.append((title, url))
        elif block_type == "web_fetch_tool_result":
            content = block.get("content")
            entries: List[Dict[str, Any]] = []
            if isinstance(content, dict):
                entries = [content]
            elif isinstance(content, list):
                entries = [entry for entry in content if isinstance(entry, dict)]
            for item in entries[:3]:
                url = item.get("url")
                content_block = item.get("content")
                title = None
                if isinstance(content_block, dict):
                    title = content_block.get("title")
                fetch_results.append((title or "Fetched document", url))

    lines: List[str] = []
    if summaries:
        lines.append("**Research actions**")
        lines.extend(f"- {entry}" for entry in summaries)
    if search_results:
        lines.append("**Search hits**")
        for title, url in search_results:
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
    if fetch_results:
        lines.append("**Fetched docs**")
        for title, url in fetch_results:
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
    return "\n".join(lines)


def run_chat_completion(
    client: Anthropic,
    conversation: List[Dict[str, Any]],
    model: str,
    tools: List[Dict[str, Any]] | None,
    system_prompt: str | None = None,
    extra_headers: Dict[str, str] | None = None,
):
    """Send the conversation to the Anthropic assistant and return the full response object."""
    params: Dict[str, Any] = {
        "model": model,
        "max_tokens": 20000,
        "messages": conversation,
        "thinking": {"type": "enabled", "budget_tokens": 12000},
    }
    if tools:
        params["tools"] = tools
    if system_prompt:
        params["system"] = system_prompt
    if extra_headers:
        params["extra_headers"] = extra_headers
    return client.messages.create(**params)


def stream_chat_completion(
    client: Anthropic,
    conversation: List[Dict[str, Any]],
    model: str,
    tools: List[Dict[str, Any]] | None,
    system_prompt: str | None = None,
    extra_headers: Dict[str, str] | None = None,
    on_text_chunk: Callable[[str], None] | None = None,
):
    """Stream an Anthropic assistant response and return the final message object."""
    params: Dict[str, Any] = {
        "model": model,
        "max_tokens": 20000,
        "messages": conversation,
        "thinking": {"type": "enabled", "budget_tokens": 12000},
    }
    if tools:
        params["tools"] = tools
    if system_prompt:
        params["system"] = system_prompt
    if extra_headers:
        params["extra_headers"] = extra_headers
    with client.messages.stream(**params) as stream:
        if on_text_chunk is not None:
            for text in stream.text_stream:
                if text:
                    on_text_chunk(text)
        else:
            for _ in stream.text_stream:
                pass
        if hasattr(stream, "get_final_response"):
            return stream.get_final_response()
        if hasattr(stream, "get_final_message"):
            return stream.get_final_message()
        if hasattr(stream, "final_response"):
            return stream.final_response
        if hasattr(stream, "response"):
            return stream.response
        raise AttributeError("Streaming interface missing a method to retrieve the final response.")


def main() -> None:
    st.set_page_config(
        page_title="Honda Japan Market Research — Based on Indian User Personas",
        page_icon="🤖",
        initial_sidebar_state="collapsed",
    )
    col_logo, col_header = st.columns([1.2, 4])
    with col_logo:
        logo_path = next(
            (Path(__file__).with_name(name) for name in LOGO_CANDIDATES if Path(__file__).with_name(name).exists()),
            None,
        )
        if logo_path:
            st.image(str(logo_path), width=200)
        else:
            st.markdown("**Honda**")
    with col_header:
        # Title and caption removed to avoid duplication with selection section
        pass

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "api_messages" not in st.session_state:
        st.session_state.api_messages = []
    if "last_tool_summary" not in st.session_state:
        st.session_state.last_tool_summary = ""
    if "selected_persona_id" not in st.session_state:
        st.session_state.selected_persona_id = None

    active_api_key = get_configured_api_key()

    internal_dataset = load_internal_dataset(DATASET_PATH)

    if not active_api_key:
        st.error("Anthropic API key missing. Set it in Streamlit secrets or `.env`.")
        st.stop()

    current_dt = datetime.now().astimezone()
    default_time_display = current_dt.strftime("%A, %d %B %Y %H:%M %Z (UTC%z)")
    tz_label = current_dt.tzname() or "local timezone"
    time_context = (
        f"User local datetime: {default_time_display} | Additional context: {tz_label}"
    )

    stream_responses = True

    enable_web_search = True
    enable_web_fetch = True

    tools = build_tool_config(
        enable_web_search=enable_web_search,
        enable_web_fetch=enable_web_fetch,
    )

    extra_headers = None
    if any(tool.get("type") == WEB_FETCH_TOOL_TYPE for tool in tools):
        extra_headers = {"anthropic-beta": WEB_FETCH_BETA_HEADER}

    sources_placeholder = st.sidebar.empty()

    def render_sources() -> None:
        if st.session_state.last_tool_summary:
            lines = st.session_state.last_tool_summary.splitlines()
            content = ["**Key References (latest analysis)**"]
            content.extend(lines)
            sources_placeholder.markdown("\n".join(content))
        else:
            sources_placeholder.write("No live sources referenced yet.")

    render_sources()

    # Sidebar: Persona controls
    st.sidebar.markdown("---")
    active_persona = None
    if st.session_state.selected_persona_id:
        active_persona = next((p for p in PERSONAS if p.id == st.session_state.selected_persona_id), None)
    if active_persona:
        st.sidebar.subheader("Active Persona")
        # Show image if available (square crop for Vikram)
        try:
            if getattr(active_persona, "image", None):
                pth = Path(__file__).with_name(active_persona.image)  # type: ignore[arg-type]
                if pth.exists():
                    if active_persona.id == "vikram-reddy":
                        _render_square_image(pth, 120)
                    else:
                        st.sidebar.image(str(pth), width=120)
        except Exception:
            pass
        st.sidebar.markdown(f"**{active_persona.name}** — {active_persona.label}")
        st.sidebar.caption(active_persona.summary_line)
        with st.sidebar.expander("Details", expanded=False):
            st.markdown("\n".join([
                "**Demographics**",
                *[f"- {k}: {v}" for k, v in active_persona.demographics.items()],
                "",
                "**Key Concerns**",
                *[f"- {it}" for it in active_persona.key_concerns],
                "",
                "**Pain Points**",
                *[f"- {it}" for it in active_persona.pain_points],
            ]))
        if st.sidebar.button("Change persona"):
            st.session_state.selected_persona_id = None
            st.session_state.messages = []
            st.session_state.api_messages = []
            st.session_state.last_tool_summary = ""
            request_rerun()

    if st.sidebar.button("Clear conversation"):
        st.session_state.messages = []
        st.session_state.api_messages = []
        st.session_state.last_tool_summary = ""
        st.session_state.pop("pending_prompt", None)
        st.session_state.pop("suggested_hidden", None)
        render_sources()
        request_rerun()

    # Persona selection screen (first page)
    if not active_persona:
        st.subheader("Honda Japan Market Research — User Personas for India")
        st.markdown("Select a persona to interview. The assistant will roleplay the selected user.")
        st.markdown(
            """
            <style>
            .persona-card { border: 1px solid #eee; padding: 0.75rem; border-radius: 10px; background: #fff; }
            .persona-card .name { font-weight: 700; }
            .persona-card .label { color: #666; font-size: 0.9rem; }
            .persona-card .summary { color: #444; margin-top: 0.25rem; }
            </style>
            """,
            unsafe_allow_html=True,
        )
        cols_per_row = 3
        rows = [PERSONAS[i : i + cols_per_row] for i in range(0, len(PERSONAS), cols_per_row)]
        for row_idx, row in enumerate(rows):
            cols = st.columns(cols_per_row, gap="small")
            for i, persona in enumerate(row):
                with cols[i]:
                    with st.container():
                        # Optional persona image (square crop for Vikram)
                        try:
                            if getattr(persona, "image", None):
                                img_path = Path(__file__).with_name(persona.image)  # type: ignore[arg-type]
                                if img_path.exists():
                                    if persona.id == "vikram-reddy":
                                        _render_square_image(img_path, 120)
                                    else:
                                        st.image(str(img_path), width=120)
                        except Exception:
                            pass
                        st.markdown(
                            f"<div class='persona-card'>"
                            f"<div class='name'>{persona.name}</div>"
                            f"<div class='label'>{persona.label}</div>"
                            f"<div class='summary'>{persona.summary_line}</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                        if st.button(f"Interview {persona.name.split()[0]}", key=f"persona_select_{persona.id}", use_container_width=True):
                            st.session_state.selected_persona_id = persona.id
                            st.session_state.messages = []
                            st.session_state.api_messages = []
                            st.session_state.last_tool_summary = ""
                            request_rerun()
        st.stop()

    # Conversation UI (persona selected)
    top_cols = st.columns([1, 3, 1])
    with top_cols[0]:
        if st.button("← Back to Personas", use_container_width=True):
            st.session_state.selected_persona_id = None
            st.session_state.messages = []
            st.session_state.api_messages = []
            st.session_state.last_tool_summary = ""
            request_rerun()
    with top_cols[1]:
        st.subheader(f"Interviewing {active_persona.name}")
        st.caption(active_persona.label)
    with top_cols[2]:
        try:
            if getattr(active_persona, "image", None):
                pth = Path(__file__).with_name(active_persona.image)  # type: ignore[arg-type]
                if pth.exists():
                    if active_persona.id == "vikram-reddy":
                        _render_square_image(pth, 80)
                    else:
                        st.image(str(pth), width=80)
        except Exception:
            pass
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    suggestions_container = st.container()

    pending_prompt = st.session_state.pop("pending_prompt", None)
    chat_prompt = st.chat_input(f"Ask {active_persona.name.split()[0]} a question…")
    prompt = pending_prompt or chat_prompt

    if st.session_state.messages and not st.session_state.get("suggested_hidden"):
        st.session_state.suggested_hidden = True

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        user_blocks = build_user_content(prompt, internal_dataset, datetime_context=time_context)
        st.session_state.api_messages.append({"role": "user", "content": user_blocks})

        st.session_state.suggested_hidden = True

        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            placeholder = st.empty()
            placeholder.markdown("_Analyzing…_")

            if not active_api_key:
                placeholder.error(
                    "Please provide an Anthropic API key in the sidebar to chat with the assistant."
                )
                return

            client = get_client(active_api_key)

            reply = ""
            assistant_api_message: Dict[str, Any] | None = None
            tool_summary: str = ""
            try:
                # Build system prompt: persona interview mode uses persona roleplay + guardrails only
                active_system_prompt = BASE_SYSTEM_PROMPT
                if active_persona is not None:
                    active_system_prompt = build_persona_prompt(active_persona) + "\n\n" + ROLEPLAY_GUARDRAILS
                if stream_responses:
                    stream_buffer = ""

                    def handle_chunk(chunk: str) -> None:
                        nonlocal stream_buffer
                        stream_buffer += chunk
                        placeholder.markdown(stream_buffer or "_Streaming…_")

                    response = stream_chat_completion(
                        client=client,
                        conversation=st.session_state.api_messages,
                        model=MODEL_NAME,
                        tools=tools,
                        system_prompt=active_system_prompt,
                        extra_headers=extra_headers,
                        on_text_chunk=handle_chunk,
                    )
                else:
                    response = run_chat_completion(
                        client=client,
                        conversation=st.session_state.api_messages,
                        model=MODEL_NAME,
                        tools=tools,
                        system_prompt=active_system_prompt,
                        extra_headers=extra_headers,
                    )
                response_dict = message_to_dict(response)
                response_blocks = response_dict.get("content", [])
                reply = format_blocks_for_display(response_blocks)
                tool_summary = extract_tool_summary(response_blocks)
                assistant_api_message = {
                    "role": "assistant",
                    "content": copy.deepcopy(response_blocks)
                    if response_blocks
                    else [{"type": "text", "text": reply}],
                }
            except RateLimitError:
                reply = "⚠️ Rate limit reached. Please wait a moment before trying again."
            except APIStatusError as err:
                reply = f"API error {err.status_code}: {err.message}"
            except APIError as err:
                reply = f"Unexpected API error: {err}"
            except Exception as err:  # noqa: BLE001
                reply = f"Unhandled error while calling the assistant: {err}"

            reply = reply or "_The assistant returned an empty response._"
            placeholder.markdown(reply)

            st.session_state.messages.append({"role": "assistant", "content": reply})

            if assistant_api_message is None:
                assistant_api_message = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": reply}],
                }
            st.session_state.api_messages.append(assistant_api_message)
            st.session_state.last_tool_summary = tool_summary
            render_sources()

    quick_prompt_triggered = False
    with suggestions_container:
        if active_persona and not st.session_state.get("suggested_hidden") and not st.session_state.messages:
            persona_prompts = get_suggested_prompts(active_persona.id)
            st.markdown("##### Interview starters")
            cols_per_row = 3
            rows = [persona_prompts[i : i + cols_per_row] for i in range(0, len(persona_prompts), cols_per_row)]
            for row_index, row_prompts in enumerate(rows):
                row_cols = st.columns(cols_per_row, gap="small")
                for idx in range(cols_per_row):
                    with row_cols[idx]:
                        try:
                            question = row_prompts[idx]
                        except IndexError:
                            st.markdown("&nbsp;")
                            continue
                        if st.button(question, key=f"persona_quick_{row_index}_{idx}", use_container_width=True):
                            st.session_state.pending_prompt = question
                            st.session_state.suggested_hidden = True
                            request_rerun()
                            quick_prompt_triggered = True
                            break
                if quick_prompt_triggered:
                    break
        elif active_persona and st.session_state.messages:
            followups = get_followup_prompts(active_persona)
            if followups:
                st.markdown("##### Suggested follow-ups")
                cols_per_row = 3
                rows = [followups[i : i + cols_per_row] for i in range(0, len(followups), cols_per_row)]
                for row_index, row_prompts in enumerate(rows):
                    row_cols = st.columns(cols_per_row, gap="small")
                    for idx in range(cols_per_row):
                        with row_cols[idx]:
                            try:
                                question = row_prompts[idx]
                            except IndexError:
                                st.markdown("&nbsp;")
                                continue
                            if st.button(question, key=f"persona_follow_{row_index}_{idx}", use_container_width=True):
                                st.session_state.pending_prompt = question
                                request_rerun()
                                quick_prompt_triggered = True
                                break
                    if quick_prompt_triggered:
                        break


if __name__ == "__main__":
    main()
