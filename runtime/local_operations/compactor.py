"""Deterministic and Semantic Context Compaction Engine.
Extracts structured operational findings from raw tool outputs, deduplicates observations,
preserves contradictions, offloads large payloads to ArtifactStore, and enforces token budgets
for the CloudReturnPacket.
"""

import os
import re
import json
import math
import hashlib
import logging
from typing import List, Dict, Any, Optional, Tuple, Set

from runtime.artifacts.store import ArtifactStore
from runtime.artifacts.models import ArtifactMetadata
from runtime.local_operations.models import (
    VerifiedFinding,
    ModelInterpretation,
    Contradiction,
    FailureRecord,
    SubagentReceipt,
    ArtifactReference,
    EscalationRequirement,
    CompactionMetrics,
    CloudReturnPacket,
    OutcomeStatus,
    outcome_to_status,
    TaskDelegationContract,
    DelegationOutcome,
)
from runtime.observability.redaction import sanitize_and_redact

logger = logging.getLogger("presence.local_operations.compactor")

# Size threshold for offloading raw tool outputs to persistent ArtifactStore (4 KB)
OFFLOAD_THRESHOLD_BYTES = 4096


class ToolOutputCompactor:
    """Performs deterministic extraction and structured reduction on raw tool outputs.
    Guarantees that critical failures, exit codes, exceptions, and test results are never silently discarded.
    """

    def __init__(self, artifact_store: Optional[ArtifactStore] = None):
        self.artifact_store = artifact_store

    def compact_tool_output(
        self,
        tool_name: str,
        tool_call_id: str,
        content: Any,
        conversation_id: str,
        step_index: int = 0,
        exit_code: Optional[int] = None,
        artifact_id: Optional[str] = None,
    ) -> Tuple[List[VerifiedFinding], List[FailureRecord], Optional[ArtifactReference]]:
        """Processes a single tool output, performing deterministic extraction, offloading if needed,
        and extracting verified findings and failure records.
        """
        findings: List[VerifiedFinding] = []
        failures: List[FailureRecord] = []
        artifact_ref: Optional[ArtifactReference] = None

        # Convert content to string and bytes
        if isinstance(content, dict) or isinstance(content, list):
            content_str = json.dumps(content, indent=2)
        else:
            content_str = str(content) if content is not None else ""

        content_bytes = content_str.encode("utf-8")
        byte_size = len(content_bytes)
        line_count = len(content_str.splitlines())
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()

        # Check if an artifact ID was passed or is embedded in content
        stored_artifact_id: Optional[str] = artifact_id
        if not stored_artifact_id:
            art_match = re.search(r"art_[a-zA-Z0-9_\-]+", content_str)
            if art_match:
                stored_artifact_id = art_match.group(0)

        # Offload to ArtifactStore if content is large and not yet offloaded
        if self.artifact_store and not stored_artifact_id and byte_size > OFFLOAD_THRESHOLD_BYTES:
            try:
                meta: ArtifactMetadata = self.artifact_store.store_text_artifact(
                    conversation_id=conversation_id,
                    tool_call_id=tool_call_id or f"call_{step_index}",
                    tool_name=tool_name,
                    content_str=content_str,
                )
                stored_artifact_id = meta.artifact_id
                content_sha256 = meta.sha256
                byte_size = meta.byte_size
                line_count = meta.line_count
            except Exception as e:
                logger.warning(f"Could not offload tool output to ArtifactStore: {e}")

        # If we have a stored artifact, retrieve its canonical metadata
        if stored_artifact_id and self.artifact_store:
            try:
                stored_meta = self.artifact_store.get_metadata(conversation_id, stored_artifact_id)
                if stored_meta:
                    content_sha256 = stored_meta.sha256
                    byte_size = stored_meta.byte_size
                    line_count = stored_meta.line_count
            except Exception:
                pass

        if stored_artifact_id:
            artifact_ref = ArtifactReference(
                artifact_id=stored_artifact_id,
                content_type="text/plain",
                byte_size=byte_size,
                line_count=line_count,
                sha256=content_sha256,
                tool_name=tool_name,
                description=f"Raw output from tool '{tool_name}' ({byte_size:,} bytes, {line_count:,} lines)",
            )

        evidence_base_ref = f"tool:{tool_name}:{tool_call_id or step_index}"
        if stored_artifact_id:
            evidence_refs = [evidence_base_ref, f"artifact:{stored_artifact_id}"]
        else:
            evidence_refs = [evidence_base_ref]

        # 1. Deterministic Extraction - Non-Zero Exit Code
        if exit_code is not None and exit_code != 0:
            failures.append(
                FailureRecord(
                    failure_type="NON_ZERO_EXIT",
                    error_message=f"Process exited with non-zero exit code {exit_code}",
                    source_tool_or_subagent=tool_name,
                    exit_code=exit_code,
                    evidence_refs=evidence_refs,
                    resolved=False,
                )
            )

        # 2. Deterministic Extraction - Test Results (pytest / unittest)
        test_data = self._extract_test_results(content_str)
        if test_data:
            summary_statement = (
                f"Test execution completed: {test_data['passed']} passed, "
                f"{test_data['failed']} failed, {test_data['skipped']} skipped, "
                f"{test_data['errors']} errors."
            )
            findings.append(
                VerifiedFinding(
                    finding_id=f"find_test_{step_index}_{tool_name}",
                    statement=summary_statement,
                    evidence_refs=evidence_refs,
                    source_location=tool_name,
                    extraction_method="deterministic_test_parser",
                    verified=True,
                )
            )
            for fail_item in test_data["failure_details"]:
                failures.append(
                    FailureRecord(
                        failure_type="TEST_FAILURE",
                        error_message=fail_item.get("reason", "Test failed"),
                        source_tool_or_subagent=f"{tool_name}:{fail_item.get('test_name', 'test')}",
                        exit_code=exit_code,
                        evidence_refs=evidence_refs,
                        resolved=False,
                    )
                )

        # 3. Deterministic Extraction - Stack Trace & Exceptions
        tb_data = self._extract_tracebacks(content_str)
        if tb_data:
            for exc in tb_data:
                failures.append(
                    FailureRecord(
                        failure_type="EXCEPTION",
                        error_message=f"{exc['exception_type']}: {exc['message']}",
                        source_tool_or_subagent=f"{tool_name}:{exc.get('frame', 'unknown')}",
                        exit_code=exit_code,
                        evidence_refs=evidence_refs,
                        resolved=False,
                    )
                )
                findings.append(
                    VerifiedFinding(
                        finding_id=f"find_exc_{step_index}_{tool_name}",
                        statement=f"Observed exception {exc['exception_type']} in {exc.get('frame', 'code')}: {exc['message']}",
                        evidence_refs=evidence_refs,
                        source_location=exc.get("frame"),
                        extraction_method="deterministic_traceback_parser",
                        verified=True,
                    )
                )

        # 4. Deterministic Extraction - Directory Listings
        dir_data = self._extract_directory_listing(content_str)
        if dir_data:
            findings.append(
                VerifiedFinding(
                    finding_id=f"find_dir_{step_index}_{tool_name}",
                    statement=f"Directory listing observed: {dir_data['file_count']} files, {dir_data['dir_count']} subdirectories. Key files: {', '.join(dir_data['prominent_files'][:8])}",
                    evidence_refs=evidence_refs,
                    source_location=dir_data.get("root_path", tool_name),
                    extraction_method="deterministic_fs_parser",
                    verified=True,
                )
            )

        # 5. Deterministic Extraction - Search / Grep Results
        search_data = self._extract_search_results(content_str)
        if search_data:
            findings.append(
                VerifiedFinding(
                    finding_id=f"find_search_{step_index}_{tool_name}",
                    statement=f"Search matched {search_data['match_count']} lines across {len(search_data['files'])} files ({', '.join(search_data['files'][:5])}).",
                    evidence_refs=evidence_refs,
                    source_location=", ".join(search_data["files"][:3]),
                    extraction_method="deterministic_search_parser",
                    verified=True,
                )
            )

        # 6. Deterministic Extraction - JSON Payload
        json_data = self._extract_json_payload(content_str)
        if json_data:
            if json_data.get("is_error"):
                failures.append(
                    FailureRecord(
                        failure_type="JSON_ERROR_RESPONSE",
                        error_message=str(json_data.get("error_message")),
                        source_tool_or_subagent=tool_name,
                        exit_code=exit_code,
                        evidence_refs=evidence_refs,
                        resolved=False,
                    )
                )
            findings.append(
                VerifiedFinding(
                    finding_id=f"find_json_{step_index}_{tool_name}",
                    statement=f"JSON response with keys: {', '.join(json_data.get('top_keys', [])[:10])}. Summary: {json_data.get('summary', '')[:200]}",
                    evidence_refs=evidence_refs,
                    source_location=tool_name,
                    extraction_method="deterministic_json_parser",
                    verified=True,
                )
            )

        # 7. Deterministic Log Scanning - Preserve Start/End and Clustered Errors
        log_errors = self._scan_large_log(content_str)
        for le in log_errors:
            # Check if this error is already captured in failures
            if not any(le["message"] in f.error_message for f in failures):
                failures.append(
                    FailureRecord(
                        failure_type=le.get("type", "LOG_ERROR"),
                        error_message=le["message"][:300],
                        source_tool_or_subagent=tool_name,
                        exit_code=exit_code,
                        evidence_refs=evidence_refs,
                        resolved=False,
                    )
                )

        # If no specialized parser matched, produce generic bounded fact
        if not findings and not failures:
            clean_snippet = sanitize_and_redact(content_str[:300].strip())
            findings.append(
                VerifiedFinding(
                    finding_id=f"find_gen_{step_index}_{tool_name}",
                    statement=f"Tool '{tool_name}' returned: {clean_snippet}",
                    evidence_refs=evidence_refs,
                    source_location=tool_name,
                    extraction_method="deterministic_bounded_raw",
                    verified=True,
                )
            )

        return findings, failures, artifact_ref

    def _extract_test_results(self, text: str) -> Optional[Dict[str, Any]]:
        """Parses pytest and unittest execution output."""
        has_test_marker = any(
            marker in text
            for marker in ("pytest", "=== test session starts", "Ran ", "passed", "failed", "FAILED ", "PASSED ")
        )
        if not has_test_marker:
            return None

        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        failures: List[Dict[str, str]] = []

        # Order-independent Pytest summary matching (e.g. "= 2 failed, 7 passed, 1 skipped in 2.34s =")
        p_match = re.search(r"(\d+)\s+passed\b", text, re.IGNORECASE)
        if p_match:
            passed = int(p_match.group(1))

        f_match = re.search(r"(\d+)\s+failed\b", text, re.IGNORECASE)
        if f_match:
            failed = int(f_match.group(1))

        s_match = re.search(r"(\d+)\s+skipped\b", text, re.IGNORECASE)
        if s_match:
            skipped = int(s_match.group(1))

        e_match = re.search(r"(\d+)\s+errors?\b", text, re.IGNORECASE)
        if e_match:
            errors = int(e_match.group(1))

        # Pytest individual failures: FAILED tests/test_foo.py::test_bar - AssertionError...
        seen_failed_tests = set()
        for f_iter in re.finditer(r"FAILED\s+([^\s:]+::\S+)(?:\s*-\s*(.*))?", text):
            t_name = f_iter.group(1)
            if t_name in seen_failed_tests:
                continue
            seen_failed_tests.add(t_name)
            reason = f_iter.group(2) or "Test failed assertion or exception"
            failures.append({"test_name": t_name, "reason": reason.strip()[:200]})

        # Unittest summary: e.g. "Ran 12 tests in 0.05s\n\nFAILED (failures=2, errors=1)"
        ut_match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([\d\.]+)s", text)
        if ut_match and passed == 0 and failed == 0:
            total_ran = int(ut_match.group(1))
            if "OK" in text:
                passed = total_ran
            else:
                fail_match = re.search(r"FAILED\s*\((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)", text)
                if fail_match:
                    failed = int(fail_match.group(1) or 0)
                    errors = int(fail_match.group(2) or 0)
                    passed = max(0, total_ran - failed - errors)

        if passed == 0 and failed == 0 and skipped == 0 and errors == 0 and not failures:
            return None

        return {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "failure_details": failures,
        }

    def _extract_tracebacks(self, text: str) -> List[Dict[str, str]]:
        """Extracts Python exception tracebacks, preserving the exception type, message, and failing frame."""
        tracebacks: List[Dict[str, str]] = []
        if "Traceback (most recent call last):" not in text and not re.search(r"\b\w+Error:\s+", text):
            return tracebacks

        # Look for full traceback blocks
        tb_blocks = re.split(r"Traceback \(most recent call last\):", text)
        for block in tb_blocks[1:]:
            lines = [l.rstrip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue

            # The last non-empty line of a traceback block is usually the exception: "ValueError: foo"
            last_line = lines[-1]
            exc_type = "Exception"
            msg = last_line
            m = re.match(r"^([a-zA-Z_]\w*(?:Error|Exception|Exit|Interrupt|Warning)):\s*(.*)", last_line)
            if m:
                exc_type = m.group(1)
                msg = m.group(2)

            # Find the last file frame line: '  File "...", line 123, in func'
            frame = "unknown"
            for line in reversed(lines[:-1]):
                fm = re.search(r'File\s+"([^"]+)",\s*line\s+(\d+)(?:,\s*in\s+(\w+))?', line)
                if fm:
                    file_path = fm.group(1)
                    line_no = fm.group(2)
                    func = fm.group(3) or "module"
                    frame = f"{os.path.basename(file_path)}:{line_no} ({func})"
                    break

            tracebacks.append({
                "exception_type": exc_type,
                "message": msg.strip(),
                "frame": frame,
            })

        return tracebacks

    def _extract_directory_listing(self, text: str) -> Optional[Dict[str, Any]]:
        """Parses directory listing or tree output."""
        has_dir_marker = "Directory:" in text or "Mode " in text or ("drwx" in text and "-rw-" in text)
        if not has_dir_marker:
            return None

        lines = text.splitlines()
        root_path = ""
        prominent_files: List[str] = []
        file_count = 0
        dir_count = 0

        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith("Mode ") or line_str.startswith("----") or set(line_str).issubset({'-', ' '}):
                continue
            if line_str.startswith("Directory:"):
                root_path = line_str.replace("Directory:", "").strip()
            elif line_str.startswith("d") or " <DIR> " in line_str:
                dir_count += 1
                parts = line_str.split()
                if parts:
                    prominent_files.append(parts[-1] + "/")
            elif line_str.startswith("-") or ("-a----" in line_str) or ("-rw-" in line_str):
                file_count += 1
                parts = line_str.split()
                if parts:
                    fname = parts[-1]
                    prominent_files.append(fname)

        if file_count == 0 and dir_count == 0:
            return None

        return {
            "root_path": root_path,
            "file_count": file_count,
            "dir_count": dir_count,
            "prominent_files": prominent_files,
        }

    def _extract_search_results(self, text: str) -> Optional[Dict[str, Any]]:
        """Parses grep / ripgrep search results."""
        matches = re.findall(r"^([^:\n\r]+):(\d+):(.*)$", text, re.MULTILINE)
        if not matches or len(matches) < 2:
            return None

        files_set: Set[str] = set()
        for f, _, _ in matches:
            files_set.add(f.strip())

        return {
            "match_count": len(matches),
            "files": sorted(list(files_set)),
        }

    def _extract_json_payload(self, text: str) -> Optional[Dict[str, Any]]:
        """Attempts to parse JSON response and identify structure or API errors."""
        text_trimmed = text.strip()
        if not (text_trimmed.startswith("{") and text_trimmed.endswith("}")):
            return None

        try:
            data = json.loads(text_trimmed)
            if not isinstance(data, dict):
                return None

            top_keys = list(data.keys())
            is_error = False
            error_msg = ""

            for ek in ("error", "errors", "detail", "message", "err"):
                if ek in data:
                    val = data[ek]
                    if val and (val != "ok" and val is not False):
                        is_error = True
                        error_msg = str(val)
                        break

            return {
                "top_keys": top_keys,
                "is_error": is_error,
                "error_message": error_msg,
                "summary": str(data)[:200],
            }
        except Exception:
            return None

    def _scan_large_log(self, text: str) -> List[Dict[str, str]]:
        """Scans lines of large outputs for error clusters, preserving early and late errors."""
        errors: List[Dict[str, str]] = []
        lines = text.splitlines()

        # If text is small, don't duplicate
        if len(lines) < 10:
            return errors

        # Regex for error keywords
        err_pattern = re.compile(
            r"\b(ERROR|CRITICAL|FATAL|EXCEPTION|FAIL|FAILED|PermissionDenied|Unauthorized|TimeoutError)\b",
            re.IGNORECASE,
        )

        for idx, line in enumerate(lines):
            line_str = line.strip()
            if err_pattern.search(line_str):
                # Classify type
                err_type = "LOG_ERROR"
                if "timeout" in line_str.lower():
                    err_type = "TIMEOUT"
                elif "permission" in line_str.lower() or "unauthorized" in line_str.lower():
                    err_type = "PERMISSION_DENIED"

                errors.append({
                    "line_no": idx + 1,
                    "type": err_type,
                    "message": line_str[:250],
                })

        return errors


class SubagentResultCompactor:
    """Consolidates findings and receipts from operational subagents,
    performs deduplication while merging evidence references,
    and detects and preserves contradictions between agents or tools.
    """

    def consolidate_subagents(
        self,
        subagent_records: List[Dict[str, Any]],
        existing_findings: List[VerifiedFinding],
    ) -> Tuple[List[SubagentReceipt], List[VerifiedFinding], List[Contradiction], List[FailureRecord]]:
        """Consolidates child subagent executions into receipts, merged findings,
        contradictions, and failure records.
        """
        receipts: List[SubagentReceipt] = []
        new_findings: List[VerifiedFinding] = []
        contradictions: List[Contradiction] = []
        failures: List[FailureRecord] = []

        for rec in subagent_records:
            s_id = rec.get("subagent_id", "subagent_unknown")
            role = rec.get("role", "general")
            status = rec.get("status", "COMPLETED")
            summary = rec.get("summary", "")
            err_msg = rec.get("error_message")

            child_evidence_refs = [f"subagent:{s_id}"]
            # Extract any artifact reference embedded in summary
            art_match = re.search(r"art_[a-zA-Z0-9_\-]+", summary)
            if art_match:
                child_evidence_refs.append(f"artifact:{art_match.group(0)}")

            child_failures: List[str] = []
            if err_msg:
                child_failures.append(err_msg)
                failures.append(
                    FailureRecord(
                        failure_type="SUBAGENT_FAILURE",
                        error_message=err_msg,
                        source_tool_or_subagent=f"subagent:{s_id}",
                        evidence_refs=child_evidence_refs,
                        resolved=False,
                    )
                )

            # Parse bullet findings from subagent summary
            sub_finding_count = 0
            lines = summary.splitlines()
            for line in lines:
                l_str = line.strip()
                if l_str.startswith(("-", "*", "•")) and len(l_str) > 5:
                    clean_finding = l_str.lstrip("-*• ").strip()
                    sub_finding_count += 1
                    new_findings.append(
                        VerifiedFinding(
                            finding_id=f"find_sub_{s_id}_{sub_finding_count}",
                            statement=clean_finding,
                            evidence_refs=child_evidence_refs,
                            source_location=f"subagent:{role}",
                            extraction_method="subagent_reported",
                            verified=False,  # Subagent reports are unverified claims until deterministically verified
                        )
                    )

            if sub_finding_count == 0 and summary.strip():
                # Fallback: Extract meaningful declarative sentences if no bullet markers were used
                sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", summary) if len(s.strip()) > 10]
                for s_stmt in sentences[:3]:
                    sub_finding_count += 1
                    new_findings.append(
                        VerifiedFinding(
                            finding_id=f"find_sub_{s_id}_{sub_finding_count}",
                            statement=s_stmt,
                            evidence_refs=child_evidence_refs,
                            source_location=f"subagent:{role}",
                            extraction_method="subagent_reported",
                            verified=False,
                        )
                    )

            receipts.append(
                SubagentReceipt(
                    subagent_id=s_id,
                    role=role,
                    status=str(status),
                    summary=summary[:400].strip(),
                    findings_count=sub_finding_count,
                    failures=child_failures,
                    evidence_refs=child_evidence_refs,
                )
            )

        # Merge new findings with existing findings, deduplicate, and detect contradictions
        all_findings = list(existing_findings) + new_findings
        deduplicated_findings, detected_contradictions = self._deduplicate_and_detect_contradictions(all_findings)
        contradictions.extend(detected_contradictions)

        return receipts, deduplicated_findings, contradictions, failures

    def _deduplicate_and_detect_contradictions(
        self,
        findings: List[VerifiedFinding]
    ) -> Tuple[List[VerifiedFinding], List[Contradiction]]:
        """Deduplicates semantically identical findings (combining evidence references)
        and detects conflicting assertions across sources.
        """
        deduped: List[VerifiedFinding] = []
        contradictions: List[Contradiction] = []

        # Antonym / contradiction keyword pairs
        conflict_pairs = [
            ("valid", "invalid"),
            ("pass", "fail"),
            ("passed", "failed"),
            ("success", "failure"),
            ("enabled", "disabled"),
            ("active", "inactive"),
            ("exists", "missing"),
            ("found", "not found"),
            ("compatible", "incompatible"),
            ("supported", "unsupported"),
            ("present", "absent"),
            ("true", "false"),
        ]

        for finding in findings:
            stmt_norm = finding.statement.strip().lower()

            # 1. Check for duplicates
            merged = False
            for existing in deduped:
                exist_norm = existing.statement.strip().lower()
                # If identical or one is fully contained in another with high overlap
                if stmt_norm == exist_norm or (len(stmt_norm) > 20 and stmt_norm in exist_norm):
                    # Combine evidence references without duplication
                    combined_refs = list(dict.fromkeys(existing.evidence_refs + finding.evidence_refs))
                    existing.evidence_refs = combined_refs
                    merged = True
                    break

            if merged:
                continue

            # 2. Check for contradictions against existing findings
            for existing in deduped:
                exist_norm = existing.statement.strip().lower()
                # Check conflict pairs
                for pos, neg in conflict_pairs:
                    is_conflict = (
                        (pos in exist_norm and neg in stmt_norm) or
                        (neg in exist_norm and pos in stmt_norm)
                    )
                    if is_conflict:
                        # Ensure they share some subject / noun context
                        exist_tokens = set(re.findall(r"\w{4,}", exist_norm))
                        stmt_tokens = set(re.findall(r"\w{4,}", stmt_norm))
                        common_tokens = exist_tokens.intersection(stmt_tokens) - {pos, neg}

                        if common_tokens:
                            # Detect contextual divergence across distinct environments
                            env_keywords = ["production", "prod", "runtime_dev", "development", "dev", "staging", "stage", "test", "local"]
                            env_a = next((w for w in env_keywords if w in exist_norm or w in (existing.source_location or "").lower()), None)
                            env_b = next((w for w in env_keywords if w in stmt_norm or w in (finding.source_location or "").lower()), None)

                            is_contextual = False
                            if env_a and env_b and env_a != env_b:
                                is_contextual = True
                                nature_str = (
                                    f"Contextual divergence across environments ({env_a} vs {env_b}) - "
                                    f"Polarity contradiction on subject '{', '.join(list(common_tokens)[:3])}' ({pos} vs {neg})"
                                )
                            else:
                                nature_str = f"Polarity contradiction on subject '{', '.join(list(common_tokens)[:3])}' ({pos} vs {neg})"

                            contradictions.append(
                                Contradiction(
                                    claim_a=existing.statement,
                                    claim_b=finding.statement,
                                    source_a=existing.source_location or "source_a",
                                    source_b=finding.source_location or "source_b",
                                    evidence_refs_a=list(existing.evidence_refs),
                                    evidence_refs_b=list(finding.evidence_refs),
                                    nature=nature_str,
                                    resolved=False,
                                    resolution_details=None,
                                    is_contextual_divergence=is_contextual,
                                )
                            )

            deduped.append(finding)

        return deduped, contradictions


# Centralized known model context capacities (exact tokens)
KNOWN_MODEL_CAPACITIES: Dict[str, int] = {
    # Gemini
    "gemini-1.5-pro": 1_048_576,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-3.1-flash-lite": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.5-flash-lite": 1_048_576,
    "gemini-3.8-flash": 1_048_576,
    "gemini-1.0-pro": 32_768,
    "gemini-pro": 32_768,
    # FreeLLMAPI / Routing Defaults
    "auto": 32_768,
    "freellm-large": 1_048_576,
    "freellm-medium": 32_768,
    "freellm-small": 8_192,
    "freellmapi-large": 1_048_576,
    "freellmapi-default": 32_768,
    "freellmapi-small": 8_192,
    "llama-3.3-70b": 65_536,
    "llama-3-8b": 8_192,
    "mistral-7b": 8_192,
    # Ollama Local
    "qwen3.5:9b": 32_768,
    "qwen2.5:7b": 32_768,
    "llama3:8b": 8_192,
}

CONSERVATIVE_PROVIDER_FALLBACKS: Dict[str, int] = {
    "gemini": 32_768,
    "google": 32_768,
    "freellmapi": 32_768,
    "freellm": 32_768,
    "ollama": 8_192,
    "default": 8_192,
}

# Backward compatibility alias
PROVIDER_MODEL_CAPACITIES = KNOWN_MODEL_CAPACITIES


class BudgetManager:
    """Enforces adaptive token budgeting for CloudReturnPacket.
    Derives available packet budget dynamically from receiving provider/model context capacity,
    existing conversation tokens, instruction/tool overhead, reasoning reserve, and contract ceiling.
    Preserves critical errors, test failures, contradictions, and escalation requirements
    while compressing descriptive verbosity.
    Guarantees that a minimum safe packet threshold never artificially manufactures context
    when available capacity is zero or negative.
    """

    MIN_SAFE_PACKET_BUDGET: int = 200

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Heuristic token estimator (~4 chars per token, bounds checked)."""
        if not text:
            return 0
        return max(1, math.ceil(len(text) / 4))

    def compute_effective_budget(
        self,
        contract: TaskDelegationContract,
        receiving_provider: Optional[str] = None,
        receiving_model: Optional[str] = None,
        existing_conversation_tokens: int = 0,
        instruction_overhead_tokens: int = 1000,
        tool_schema_overhead_tokens: int = 0,
        reserved_reasoning_tokens: int = 2048,
        minimum_safe_packet_budget: Optional[int] = None,
        coordinator: Optional[Any] = None,
        request_config: Optional[Any] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Calculates adaptive context budget based on receiving model capacity,
        existing conversation length, instruction/tool overhead, and contract limit.
        Performs explicit feasibility checking and blocks unsafe handoffs.
        """
        min_safe = minimum_safe_packet_budget if minimum_safe_packet_budget is not None else self.MIN_SAFE_PACKET_BUDGET

        # Resolve receiving model and provider
        prov_name = receiving_provider
        model_id = receiving_model

        if request_config:
            if hasattr(request_config, "model") and request_config.model:
                model_id = request_config.model
            if hasattr(request_config, "provider") and request_config.provider:
                prov_name = request_config.provider

        prov_key = (prov_name or "").lower().strip()
        model_key = (model_id or "").lower().strip()

        # Authoritative model capacity resolution
        capacity_verified = False
        estimator_used = "conservative_default_fallback"

        if model_key and model_key in KNOWN_MODEL_CAPACITIES:
            capacity = KNOWN_MODEL_CAPACITIES[model_key]
            capacity_verified = True
            estimator_used = f"known_model_capacity:{model_key}"
        elif model_key and any(m in model_key for m in ("1.5-pro", "1.5-flash", "2.0-flash", "2.5-flash", "3.1-flash", "3.5-flash", "3.8-flash", "gemini")):
            capacity = 1_048_576
            capacity_verified = True
            estimator_used = "known_model_pattern:gemini_1m"
        elif prov_key and prov_key in CONSERVATIVE_PROVIDER_FALLBACKS:
            capacity = CONSERVATIVE_PROVIDER_FALLBACKS[prov_key]
            capacity_verified = False
            estimator_used = f"conservative_provider_fallback:{prov_key}"
        elif any(k in prov_key for k in ("gemini", "google")):
            capacity = CONSERVATIVE_PROVIDER_FALLBACKS["gemini"]
            capacity_verified = False
            estimator_used = "conservative_provider_fallback:gemini"
        elif any(k in prov_key for k in ("freellm", "free")):
            capacity = CONSERVATIVE_PROVIDER_FALLBACKS["freellmapi"]
            capacity_verified = False
            estimator_used = "conservative_provider_fallback:freellmapi"
        else:
            capacity = CONSERVATIVE_PROVIDER_FALLBACKS["default"]
            capacity_verified = False
            estimator_used = "conservative_default_fallback"

        # Explicit overhead accounting
        reserved_total = (
            existing_conversation_tokens
            + instruction_overhead_tokens
            + tool_schema_overhead_tokens
            + reserved_reasoning_tokens
        )
        available_in_window = capacity - reserved_total

        # Strict safety and feasibility checking (Correction A)
        contract_ceiling = contract.constraints.context_budget_tokens

        if available_in_window <= 0:
            effective_budget = 0
            is_handoff_ready = False
            blocked_reason = (
                f"Context window exhausted ({available_in_window} tokens remaining after "
                f"{reserved_total} reserved tokens out of {capacity} ceiling). Unsafe for handoff."
            )
        else:
            effective_budget = min(contract_ceiling, available_in_window)
            if effective_budget < min_safe:
                is_handoff_ready = False
                blocked_reason = (
                    f"Available budget ({effective_budget} tokens) is below minimum safe packet budget "
                    f"({min_safe} tokens). Unsafe for handoff."
                )
            else:
                is_handoff_ready = True
                blocked_reason = None

        metadata = {
            "target_provider": prov_name or "default",
            "target_model": model_id or "default",
            "effective_context_ceiling": capacity,
            "reserved_context_budget": reserved_total,
            "available_in_window": available_in_window,
            "contract_ceiling": contract_ceiling,
            "effective_budget": effective_budget,
            "estimator_used": estimator_used,
            "is_estimated": not capacity_verified,
            "capacity_verified": capacity_verified,
            "is_handoff_ready": is_handoff_ready,
            "handoff_blocked_reason": blocked_reason,
            "tool_schema_overhead_tokens": tool_schema_overhead_tokens,
            "minimum_safe_packet_budget": min_safe,
        }
        return effective_budget, metadata

    def enforce_budget(
        self,
        packet: CloudReturnPacket,
        target_budget_tokens: int,
        original_raw_bytes: int,
        budget_metadata: Optional[Dict[str, Any]] = None,
    ) -> CloudReturnPacket:
        """Applies adaptive progressive compression to packet if it exceeds target budget,
        while strictly preserving all failures, contradictions, and escalation requirements.
        """
        # Calculate original tokens (raw outputs)
        original_estimated_tokens = max(1, math.ceil(original_raw_bytes / 4))

        meta = budget_metadata or {}
        min_safe = meta.get("minimum_safe_packet_budget", self.MIN_SAFE_PACKET_BUDGET)
        is_handoff_ready = meta.get("is_handoff_ready", True)
        blocked_reason = meta.get("handoff_blocked_reason")
        budget_exceeded = False
        duplicate_merged_count = 0

        # Initial estimation of current packet
        packet_json = packet.model_dump_json()
        current_tokens = self.estimate_tokens(packet_json)

        # Unsafe context window check: if target budget is below safe threshold, handoff is impossible
        if target_budget_tokens < min_safe or not is_handoff_ready:
            budget_exceeded = True
            is_handoff_ready = False
            packet.unresolved_questions.append(
                f"Notice: Packet cannot be safely handed off to cloud receiver ({blocked_reason or 'Context capacity exhausted'}). "
                "Full evidence retained in local artifacts."
            )
            packet.escalation_requirements.append(
                EscalationRequirement(
                    reason=f"Context budget constraint violated: {blocked_reason or 'Context capacity exhausted'}",
                    severity="HIGH",
                    required_action="Reduce conversation context or escalate to compact mode.",
                    context={"target_budget_tokens": target_budget_tokens, "blocked_reason": blocked_reason},
                )
            )

        # Progressive Compaction Ladder if current packet exceeds target budget
        if current_tokens > target_budget_tokens and target_budget_tokens >= min_safe:
            logger.info(
                f"Packet estimated tokens ({current_tokens}) exceeds budget ({target_budget_tokens}). "
                "Applying adaptive progressive context compression."
            )

            # ---------------------------------------------------------
            # Stage 1: Mild Truncation
            # ---------------------------------------------------------
            if len(packet.summary) > 350:
                packet.summary = packet.summary[:350].strip() + "..."

            for f in packet.verified_findings:
                if len(f.statement) > 180:
                    f.statement = f.statement[:180].strip() + "..."

            for interp in packet.interpretations:
                if len(interp.statement) > 180:
                    interp.statement = interp.statement[:180].strip() + "..."
                if len(interp.assumptions) > 2:
                    interp.assumptions = interp.assumptions[:2]

            for r in packet.subagent_receipts:
                if len(r.summary) > 180:
                    r.summary = r.summary[:180].strip() + "..."

            current_tokens = self.estimate_tokens(packet.model_dump_json())

            # ---------------------------------------------------------
            # Stage 2: Medium Compaction (if still over budget)
            # ---------------------------------------------------------
            if current_tokens > target_budget_tokens:
                logger.debug(f"Stage 1 compaction yielded {current_tokens} tokens (> {target_budget_tokens}). Entering Stage 2.")
                if len(packet.summary) > 200:
                    packet.summary = packet.summary[:200].strip() + "..."

                for f in packet.verified_findings:
                    if len(f.statement) > 120:
                        f.statement = f.statement[:120].strip() + "..."
                    # Condense evidence_refs to top 2, prioritizing artifact references
                    if len(f.evidence_refs) > 2:
                        art_refs = [r for r in f.evidence_refs if "art_" in r or "artifact:" in r]
                        other_refs = [r for r in f.evidence_refs if r not in art_refs]
                        f.evidence_refs = (art_refs + other_refs)[:2]

                for fail in packet.failures:
                    if len(fail.error_message) > 120:
                        fail.error_message = fail.error_message[:120].strip() + "..."

                for interp in packet.interpretations:
                    if len(interp.statement) > 100:
                        interp.statement = interp.statement[:100].strip() + "..."
                    if len(interp.assumptions) > 2:
                        interp.assumptions = interp.assumptions[:2]
                if len(packet.interpretations) > 2:
                    packet.interpretations = packet.interpretations[:2]

                for r in packet.subagent_receipts:
                    if len(r.summary) > 100:
                        r.summary = r.summary[:100].strip() + "..."
                    if len(r.evidence_refs) > 2:
                        r.evidence_refs = r.evidence_refs[:2]

                for art in packet.artifact_references:
                    if art.description and len(art.description) > 60:
                        art.description = art.description[:60].strip() + "..."

                packet.unresolved_questions = [
                    (q[:100].strip() + "..." if len(q) > 100 else q) for q in packet.unresolved_questions
                ]

                current_tokens = self.estimate_tokens(packet.model_dump_json())

            # ---------------------------------------------------------
            # Stage 3: Deep Compaction (if still over budget)
            # ---------------------------------------------------------
            if current_tokens > target_budget_tokens:
                logger.debug(f"Stage 2 compaction yielded {current_tokens} tokens (> {target_budget_tokens}). Entering Stage 3.")
                if len(packet.summary) > 120:
                    packet.summary = packet.summary[:120].strip() + "..."

                # Retain essential interpretation but deeply compress
                if packet.interpretations:
                    for interp in packet.interpretations:
                        if len(interp.statement) > 60:
                            interp.statement = interp.statement[:60].strip() + "..."
                        if len(interp.assumptions) > 1:
                            interp.assumptions = interp.assumptions[:1]
                    if len(packet.interpretations) > 1:
                        packet.interpretations = packet.interpretations[:1]

                for fail in packet.failures:
                    if len(fail.error_message) > 80:
                        fail.error_message = fail.error_message[:80].strip() + "..."

                for r in packet.subagent_receipts:
                    if len(r.summary) > 60:
                        r.summary = r.summary[:60].strip() + "..."
                    if len(r.evidence_refs) > 1:
                        r.evidence_refs = r.evidence_refs[:1]
                if len(packet.subagent_receipts) > 2:
                    packet.subagent_receipts = packet.subagent_receipts[:2]

                # Compress verified findings statements while strictly preserving each finding record
                for f in packet.verified_findings:
                    if len(f.statement) > 80:
                        f.statement = f.statement[:80].strip() + "..."
                    if len(f.evidence_refs) > 1:
                        art_refs = [r for r in f.evidence_refs if "art_" in r or "artifact:" in r]
                        f.evidence_refs = art_refs[:1] if art_refs else f.evidence_refs[:1]

                if len(packet.unresolved_questions) > 3:
                    packet.unresolved_questions = packet.unresolved_questions[:3]
                packet.unresolved_questions = [
                    (q[:60].strip() + "..." if len(q) > 60 else q) for q in packet.unresolved_questions
                ]

                current_tokens = self.estimate_tokens(packet.model_dump_json())

            # Evaluate final readiness after all compaction passes
            if current_tokens <= target_budget_tokens:
                budget_exceeded = False
                is_handoff_ready = True
                blocked_reason = None
            else:
                budget_exceeded = True
                is_handoff_ready = False
                blocked_reason = (
                    f"Packet size ({current_tokens} tokens) exceeds context budget "
                    f"({target_budget_tokens} tokens) after deep compaction."
                )
                packet.unresolved_questions.append(
                    f"Notice: Packet size ({current_tokens} tokens) exceeds context budget "
                    f"({target_budget_tokens} tokens). Full evidence retained in local artifacts."
                )

        # Compute reduction ratio
        reduction_ratio = 0.0
        if original_estimated_tokens > 0:
            reduction_ratio = max(0.0, round(1.0 - (current_tokens / original_estimated_tokens), 3))

        # Populate compaction metrics with explicit handoff readiness telemetry
        packet.compaction_metrics = CompactionMetrics(
            original_bytes=original_raw_bytes,
            original_estimated_tokens=original_estimated_tokens,
            final_estimated_tokens=current_tokens,
            reduction_ratio=reduction_ratio,
            tool_outputs_compacted=len(packet.artifact_references),
            deterministic_extractions=len(packet.verified_findings),
            semantic_summaries=len(packet.interpretations),
            subagent_receipts_count=len(packet.subagent_receipts),
            duplicate_findings_merged=duplicate_merged_count,
            contradictions_count=len(packet.contradictions),
            artifacts_referenced=len(packet.artifact_references),
            budget_exceeded=budget_exceeded,
            estimator_used=meta.get("estimator_used", "heuristic_chars_div_4"),
            target_provider=meta.get("target_provider"),
            target_model=meta.get("target_model"),
            effective_context_ceiling=meta.get("effective_context_ceiling", 0),
            reserved_context_budget=meta.get("reserved_context_budget", 0),
            is_estimated=meta.get("is_estimated", True),
            is_handoff_ready=is_handoff_ready and not budget_exceeded,
            handoff_blocked_reason=blocked_reason or (f"Packet size ({current_tokens} tokens) exceeds budget ({target_budget_tokens} tokens)" if budget_exceeded else None),
            tool_schema_overhead_tokens=meta.get("tool_schema_overhead_tokens", 0),
            capacity_verified=meta.get("capacity_verified", True),
        )

        return packet
