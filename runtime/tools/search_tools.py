"""Native Search and Web Fetching Tools
Exposes governed DuckDuckGo web search and web page text extraction to the AgentExecutor
under deterministic L0_OBSERVE authority.
"""

import logging
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition
from runtime.search.governor import SearchGovernor
from runtime.artifacts.store import ArtifactStore

logger = logging.getLogger("presence.tools.search")


def build_search_tools(
    governor: SearchGovernor,
    artifact_store: Optional[ArtifactStore] = None,
    current_conversation_id: str = "global_search"
) -> List[ToolDefinition]:
    """Constructs ToolDefinitions for presence.web_search and presence.fetch_web_page.
    All search traffic is routed through the centralized SearchGovernor.
    """

    def _handle_web_search(args: Dict[str, Any]) -> Dict[str, Any]:
        query = args.get("query", "")
        raw_limit = args.get("max_results") or args.get("limit") or args.get("num_results", 5)
        try:
            max_results = int(raw_limit)
        except (ValueError, TypeError):
            max_results = 5
        max_results = max(1, min(max_results, 10))

        response = governor.search(query=query, max_results=max_results)
        return response.to_dict()

    def _handle_fetch_web_page(args: Dict[str, Any]) -> Dict[str, Any]:
        url = args.get("url", "")
        max_chars = int(args.get("max_chars", 10000))
        max_chars = max(100, min(max_chars, 50000))

        response = governor.fetch_page(url=url, max_chars=max_chars)
        res_dict = response.to_dict()

        # Optional proactive artifact offloading if content exceeds threshold
        if response.is_success and response.content and artifact_store:
            if artifact_store.should_offload(response.content):
                try:
                    meta = artifact_store.store_text_artifact(
                        conversation_id=current_conversation_id,
                        tool_call_id="fetch_page",
                        tool_name="presence.fetch_web_page",
                        content_str=response.content
                    )
                    res_dict["artifact_id"] = meta.artifact_id
                    res_dict["byte_size"] = meta.byte_size
                    res_dict["preview"] = meta.preview
                    res_dict["content"] = meta.preview  # keep in-memory preview small
                except Exception as art_err:
                    logger.warning(f"Failed to offload large web page to artifact store: {art_err}")

        return res_dict

    web_search_tool = ToolDefinition(
        tool_id="presence.web_search",
        name="web_search",
        description=(
            "Searches the web using DuckDuckGo to find public information, articles, documentation, or links. "
            "Governed by anti-burst rate limits and query deduplication."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or search phrase to look up on the web."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to return (default 5, min 1, max 10)."
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional alias for max_results."
                },
                "num_results": {
                    "type": "integer",
                    "description": "Optional alias for max_results."
                }
            },
            "required": ["query"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_web_search,
        timeout_seconds=governor.policy.timeout_seconds + 5.0
    )

    fetch_web_page_tool = ToolDefinition(
        tool_id="presence.fetch_web_page",
        name="fetch_web_page",
        description=(
            "Fetches and extracts clean readable text from a public web page URL. "
            "Subject to strict SSRF validation and per-domain rate limits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full HTTP/HTTPS URL of the web page to fetch."
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum number of characters of extracted text to return (default 10000, max 50000)."
                }
            },
            "required": ["url"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_fetch_web_page,
        timeout_seconds=governor.policy.timeout_seconds + 5.0
    )

    return [web_search_tool, fetch_web_page_tool]
