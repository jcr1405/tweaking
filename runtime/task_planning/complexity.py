"""Lightweight Deterministic Task Complexity Classifier
Evaluates incoming user prompts against structural, semantic, and operational indicators
to categorize complexity without incurring conversational LLM overhead.
"""

import re
import logging
from typing import Dict, Any, List, Tuple
from runtime.task_planning.models import ComplexityCategory

logger = logging.getLogger("presence.task_planning.complexity")


class ComplexityClassifier:
    """Classifies user prompts into operational complexity tiers."""

    # Pure conversational / informational inquiry prefixes (Discussion Only)
    DISCUSSION_PREFIXES = (
        "explain", "can you explain", "how do i", "how can i", "how does",
        "how would", "how could", "tell me about", "what is", "what are",
        "why is", "why does", "what if", "compare", "what difference",
        "difference between", "help me understand", "give me advice on",
        "describe", "can you describe", "what would happen", "which",
        "is there", "are there", "does", "who", "where", "do you", "do you have",
        "what happened", "what happend", "what occurred", "what failed",
        "why did", "why was", "it feels like", "it seems", "i feel", "i think",
        "i noticed", "sometimes", "well sometimes"
    )

    # Fast conversational greetings / social prefixes
    SIMPLE_PREFIXES = (
        "hi", "hello", "hey", "who is", "why", "help", "thanks",
        "thank you", "good morning", "good evening", "status",
        "ya", "yeah", "yo", "ok", "okay", "alright", "dude", "bro", "man", "well", "so"
    )

    # Explicit negative instructions that forbid operational execution / TDEL
    NEGATIVE_INSTRUCTION_PATTERN = re.compile(
        r"(?:\bnot a task\b|"
        r"\b(?:this is|it is|it's) (?:a |an )?(?:conversational |status |discussion |informational )?question\b|"
        r"\bdo not (?:invoke|create|use|execute|run|modify|touch|edit)\b|"
        r"\bdon't (?:invoke|create|use|execute|run|modify|touch|edit)\b|"
        r"\bdo not (?:plan|interact)\b|"
        r"\bwithout (?:executing|running|modifying|invoking)\b|"
        r"\bno execution\b|"
        r"\bdo not create (?:a )?(?:dag|workflow|plan)\b)",
        re.IGNORECASE
    )

    # Capability queries asking about system ability without commanding action
    CAPABILITY_QUERY_PATTERN = re.compile(
        r"(?:\b(?:what are|list|tell me|give me a (?:direct )?report of) (?:your |the )?(?:current )?capabilities\b|"
        r"\bwhat can you (?:do|access|execute|run|inspect)\b|"
        r"\bwhich (?:tools|desktop tools|capabilities|models|actions) are (?:actually )?(?:callable|available|registered|required)\b|"
        r"\bcan you (?:read, search, create|inspect codebases|operate windows|use tdel|read files|edit files|run powershell|terminate processes|run tests)\b|"
        r"\bdo you have (?:access to|the ability to)\b|"
        r"\bare you able to\b|"
        r"\bhow does presence (?:work|coordinate|handle)\b)",
        re.IGNORECASE
    )

    # Explicit plan-only request (plan creation requested, execution forbidden)
    PLAN_ONLY_PATTERN = re.compile(
        r"(?:\bplan[- ]only\b|"
        r"\bcreate (?:a )?plan .*\b(?:do not|don't|without) (?:execute|executing|run|running)\b|"
        r"\bdraft (?:a )?(?:plan|dag|workflow) .*\b(?:do not|don't|without) (?:execute|executing|run|running)\b|"
        r"\bgenerate (?:a )?plan .*\bdo not (?:execute|run)\b)",
        re.IGNORECASE
    )

    # Explicit single-turn tool invocation instructions that should execute directly in agent turn
    DIRECT_TOOL_INVOCATION_PATTERN = re.compile(
        r"(?:\busing presence\.\w+\b|"
        r"\bdelegate_local_work\b|"
        r"\bpresence\.delegate_local_work\b|"
        r"\bpresence\.web_search\b|"
        r"\bpresence\.fetch_web_page\b)",
        re.IGNORECASE
    )

    # Multi-target / list patterns
    ENUMERATION_PATTERN = re.compile(
        r"(?:for (?:each|all|these|the following))|(?:\b(?:apps|tools|repos|files|endpoints)\b\s*[:\-])|"
        r"(?:[a-zA-Z0-9_\-\.]+(?:\s*,\s*[a-zA-Z0-9_\-\.]+){2,})",  # 3+ comma separated items
        re.IGNORECASE
    )

    # Sequential chaining conjunctions (sequential execution, not conversational 'and also')
    CHAINING_PATTERN = re.compile(
        r"\b(?:and then|after that|subsequently|followed by|once done|then check|and bring|and generate|and produce)\b",
        re.IGNORECASE
    )

    # Long-running process pattern
    LONG_RUNNING_PATTERN = re.compile(
        r"\b(?:build|compile|install dependencies|run (?:the |all )?tests?|run test suite|"
        r"failing tests|pytest|npm install|cargo build|pip install|benchmark|stress test|full scan|deep scan)\b",
        re.IGNORECASE
    )

    # Research-heavy pattern
    RESEARCH_PATTERN = re.compile(
        r"\b(?:investigate|research|scour|search github|find mcps?|"
        r"find compatible|compare implementations|literature review|"
        r"discover repositories|explore multiple|market research)\b",
        re.IGNORECASE
    )

    # Complex audit/diagnostic/inventory pattern
    COMPLEX_PATTERN = re.compile(
        r"\b(?:audit|diagnose|configure|reconstruct|reverse engineer|"
        r"multi-step|workflow|plan and execute|inspect all|system report|"
        r"most used apps|make a report|report with all|"
        r"inventory|installed apps|installed desktop applications|"
        r"desktop applications|list installed applications|categorize|"
        r"gpu compute|cuda|driver incompatibility|compatibility issues|"
        r"development environment|dev environment|bottleneck|"
        r"inspect (?:this |the |my )?repository|inspect (?:this |the |my )?repo|inspect (?:this |the |my )?project|"
        r"codebase audit|backend architecture|"
        r"delete unnecessary|clean up|remove files|purge|optimize my system)\b",
        re.IGNORECASE
    )

    @classmethod
    def strip_quotes_and_code(cls, text: str) -> str:
        """Strips code fences, inline code, and quoted blocks to prevent false positive keyword matches."""
        cleaned = re.sub(r"```[\s\S]*?```", " ", text)
        cleaned = re.sub(r"`[^`]*`", " ", cleaned)
        cleaned = re.sub(r'"[^"]*"', " ", cleaned)
        cleaned = re.sub(r"'[^']*'", " ", cleaned)
        return cleaned

    @classmethod
    def is_plan_only(cls, prompt: str) -> bool:
        """Returns True if the user asks for a plan without executing it."""
        if not prompt or not isinstance(prompt, str):
            return False
        return bool(cls.PLAN_ONLY_PATTERN.search(prompt))

    @classmethod
    def is_discussion_only(cls, prompt: str) -> bool:
        """Returns True if the prompt is asking for explanation, discussion, or capability status rather than execution."""
        if not prompt or not isinstance(prompt, str):
            return True
        cleaned = prompt.strip().lower()

        # 1. Explicit negative instructions ("not a task", "do not invoke tdel", "do not execute any tools")
        if cls.NEGATIVE_INSTRUCTION_PATTERN.search(cleaned):
            return True

        # 2. Plan-only request (user explicitly asks not to execute)
        if cls.is_plan_only(cleaned):
            return True

        # 3. Capability / status inquiries ("what are your capabilities", "can you read files?")
        if cls.CAPABILITY_QUERY_PATTERN.search(cleaned):
            if not ("and execute it" in cleaned or "by auditing and executing" in cleaned):
                return True

        # 4. Known conversational / discussion prefixes
        for prefix in cls.DISCUSSION_PREFIXES:
            if cleaned.startswith(prefix):
                # Check if it specifically commands operational execution
                if ("could you inspect" in cleaned or "can you inspect my" in cleaned or 
                    "and report failures" in cleaned or "and fix" in cleaned or 
                    "by auditing and" in cleaned or "and execute" in cleaned):
                    return False
                return True

        # 5. Hypothetical inquiry ("if I wanted to...", "what would happen if...")
        if cleaned.startswith("if i") or "what would happen if" in cleaned:
            return True

        # 6. Generic questions ending with '?' without operational imperative commands
        if cleaned.endswith("?") and not any(kw in cleaned for kw in ["execute this", "audit and fix", "clean up the", "inspect my project", "run the tests and"]):
            if any(cleaned.startswith(p) for p in ["can you", "are you", "what", "how", "why", "which", "do you"]):
                return True

        return False

    @classmethod
    def classify(cls, prompt: str) -> ComplexityCategory:
        """Determines the complexity category for a user prompt."""
        if not prompt or not isinstance(prompt, str):
            return ComplexityCategory.SIMPLE

        # 1. Informational discussion / explanation inquiries bypass decomposition
        if cls.is_discussion_only(prompt):
            return ComplexityCategory.SIMPLE

        # 2. Direct tool invocation instructions execute within conversational agent turn
        if cls.DIRECT_TOOL_INVOCATION_PATTERN.search(prompt):
            return ComplexityCategory.SIMPLE

        # 3. Strip quotes and code snippets before scanning operational keywords
        unquoted = cls.strip_quotes_and_code(prompt).strip().lower()

        # 3. Very short messages without chaining or lists are almost always SIMPLE
        if len(unquoted) < 50:
            if not cls.CHAINING_PATTERN.search(unquoted) and not cls.ENUMERATION_PATTERN.search(unquoted):
                return ComplexityCategory.SIMPLE

        # 4. Check simple conversational greetings
        for prefix in cls.SIMPLE_PREFIXES:
            if unquoted.startswith(prefix) and len(unquoted) < 120 and not cls.ENUMERATION_PATTERN.search(unquoted):
                if not cls.CHAINING_PATTERN.search(unquoted):
                    return ComplexityCategory.SIMPLE

        # 5. Detect Long-Running process tasks
        if cls.LONG_RUNNING_PATTERN.search(unquoted):
            return ComplexityCategory.LONG_RUNNING

        # 6. Detect Research-Heavy tasks
        if cls.RESEARCH_PATTERN.search(unquoted) and (cls.ENUMERATION_PATTERN.search(unquoted) or cls.CHAINING_PATTERN.search(unquoted) or "mcp" in unquoted or "repo" in unquoted):
            return ComplexityCategory.RESEARCH_HEAVY

        # 7. Detect Complex audits, inventory, diagnostics and system tasks
        has_complex = bool(cls.COMPLEX_PATTERN.search(unquoted))
        has_chaining = bool(cls.CHAINING_PATTERN.search(unquoted))
        has_enumeration = bool(cls.ENUMERATION_PATTERN.search(unquoted))

        if has_complex or (has_chaining and has_enumeration):
            return ComplexityCategory.COMPLEX

        # 8. Multi-step if chaining OR enumeration is present WITH operational action verb
        if has_chaining or has_enumeration:
            action_verbs = (
                "run", "execute", "install", "download", "build", "search", "create",
                "delete", "move", "clean", "organize", "inspect", "audit", "test",
                "compile", "launch", "open", "deploy", "pipe", "clone", "fetch"
            )
            if any(re.search(rf"\b{v}\b", unquoted) for v in action_verbs):
                return ComplexityCategory.MULTI_STEP

        # Default fallback
        return ComplexityCategory.SIMPLE

    @classmethod
    def requires_decomposition(cls, category: ComplexityCategory) -> bool:
        """Returns True if the category justifies structured TaskPlan decomposition."""
        return category in (
            ComplexityCategory.COMPLEX,
            ComplexityCategory.LONG_RUNNING,
            ComplexityCategory.RESEARCH_HEAVY,
            ComplexityCategory.MULTI_STEP,
        )
